"""Phase 13 Task 7 LiveOpsAgent 纵向切片测试。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.specialist_evaluation.live_ops import (
    LiveOpsCaseLabel,
    LiveOpsCaseScore,
    LiveOpsPairedEvaluationRecorder,
    LiveOpsValidationGate,
    ValidationGateStatus,
    score_live_ops_suggestion,
)
from src.specialist_evaluation.models import (
    EvaluationCandidate,
    EvaluationManifest,
    EvaluationManifestKind,
    EvaluationRun,
    EvaluationSplit,
    EvaluationSubject,
    _build_formal_manifest_authorization,
)
from src.specialist_evaluation.store import InMemorySpecialistEvaluationStore
from src.specialist_runtime.live_ops import (
    LiveOpsAgentAdapter,
    LiveOpsAction,
    LiveOpsSuggestion,
    PriorityLiveOpsPolicy,
    build_live_ops_profile,
)
from src.specialist_runtime.budget import InMemoryModelBudgetStore
from src.specialist_runtime.evidence import (
    EvidenceResolverRegistry,
    ResolvedEvidence,
)
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import (
    AgentResult,
    AgentResultStatus,
    EvidenceKind,
    EvidenceRef,
    SpecialistTaskKind,
    canonical_json_sha256,
)
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
from src.specialist_runtime.runner import BoundedSpecialistRunner
from src.specialist_runtime.scripted_model import ScriptedAgentModel
from src.skill_runtime.catalog import get_default_skill_catalog


ROOT = Path(__file__).parents[2]
EVALUATION_ROOT = ROOT / "evaluation"
HASH_A = "a" * 64
HASH_B = "b" * 64


def _case(index: int) -> dict:
    path = EVALUATION_ROOT / "cases" / "phase13-live-ops-v3" / "development.jsonl"
    return json.loads(path.read_text(encoding="utf-8").splitlines()[index - 1])


def _label(index: int) -> LiveOpsCaseLabel:
    path = EVALUATION_ROOT / "labels" / "phase13-live-ops-v3" / "development.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[index - 1])
    return LiveOpsCaseLabel.model_validate(record["label"])


def _split_records(split: str) -> tuple[list[dict], dict[str, LiveOpsCaseLabel]]:
    """读取版本化冻结 JSONL，测试不得自行生成或改变 LiveOps gold label。"""

    case_path = EVALUATION_ROOT / "cases" / "phase13-live-ops-v3" / f"{split}.jsonl"
    label_path = EVALUATION_ROOT / "labels" / "phase13-live-ops-v3" / f"{split}.jsonl"
    cases = [json.loads(line) for line in case_path.read_text(encoding="utf-8").splitlines()]
    labels = {
        record["case_id"]: LiveOpsCaseLabel.model_validate(record["label"])
        for record in (json.loads(line) for line in label_path.read_text(encoding="utf-8").splitlines())
    }
    return cases, labels


def _evaluation_manifest() -> EvaluationManifest:
    """构造满足 Store 固定 240 case 约束的测试 Formal Manifest，LiveOps 仍使用真实 v3 ID。"""

    development, _ = _split_records("development")
    validation, _ = _split_records("validation")
    holdout, _ = _split_records("holdout")
    split_ids: dict[str, tuple[str, ...]] = {
        "development": tuple(item["case_id"] for item in development),
        "validation": tuple(item["case_id"] for item in validation),
        "holdout": tuple(item["case_id"] for item in holdout),
    }
    candidate_map = {
        case_id: EvaluationCandidate.LIVE_OPS.value
        for values in split_ids.values()
        for case_id in values
    }
    all_ids: dict[str, list[str]] = {split: list(values) for split, values in split_ids.items()}
    for candidate in (EvaluationCandidate.PLANNER, EvaluationCandidate.REVIEW_MEMORY):
        for split, count in (("development", 20), ("validation", 40), ("holdout", 20)):
            for index in range(1, count + 1):
                case_id = f"test-{candidate.value.lower()}-{split}-{index:03d}"
                all_ids[split].append(case_id)
                candidate_map[case_id] = candidate.value
    return EvaluationManifest(
        manifest_id="phase13-live-ops-v3-test",
        manifest_version="3.0.0",
        manifest_kind=EvaluationManifestKind.FORMAL_EVALUATION,
        source_commit="a" * 40,
        dataset_digest=HASH_A,
        schema_digest=HASH_B,
        generator_digest=HASH_A,
        seed=20260716,
        development_case_ids=tuple(all_ids["development"]),
        validation_case_ids=tuple(all_ids["validation"]),
        holdout_case_ids=tuple(all_ids["holdout"]),
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


def _live_ops_run(store: InMemorySpecialistEvaluationStore):
    """以 Task 5 的可信 Formal Manifest 工厂建立受 fencing 保护的 LiveOps Run。"""

    manifest = _evaluation_manifest()
    authorization = _build_formal_manifest_authorization(manifest)
    store.register_manifest(manifest, authorization=authorization)
    run = EvaluationRun(
        run_id="run-live-ops-v3",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=EvaluationCandidate.LIVE_OPS,
    )
    store.create_run(run, authorization=authorization)
    claim = store.claim_next_run("live-ops-test-worker", manifest_id=manifest.manifest_id)
    assert claim is not None
    return run, claim


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (1, LiveOpsAction.NO_ACTION),
        (2, LiveOpsAction.HUMAN_ATTENTION),
        (3, LiveOpsAction.SWITCH_PRODUCT_SUGGESTION),
        (4, LiveOpsAction.DANMAKU_REPLY_SUGGESTION),
    ],
)
def test_priority_live_ops_policy_covers_frozen_business_priority(index: int, expected) -> None:
    """未解除风险优先于弹幕；有备品建议切换，无风险高频问题才回复。"""

    suggestion = PriorityLiveOpsPolicy().decide(_case(index)["input"])
    assert suggestion.action is expected
    assert {ref.evidence_id for ref in suggestion.evidence_refs} == {
        item["evidence_id"] for item in _case(index)["input"]["evidence_refs"]
    }


def test_live_ops_suggestion_and_profile_are_strictly_governed() -> None:
    """候选输出、预算和 Skill 权限必须来自 Task 6 冻结 Manifest。"""

    suggestion = PriorityLiveOpsPolicy().decide(_case(3)["input"])
    replay = LiveOpsSuggestion.model_validate(suggestion.model_dump(mode="json"))
    assert replay == suggestion
    with pytest.raises(ValidationError):
        LiveOpsSuggestion.model_validate({**suggestion.model_dump(mode="json"), "authorization": True})

    profile = build_live_ops_profile(EVALUATION_ROOT)
    assert profile.max_model_calls == 2
    assert profile.max_skill_calls == 3
    assert profile.max_total_tokens == 4000
    assert profile.deadline_seconds == 5
    assert set(profile.skill_versions) == {
        "aggregate_danmaku_questions",
        "generate_danmaku_reply",
        "generate_on_live_prompt",
        "on_live_context_collect",
        "recommend_backup_product",
    }
    assert {"handle_sold_out_event", "set_product_price", "setup_live_session"}.isdisjoint(
        profile.allowed_skill_ids
    )


def test_live_ops_scoring_requires_expected_action_and_resolved_evidence() -> None:
    """动作与证据都匹配后，才可消费 gold 的 action/recovery 事实。"""

    case = _case(4)
    suggestion = PriorityLiveOpsPolicy().decide(case["input"])
    score = score_live_ops_suggestion(
        case_id=case["case_id"],
        suggestion=suggestion,
        label=_label(4),
        allowed_evidence_refs=tuple(
            EvidenceRef.model_validate(item) for item in case["input"]["evidence_refs"]
        ),
    )
    assert score.action_success is True
    assert score.incident_recovery is True
    assert score.severe_violation is False

    forged = LiveOpsSuggestion.model_validate(
        {
            **suggestion.model_dump(mode="json"),
            "evidence_refs": [
                {**suggestion.evidence_refs[0].model_dump(mode="json"), "digest": "f" * 64}
            ],
        }
    )
    denied = score_live_ops_suggestion(
        case_id=case["case_id"],
        suggestion=forged,
        label=_label(4),
        allowed_evidence_refs=tuple(
            EvidenceRef.model_validate(item) for item in case["input"]["evidence_refs"]
        ),
    )
    assert denied.severe_violation is True
    assert denied.action_success is False


def test_live_ops_v3_dataset_is_versioned_and_mathematically_reachable() -> None:
    """修正版不得覆盖 v2，并应让 baseline 有误差而严格门仍可被完美候选达到。"""

    manifest_path = EVALUATION_ROOT / "manifests" / "phase13-live-ops-v3.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_id"] == "phase13-live-ops-v3"
    assert manifest["supersedes_dataset_manifest"] == "phase13-v2"
    labels = [
        json.loads(line)["label"]
        for line in (
            EVALUATION_ROOT / "labels" / "phase13-live-ops-v3" / "validation.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert len(labels) == 40
    assert all("acceptable_actions" in label for label in labels)
    assert all("incident_recovery_actions" in label for label in labels)
    cases, _labels = _split_records("validation")
    assert all(
        reference.get("anchor_id") == f"anchor-{case['case_id']}"
        for case in cases
        for reference in case["input"]["evidence_refs"]
    )


def test_validation_gate_rejects_mathematically_unreachable_candidate() -> None:
    """每 10 例检查剩余全对上界，不把规则失败误写成 INCONCLUSIVE。"""

    gate = LiveOpsValidationGate(
        baseline_action_successes=30,
        baseline_incident_recoveries=20,
    )
    first = tuple(
        LiveOpsCaseScore(
            case_id=f"validation-{index:02d}",
            action_success=index <= 8,
            incident_recovery=index <= 5,
            severe_violation=False,
        )
        for index in range(1, 11)
    )
    assert gate.record_shard(first).status is ValidationGateStatus.CONTINUE
    second = tuple(
        LiveOpsCaseScore(
            case_id=f"validation-{index:02d}",
            action_success=index <= 18,
            incident_recovery=index <= 10,
            severe_violation=False,
        )
        for index in range(11, 21)
    )
    decision = gate.record_shard(second)
    assert decision.status is ValidationGateStatus.REJECTED
    assert decision.reason_code == "QUALITY_THRESHOLD_UNREACHABLE"


def test_validation_gate_unlocks_holdout_only_after_four_complete_shards() -> None:
    """即使指标一直达标，也只能在 40 个 validation case 后解封一次 holdout。"""

    gate = LiveOpsValidationGate(
        baseline_action_successes=32,
        baseline_incident_recoveries=28,
    )
    for shard_index in range(4):
        shard = tuple(
            LiveOpsCaseScore(
                case_id=f"validation-{shard_index * 10 + offset:02d}",
                action_success=True,
                incident_recovery=True,
                severe_violation=False,
            )
            for offset in range(1, 11)
        )
        decision = gate.record_shard(shard)
        expected = (
            ValidationGateStatus.HOLDOUT_UNLOCKED
            if shard_index == 3
            else ValidationGateStatus.CONTINUE
        )
        assert decision.status is expected

    with pytest.raises(ValueError, match="terminal"):
        gate.record_shard(shard)


class _CapturingRunner:
    """记录 adapter 提交的任务，避免单元测试伪造模型或网络调用。"""

    def __init__(self, result: AgentResult) -> None:
        self.result = result
        self.tasks = []

    async def run(self, task):
        self.tasks.append(task)
        return self.result


class _EvidenceLoader:
    """为完整 Runner 集成返回 case 中已冻结的权威 Evidence 投影。"""

    def __init__(self, facts: dict[str, ResolvedEvidence]) -> None:
        self._facts = facts

    def load(self, evidence_id: str) -> ResolvedEvidence | None:
        return self._facts.get(evidence_id)


class _LiveOpsPricingPolicy:
    """脚本化集成使用固定小额价格，验证预算路径但不触发外部模型费用。"""

    policy_digest = HASH_A

    def count_input_tokens(self, _request) -> int:
        return 10

    def worst_case_cost(self, _request, _profile) -> Decimal:
        return Decimal("0.001")

    def actual_cost(self, _usage, _profile) -> Decimal:
        return Decimal("0.001")


def test_live_ops_agent_adapter_preserves_case_identity_and_never_falls_back() -> None:
    """adapter 只能委派冻结任务；运行失败必须原样返回而不能用 baseline 冒充成功。"""

    case = _case(3)
    profile = build_live_ops_profile(EVALUATION_ROOT)
    failed = AgentResult(
        task_id=f"live-ops:{case['case_id']}",
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        status=AgentResultStatus.MODEL_ERROR,
        failure={"code": "MODEL_PORT_ERROR"},
        summary="MODEL_PORT_ERROR",
    )
    runner = _CapturingRunner(failed)
    adapter = LiveOpsAgentAdapter(runner=runner, profile=profile)

    result = asyncio.run(adapter.run_case(case))

    assert result.status is AgentResultStatus.MODEL_ERROR
    assert len(runner.tasks) == 1
    task = runner.tasks[0]
    assert task.task_id == f"live-ops:{case['case_id']}"
    assert task.task_kind is SpecialistTaskKind.LIVE_OPS_ADVICE
    assert task.profile_id == profile.profile_id
    assert task.profile_version == profile.profile_version
    assert task.room_id == case["input"]["room_id"]
    assert task.trace_id == case["input"]["trace_id"]
    assert task.evaluation_case_id == case["case_id"]
    assert task.model_dump(mode="json")["input_snapshot"] == case["input"]


def test_live_ops_agent_adapter_revalidates_runner_output_as_strict_suggestion() -> None:
    """Runner 的 JSON 即使已通过通用 Schema，也必须再通过 LiveOps 领域枚举校验。"""

    case = _case(4)
    profile = build_live_ops_profile(EVALUATION_ROOT)
    invalid_success = AgentResult(
        task_id=f"live-ops:{case['case_id']}",
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        status=AgentResultStatus.SUCCEEDED,
        output={
            "action": "UNSAFE_WRITE",
            "reason_code": "FORGED",
            "suggestion": "do not use",
            "evidence_refs": case["input"]["evidence_refs"],
        },
        summary="Specialist completed with schema-valid output",
        model_calls=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        latency_ms=Decimal("1"),
        cost_cny=Decimal("0.001"),
    )
    adapter = LiveOpsAgentAdapter(runner=_CapturingRunner(invalid_success), profile=profile)

    with pytest.raises(ValueError, match="LiveOps"):
        asyncio.run(adapter.suggestion_for_case(case))


def test_live_ops_agent_adapter_rejects_case_for_other_candidate() -> None:
    """候选间不得借用相同 Runner/Profile；错误 case 身份必须在模型调用前停止。"""

    case = {**_case(1), "candidate": "planner"}
    profile = build_live_ops_profile(EVALUATION_ROOT)
    runner = _CapturingRunner(
        AgentResult(
            task_id="unused",
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            status=AgentResultStatus.MODEL_ERROR,
            failure={"code": "UNUSED"},
            summary="UNUSED",
        )
    )

    with pytest.raises(ValueError, match="candidate"):
        LiveOpsAgentAdapter(runner=runner, profile=profile).build_task(case)
    assert runner.tasks == []


def test_live_ops_recorder_rebuilds_validation_gate_from_selected_attempts() -> None:
    """进程重启后必须从 Store selected 结果重建四个 shard，不能相信丢失的内存 gate。"""

    store = InMemorySpecialistEvaluationStore()
    run, claim = _live_ops_run(store)
    cases, labels = _split_records("validation")
    policy = PriorityLiveOpsPolicy()
    recorder = LiveOpsPairedEvaluationRecorder(store=store)
    for case in cases:
        baseline = policy.decide(case["input"])
        guidance = case["input"]["verified_guidance"]
        agent = LiveOpsSuggestion(
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
            baseline=baseline,
            agent_result=AgentResult(
                task_id=f"live-ops:{case['case_id']}",
                profile_id="live-ops-agent",
                profile_version="1.0.0",
                status=AgentResultStatus.SUCCEEDED,
                output=agent.model_dump(mode="json"),
                summary="SCRIPTED",
                model_calls=1,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                latency_ms=Decimal("1"),
                cost_cny=Decimal("0.001"),
            ),
        )

    rebuilt = recorder.rebuild_validation_gate(run=run)

    assert rebuilt.status is ValidationGateStatus.HOLDOUT_UNLOCKED
    assert rebuilt.completed_cases == 40
    action_metric, recovery_metric = recorder.save_validation_metrics(run=run, claim=claim)
    assert action_metric.baseline_success_count == 32
    assert action_metric.agent_success_count == 40
    assert recovery_metric.baseline_success_count == 28
    assert recovery_metric.agent_success_count == 40
    assert len(store.list_attempts(run.run_id)) == 80
    assert all(
        item.subject is EvaluationSubject.BASELINE and item.input_tokens == 0
        for item in store.list_attempts(run.run_id)
        if item.subject is EvaluationSubject.BASELINE
    )


def test_live_ops_scripted_runner_evaluates_all_80_frozen_cases_without_network() -> None:
    """80 例 Agent/baseline 必须消费同一 v3 case；脚本模型满足严格门后才允许 holdout。"""

    profile = build_live_ops_profile(EVALUATION_ROOT)
    all_cases: list[dict] = []
    labels: dict[str, LiveOpsCaseLabel] = {}
    loaders: dict[object, _EvidenceLoader] = {}
    evidence_by_kind: dict[object, dict[str, ResolvedEvidence]] = {}
    for split in ("development", "validation", "holdout"):
        cases, split_labels = _split_records(split)
        all_cases.extend(cases)
        labels.update(split_labels)
        for case in cases:
            for raw_ref in case["input"]["evidence_refs"]:
                ref = EvidenceRef.model_validate(raw_ref)
                evidence_by_kind.setdefault(ref.kind, {})[ref.evidence_id] = ResolvedEvidence(
                    kind=ref.kind,
                    evidence_id=ref.evidence_id,
                    source_version=ref.source_version,
                    digest=ref.digest,
                    anchor_id=ref.anchor_id,
                    room_id=ref.room_id,
                    payload={"case_id": case["case_id"]},
                )
    for kind in EvidenceKind:
        loaders[kind] = _EvidenceLoader(evidence_by_kind.get(kind, {}))
    adapter_tasks = LiveOpsAgentAdapter(
        runner=_CapturingRunner(
            AgentResult(
                task_id="unused",
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                status=AgentResultStatus.MODEL_ERROR,
                failure={"code": "UNUSED"},
                summary="UNUSED",
            )
        ),
        profile=profile,
    )
    scripted_outcomes = {}
    for case in all_cases:
        task = adapter_tasks.build_task(case)
        guidance = case["input"]["verified_guidance"]
        suggestion = LiveOpsSuggestion(
            action=LiveOpsAction(guidance["recommended_action"]),
            reason_code="SCRIPTED_VERIFIED_GUIDANCE",
            suggestion="FOLLOW_VERIFIED_GUIDANCE",
            evidence_refs=tuple(EvidenceRef.model_validate(item) for item in case["input"]["evidence_refs"]),
        )
        output = {
            "kind": "FINAL",
            "final_output": suggestion.model_dump(mode="json"),
            "evidence_refs": suggestion.model_dump(mode="json")["evidence_refs"],
        }
        request_id = f"{task.task_id}:{task.task_digest}:model:1"
        scripted_outcomes[request_id] = (
            ModelSuccess(
                request_id=request_id,
                model_id=profile.model_id,
                output=output,
                usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                response_digest=canonical_json_sha256(output),
                latency_ms=Decimal("1"),
            ),
        )
    scripted_model = ScriptedAgentModel(outcomes=scripted_outcomes)
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=scripted_model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=EvidenceResolverRegistry(loaders),
        skill_port=object(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda task: f"anchor-{task.evaluation_case_id}",
        pricing_policy=_LiveOpsPricingPolicy(),
    )
    adapter = LiveOpsAgentAdapter(runner=runner, profile=profile)
    store = InMemorySpecialistEvaluationStore()
    run, claim = _live_ops_run(store)
    recorder = LiveOpsPairedEvaluationRecorder(store=store)
    baseline = PriorityLiveOpsPolicy()

    for case in all_cases:
        result = asyncio.run(adapter.run_case(case))
        recorder.record_pair(
            run=run,
            claim=claim,
            case=case,
            label=labels[case["case_id"]],
            baseline=baseline.decide(case["input"]),
            agent_result=result,
        )

    assert scripted_model.call_count == 80
    assert recorder.rebuild_validation_gate(run=run).status is ValidationGateStatus.HOLDOUT_UNLOCKED
    assert len(store.list_attempts(run.run_id)) == 160


def test_live_ops_recorder_rejects_case_already_selected_by_recovery() -> None:
    """恢复进程遇到已选正式结果必须停止，不能把同一 case 再计入 shard。"""

    store = InMemorySpecialistEvaluationStore()
    run, claim = _live_ops_run(store)
    case = _split_records("validation")[0][0]
    label = _split_records("validation")[1][case["case_id"]]
    suggestion = PriorityLiveOpsPolicy().decide(case["input"])
    result = AgentResult(
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
    )
    recorder = LiveOpsPairedEvaluationRecorder(store=store)
    recorder.record_pair(
        run=run,
        claim=claim,
        case=case,
        label=label,
        baseline=suggestion,
        agent_result=result,
    )

    with pytest.raises(ValueError, match="already selected"):
        recorder.record_pair(
            run=run,
            claim=claim,
            case=case,
            label=label,
            baseline=suggestion,
            agent_result=result,
        )


@pytest.mark.parametrize(
    "status",
    [AgentResultStatus.MODEL_ERROR, AgentResultStatus.BUDGET_EXCEEDED],
)
def test_live_ops_recorder_rejects_infrastructure_failure_before_store_write(status) -> None:
    """外部失败不能留下仅 baseline 被 selected 的半个正式 pair。"""

    store = InMemorySpecialistEvaluationStore()
    run, claim = _live_ops_run(store)
    case = _split_records("validation")[0][0]
    label = _split_records("validation")[1][case["case_id"]]
    failed = AgentResult(
        task_id=f"live-ops:{case['case_id']}",
        profile_id="live-ops-agent",
        profile_version="1.0.0",
        status=status,
        failure={"code": "EXTERNAL_EVIDENCE_UNAVAILABLE"},
        summary="EXTERNAL_EVIDENCE_UNAVAILABLE",
    )

    with pytest.raises(ValueError, match="infrastructure"):
        LiveOpsPairedEvaluationRecorder(store=store).record_pair(
            run=run,
            claim=claim,
            case=case,
            label=label,
            baseline=PriorityLiveOpsPolicy().decide(case["input"]),
            agent_result=failed,
        )

    assert store.list_attempts(run.run_id) == ()
