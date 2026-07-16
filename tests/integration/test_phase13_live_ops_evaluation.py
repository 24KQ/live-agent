"""Phase 13 Task 7 LiveOps 配对评估的 PostgreSQL 恢复验证。"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from src.config.settings import get_settings
from src.specialist_evaluation.live_ops import (
    LiveOpsCaseLabel,
    LiveOpsPairedEvaluationRecorder,
    ValidationGateStatus,
)
from src.specialist_evaluation.models import (
    EvaluationCandidate,
    EvaluationManifest,
    EvaluationManifestKind,
    EvaluationRun,
    _build_formal_manifest_authorization,
)
from src.specialist_evaluation.store import (
    PostgresSpecialistEvaluationStore,
    initialize_specialist_evaluation_schema,
)
from src.specialist_runtime.live_ops import (
    LiveOpsAction,
    LiveOpsSuggestion,
    PriorityLiveOpsPolicy,
)
from src.specialist_runtime.models import AgentResult, AgentResultStatus, EvidenceRef


ROOT = Path(__file__).parents[2]
EVALUATION_ROOT = ROOT / "evaluation"
HASH_A = "a" * 64
HASH_B = "b" * 64


def _records(split: str) -> tuple[list[dict], dict[str, LiveOpsCaseLabel]]:
    """读取已冻结 v3 case/label；集成测试不能从代码临时伪造 gold 事实。"""

    cases = [
        json.loads(line)
        for line in (
            EVALUATION_ROOT / "cases" / "phase13-live-ops-v3" / f"{split}.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    labels = {
        item["case_id"]: LiveOpsCaseLabel.model_validate(item["label"])
        for item in (
            json.loads(line)
            for line in (
                EVALUATION_ROOT / "labels" / "phase13-live-ops-v3" / f"{split}.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        )
    }
    return cases, labels


def _manifest(suffix: str) -> EvaluationManifest:
    """构造满足 240 case 固定约束的测试 Manifest，LiveOps 仍使用真实 v3 case ID。"""

    live_by_split = {split: _records(split)[0] for split in ("development", "validation", "holdout")}
    counts = {"development": 20, "validation": 40, "holdout": 20}
    case_ids = {split: [item["case_id"] for item in records] for split, records in live_by_split.items()}
    candidate_map = {
        case_id: EvaluationCandidate.LIVE_OPS.value
        for identifiers in case_ids.values()
        for case_id in identifiers
    }
    for candidate in (EvaluationCandidate.PLANNER, EvaluationCandidate.REVIEW_MEMORY):
        for split, count in counts.items():
            for index in range(1, count + 1):
                case_id = f"postgres-{suffix}-{candidate.value.lower()}-{split}-{index:03d}"
                case_ids[split].append(case_id)
                candidate_map[case_id] = candidate.value
    return EvaluationManifest(
        manifest_id=f"phase13-live-ops-v3-{suffix}",
        manifest_version="3.0.0",
        manifest_kind=EvaluationManifestKind.FORMAL_EVALUATION,
        source_commit="a" * 40,
        dataset_digest=HASH_A,
        schema_digest=HASH_B,
        generator_digest=HASH_A,
        seed=20260716,
        development_case_ids=tuple(case_ids["development"]),
        validation_case_ids=tuple(case_ids["validation"]),
        holdout_case_ids=tuple(case_ids["holdout"]),
        case_candidate_map=candidate_map,
        profile_bundle_digest=HASH_A,
        prompt_bundle_digest=HASH_B,
        result_schema_bundle_digest=HASH_A,
        pricing_source_digest=HASH_B,
        temperature=Decimal("0"),
        code_digest=HASH_A,
        price_policy_digest=HASH_B,
        endpoint_host="api.deepseek.com",
        model_id="deepseek-v4-flash",
        candidate_ids=tuple(item.value for item in EvaluationCandidate),
    )


@pytest.fixture
def live_ops_postgres_run():
    """为单个测试创建隔离 Manifest/Run，并按外键依赖逆序精确清理。"""

    settings = get_settings()
    initialize_specialist_evaluation_schema(settings)
    suffix = str(uuid4())
    manifest = _manifest(suffix)
    run = EvaluationRun(
        run_id=f"run-live-ops-{suffix}",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=EvaluationCandidate.LIVE_OPS,
    )
    authorization = _build_formal_manifest_authorization(manifest)
    store = PostgresSpecialistEvaluationStore(settings)
    store.register_manifest(manifest, authorization=authorization)
    store.create_run(run, authorization=authorization)
    try:
        yield settings, store, run
    finally:
        with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                # decision 通过 run 外键关联；必须先于 run 和 Manifest 删除。
                cursor.execute(
                    "DELETE FROM specialist_retention_decisions WHERE run_id=%s;", (run.run_id,)
                )
                for table in (
                    "specialist_paired_metrics",
                    "specialist_selected_case_results",
                    "specialist_case_attempts",
                    "specialist_evaluation_runs",
                    "specialist_evaluation_manifests",
                ):
                    cursor.execute(
                        f"DELETE FROM {table} WHERE "
                        f"{'run_id=%s' if table != 'specialist_evaluation_manifests' else 'manifest_id=%s'};",
                        (run.run_id if table != "specialist_evaluation_manifests" else manifest.manifest_id,),
                    )
            connection.commit()


def test_postgres_restart_rebuilds_live_ops_validation_shard(live_ops_postgres_run) -> None:
    """重建 Store 后，首个十例 shard 必须从 selected Attempt 返回 CONTINUE 而非重新调用 Agent。"""

    settings, store, run = live_ops_postgres_run
    claim = store.claim_next_run("live-ops-postgres-worker", manifest_id=run.manifest_id)
    assert claim is not None
    cases, labels = _records("validation")
    recorder = LiveOpsPairedEvaluationRecorder(store=store)
    baseline = PriorityLiveOpsPolicy()
    for case in cases[:10]:
        guidance = case["input"]["verified_guidance"]
        suggestion = LiveOpsSuggestion(
            action=LiveOpsAction(guidance["recommended_action"]),
            reason_code="SCRIPTED_VERIFIED_GUIDANCE",
            suggestion="FOLLOW_VERIFIED_GUIDANCE",
            evidence_refs=tuple(EvidenceRef.model_validate(item) for item in case["input"]["evidence_refs"]),
        )
        recorder.record_pair(
            run=run,
            claim=claim,
            case=case,
            label=labels[case["case_id"]],
            baseline=baseline.decide(case["input"]),
            agent_result=AgentResult(
                task_id=f"live-ops:{case['case_id']}",
                profile_id="live-ops-agent",
                profile_version="1.0.0",
                status=AgentResultStatus.SUCCEEDED,
                output=suggestion.model_dump(mode="json"),
                summary="SCRIPTED",
                model_calls=1,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                latency_ms=Decimal("1"),
                cost_cny=Decimal("0.001"),
            ),
        )

    recovered = LiveOpsPairedEvaluationRecorder(
        store=PostgresSpecialistEvaluationStore(settings)
    )
    decision = recovered.rebuild_validation_gate(run=run)

    assert decision.status is ValidationGateStatus.CONTINUE
    assert decision.completed_cases == 10
