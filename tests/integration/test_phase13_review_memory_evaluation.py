"""Phase 13 Task 10 ReviewMemoryAgent 的冻结数据集集成验证。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from src.config.settings import get_settings
from src.specialist_evaluation.models import (
    EvaluationCandidate,
    EvaluationManifest,
    EvaluationManifestKind,
    EvaluationRun,
    _build_formal_manifest_authorization,
)
from src.specialist_evaluation.review_memory import (
    ReviewMemoryCaseLabel,
    ReviewMemoryPairedEvaluationRecorder,
    ReviewValidationStatus,
)
from src.specialist_evaluation.store import (
    InMemorySpecialistEvaluationStore,
    PostgresSpecialistEvaluationStore,
    initialize_specialist_evaluation_schema,
)
from src.specialist_runtime.budget import InMemoryModelBudgetStore
from src.specialist_runtime.evidence import EvidenceResolverRegistry, ResolvedEvidence
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import (
    AgentResult,
    AgentResultStatus,
    EvidenceKind,
    canonical_json_sha256,
)
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
from src.specialist_runtime.review_memory import (
    ReviewMemoryAgentAdapter,
    ReviewMemoryBaseline,
    ReviewMemoryRecommendation,
    build_review_memory_profile,
)
from src.specialist_runtime.runner import BoundedSpecialistRunner
from src.specialist_runtime.scripted_model import ScriptedAgentModel
from src.skill_runtime.catalog import get_default_skill_catalog


ROOT = Path(__file__).parents[2]
EVALUATION_ROOT = ROOT / "evaluation"
HASH_A = "a" * 64


class _EmptyLoader:
    """Review case 只提供 AUDIT 证据，其余类型不能被本任务隐式读取。"""

    def load(self, _evidence_id: str):
        return None


class _AuditLoader:
    """把冻结 case 的 trace 引用投影为 Runner 可复核的权威 AUDIT 快照。"""

    def __init__(self, records: dict[str, ResolvedEvidence]) -> None:
        self._records = dict(records)

    def load(self, evidence_id: str) -> ResolvedEvidence | None:
        return self._records.get(evidence_id)


class _NoSkillPort:
    """理想脚本直接 FINAL；若调用播后 Skill 即表示评估权限或脚本发生回归。"""

    async def invoke(self, **_kwargs):
        raise AssertionError("ReviewMemory scripted evaluation must not call a Skill")


class _PricingPolicy:
    """固定的本地计价只验证预算账本闭环，不访问真实模型计费接口。"""

    policy_digest = HASH_A

    def count_input_tokens(self, _request) -> int:
        return 10

    def worst_case_cost(self, _request, _profile) -> Decimal:
        return Decimal("0.001")

    def actual_cost(self, _usage, _profile) -> Decimal:
        return Decimal("0.001")


def _records(split: str) -> tuple[list[dict], dict[str, ReviewMemoryCaseLabel]]:
    """读取 Task 6 固化的 review 输入与独立标签，不从候选输出生成 gold。"""

    cases = [
        json.loads(line)
        for line in (
            EVALUATION_ROOT / "cases" / "phase13" / f"review_memory-{split}.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    labels = {
        item["case_id"]: ReviewMemoryCaseLabel.model_validate(item["label"])
        for item in (
            json.loads(line)
            for line in (
                EVALUATION_ROOT / "labels" / "phase13" / f"review_memory-{split}.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        )
    }
    return cases, labels


def _formal_manifest() -> EvaluationManifest:
    """测试 Store 用新的正式身份引用完整 240 case 冻结基线。"""

    source = json.loads(
        (EVALUATION_ROOT / "manifests" / "phase13-v2.json").read_text(encoding="utf-8")
    )["store_manifest"]
    payload = {
        **source,
        "manifest_id": "phase13-review-memory-scripted-test",
        "manifest_kind": EvaluationManifestKind.FORMAL_EVALUATION.value,
        "source_commit": "a" * 40,
    }
    payload.pop("manifest_digest", None)
    return EvaluationManifest.model_validate(payload)


def _postgres_manifest(suffix: str) -> EvaluationManifest:
    """PostgreSQL 用例使用随机 Manifest ID，避免并发或历史探针互相覆盖。"""

    payload = _formal_manifest().model_dump(mode="json", exclude={"manifest_digest"})
    payload["manifest_id"] = f"phase13-review-memory-postgres-{suffix}"
    return EvaluationManifest.model_validate(payload)


def _ideal_recommendation(
    case: dict, label: ReviewMemoryCaseLabel
) -> ReviewMemoryRecommendation:
    """生成受限 Schema 内的理想脚本输出，以验证管线而非伪造真实模型能力。"""

    case_input = case["input"]
    evidence_ids = tuple(item["evidence_id"] for item in case_input["decision_traces"][:2])
    return ReviewMemoryRecommendation(
        attribution={
            "category": label.attribution_category,
            "reason_code": "SCRIPTED_GROUNDED_ATTRIBUTION",
            "evidence_ids": evidence_ids,
        },
        memory_candidates=(
            {
                "class": label.memory_candidate_class,
                "product_id": case_input["catalog_whitelist"]["product_ids"][0],
                "category": label.attribution_category,
                "tag": case_input["catalog_whitelist"]["tags"][0],
                "evidence_ids": evidence_ids,
            },
        ),
        evidence_ids=evidence_ids,
    )


def test_scripted_review_memory_runs_80_cases_and_rebuilds_validation_gate() -> None:
    """实际 Runner 运行 80 case；40 validation 的 selected facts 可重建并解锁 holdout。"""

    profile = build_review_memory_profile(EVALUATION_ROOT)
    all_cases: list[dict] = []
    labels: dict[str, ReviewMemoryCaseLabel] = {}
    records: dict[str, ResolvedEvidence] = {}
    for split in ("development", "validation", "holdout"):
        cases, split_labels = _records(split)
        all_cases.extend(cases)
        labels.update(split_labels)
        for case in cases:
            for reference in case["input"]["decision_traces"]:
                records[reference["evidence_id"]] = ResolvedEvidence(
                    kind=EvidenceKind.AUDIT,
                    evidence_id=reference["evidence_id"],
                    source_version=reference["source_version"],
                    digest=reference["digest"],
                    anchor_id=reference["anchor_id"],
                    room_id=reference["room_id"],
                    payload={"case_id": case["case_id"]},
                )
    task_builder = ReviewMemoryAgentAdapter(runner=None, profile=profile)  # type: ignore[arg-type]
    outcomes = {}
    for case in all_cases:
        task = task_builder.build_task(case)
        recommendation = _ideal_recommendation(case, labels[case["case_id"]])
        action = {
            "kind": "FINAL",
            "final_output": recommendation.model_dump(mode="json", by_alias=True),
            "evidence_refs": case["input"]["decision_traces"],
        }
        outcomes[f"{task.task_id}:{task.task_digest}:model:1"] = (
            ModelSuccess(
                request_id=f"{task.task_id}:{task.task_digest}:model:1",
                model_id=profile.model_id,
                output=action,
                usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                response_digest=canonical_json_sha256(action),
                latency_ms=Decimal("1"),
            ),
        )
    scripted_model = ScriptedAgentModel(outcomes=outcomes)
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=scripted_model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=EvidenceResolverRegistry(
            {
                kind: _AuditLoader(records) if kind is EvidenceKind.AUDIT else _EmptyLoader()
                for kind in EvidenceKind
            }
        ),
        skill_port=_NoSkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda task: task.input_snapshot["anchor_id"],
        pricing_policy=_PricingPolicy(),
    )
    adapter = ReviewMemoryAgentAdapter(runner=runner, profile=profile)
    store = InMemorySpecialistEvaluationStore()
    manifest = _formal_manifest()
    authorization = _build_formal_manifest_authorization(manifest)
    store.register_manifest(manifest, authorization=authorization)
    run = EvaluationRun(
        run_id="run-review-memory-scripted",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=EvaluationCandidate.REVIEW_MEMORY,
    )
    store.create_run(run, authorization=authorization)
    claim = store.claim_next_run("review-memory-scripted-worker", manifest_id=manifest.manifest_id)
    assert claim is not None
    recorder = ReviewMemoryPairedEvaluationRecorder(store=store, labels_by_case=labels)
    baseline = ReviewMemoryBaseline()

    for case in all_cases[:60]:
        result = asyncio.run(adapter.run_case(case))
        recorder.record_pair(
            run=run,
            claim=claim,
            case=case,
            label=labels[case["case_id"]],
            baseline=baseline.decide(case["input"]),
            agent_result=result,
        )
    assert recorder.rebuild_validation_gate(run=run).status is ReviewValidationStatus.HOLDOUT_UNLOCKED
    for case in all_cases[60:]:
        result = asyncio.run(adapter.run_case(case))
        recorder.record_pair(
            run=run,
            claim=claim,
            case=case,
            label=labels[case["case_id"]],
            baseline=baseline.decide(case["input"]),
            agent_result=result,
        )

    grounded, candidate = recorder.save_validation_metrics(run=run, claim=claim)
    assert grounded.agent_success_count == 40
    assert candidate.agent_success_count == 40
    assert candidate.delta_percentage_points >= Decimal("10")
    assert scripted_model.call_count == 80
    assert len(store.list_attempts(run.run_id)) == 160


@pytest.fixture
def review_memory_postgres_run():
    """每次重启验证使用隔离的 Manifest/Run，并按外键逆序精确清理自身数据。"""

    settings = get_settings()
    initialize_specialist_evaluation_schema(settings)
    suffix = str(uuid4())
    manifest = _postgres_manifest(suffix)
    run = EvaluationRun(
        run_id=f"run-review-memory-{suffix}",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=EvaluationCandidate.REVIEW_MEMORY,
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
                cursor.execute("DELETE FROM specialist_retention_decisions WHERE run_id=%s;", (run.run_id,))
                for table in (
                    "specialist_paired_metrics",
                    "specialist_selected_case_results",
                    "specialist_case_attempts",
                    "specialist_evaluation_runs",
                ):
                    cursor.execute(f"DELETE FROM {table} WHERE run_id=%s;", (run.run_id,))
                cursor.execute(
                    "DELETE FROM specialist_evaluation_manifests WHERE manifest_id=%s;",
                    (manifest.manifest_id,),
                )
            connection.commit()


def test_postgres_restart_rebuilds_review_memory_validation_shard(review_memory_postgres_run) -> None:
    """重建 Postgres Store 后，十例 selected pair 必须恢复 shard，而不是创建第二次 Attempt。"""

    settings, store, run = review_memory_postgres_run
    claim = store.claim_next_run("review-memory-postgres-worker", manifest_id=run.manifest_id)
    assert claim is not None
    cases, labels = _records("validation")
    recorder = ReviewMemoryPairedEvaluationRecorder(store=store, labels_by_case=labels)
    baseline = ReviewMemoryBaseline()
    for case in cases[:10]:
        recommendation = _ideal_recommendation(case, labels[case["case_id"]])
        recorder.record_pair(
            run=run,
            claim=claim,
            case=case,
            label=labels[case["case_id"]],
            baseline=baseline.decide(case["input"]),
            agent_result=AgentResult(
                task_id=f"review-memory:{case['case_id']}",
                profile_id="review-memory-agent",
                profile_version="1.0.0",
                status=AgentResultStatus.SUCCEEDED,
                output=recommendation.model_dump(mode="json", by_alias=True),
                summary="SCRIPTED",
                model_calls=1,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                latency_ms=Decimal("1"),
                cost_cny=Decimal("0.001"),
            ),
        )

    recovered = ReviewMemoryPairedEvaluationRecorder(
        store=PostgresSpecialistEvaluationStore(settings),
        labels_by_case=labels,
    )
    decision = recovered.rebuild_validation_gate(run=run)

    assert decision.status is ReviewValidationStatus.CONTINUE
    assert decision.completed_cases == 10
