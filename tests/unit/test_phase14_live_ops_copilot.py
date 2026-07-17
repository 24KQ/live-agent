"""Phase 14 Task 4 播中 Copilot 与结构化方案的 RED/GREEN 契约。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.decision_support.evidence import EvidenceBundleSnapshot
from src.decision_support.models import EvidenceBundle
from src.decision_support.live_ops_copilot import (
    LiveOpsDecisionSupport,
    build_live_ops_decision_support_profile,
)
from src.decision_support.proposal import (
    DecisionOption,
    DecisionTiming,
    LiveDecisionProposal,
    ProposalStatus,
    ProductStrategy,
)
from src.specialist_runtime.evidence import EvidenceResolverRegistry, ResolvedEvidence
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import (
    AgentFailure,
    AgentResult,
    AgentResultStatus,
    AgentTask,
    EvidenceKind,
    EvidenceRef,
    SpecialistTaskKind,
    canonical_json_sha256,
)
from src.specialist_runtime.budget import (
    BudgetCandidate,
    BudgetLimitExceeded,
    InMemoryModelBudgetStore,
)
from src.specialist_runtime.profiles import SpecialistProfile
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
from src.specialist_runtime.runner import BoundedSpecialistRunner, budget_candidate_for_task
from src.specialist_runtime.scripted_model import ScriptedAgentModel
from src.skill_runtime.catalog import get_default_skill_catalog
from tests.phase14_evidence_factory import NOW, build_evidence_bundle


def _bundle():
    """构造 Task 3 已验证的完整六角色快照，测试不自行伪造父事实。"""

    return build_evidence_bundle(
        live_session_id="live-session-p001-sold-out-v1",
        incident_id="incident-copilot-001",
        suffix="copilot-001",
        idempotency_key="evidence-copilot-001-idem",
    ).bundle


def _refs(bundle) -> tuple[EvidenceRef, ...]:
    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    return tuple(component.reference for component in snapshot.components)


def _ready_output(bundle) -> dict[str, Any]:
    refs = [ref.model_dump(mode="json") for ref in _refs(bundle)]
    return {
        "proposal_id": "proposal-copilot-001",
        "live_session_id": bundle.live_session_id,
        "incident_id": bundle.incident_id,
        "trace_id": EvidenceBundleSnapshot.model_validate(bundle.snapshot).scope.trace_id,
        "evidence_bundle_id": bundle.evidence_bundle_id,
        "status": "READY",
        "options": [
            {
                "option_id": "switch-to-backup",
                "product_strategy": "SWITCH_TO_BACKUP",
                "backup_product_id": "p002",
                "host_prompt": "请运营确认备品后再恢复讲解。",
                "timing": "AFTER_OPERATOR_CONFIRMATION",
                "risk_flags": ["INVENTORY_CONFLICT_REQUIRES_REVIEW"],
                "evidence_refs": refs,
            }
        ],
        "evidence_refs": refs,
    }


def _copilot(runner: _Runner, *, clock=None, profile=None) -> LiveOpsDecisionSupport:
    """测试固定在 Fixture 的可信时钟上，另由单独用例推进时钟验证过期门禁。"""

    return LiveOpsDecisionSupport(
        runner=runner,
        clock=clock or (lambda: NOW),
        profile=profile,
    )


def _bundle_with_eligibility(
    bundle: EvidenceBundle,
    *,
    proposal_eligible: bool,
    blocking_reasons: list[str],
) -> EvidenceBundle:
    """重建合法快照摘要，用于验证不可提案状态仍走正式模型校验路径。"""

    data = bundle.model_dump(mode="json")
    snapshot = dict(data["snapshot"])
    snapshot["proposal_eligible"] = proposal_eligible
    snapshot["blocking_reasons"] = blocking_reasons
    snapshot_without_digest = dict(snapshot)
    snapshot_without_digest.pop("bundle_digest", None)
    snapshot["bundle_digest"] = canonical_json_sha256(snapshot_without_digest)
    data["snapshot"] = snapshot
    data["input_fingerprint"] = canonical_json_sha256(snapshot)
    return EvidenceBundle.model_validate(data)


class _Runner:
    """只暴露 Runner.run 的测试端口，验证 Copilot 不创建第二套执行循环。"""

    def __init__(self, output: Any = None, *, status=AgentResultStatus.SUCCEEDED) -> None:
        self.output = output
        self.status = status
        self.task: AgentTask | None = None

    async def run(self, task: AgentTask) -> AgentResult:
        self.task = task
        return AgentResult(
            task_id=task.task_id,
            profile_id=task.profile_id,
            profile_version=task.profile_version,
            status=self.status,
            output=self.output,
            failure=(
                AgentFailure(code="MODEL_ERROR")
                if self.status is not AgentResultStatus.SUCCEEDED
                else None
            ),
            evidence_refs=task.initial_evidence_refs,
            summary="scripted task result",
        )


class _ResolvedEvidenceLoader:
    """为共享 Runner 提供只读、按 EvidenceRef 身份索引的测试投影。"""

    def __init__(self, facts: dict[str, ResolvedEvidence]) -> None:
        self._facts = facts

    def load(self, evidence_id: str) -> ResolvedEvidence | None:
        return self._facts.get(evidence_id)


class _CopilotPricing:
    """固定小额脚本价格，覆盖预算路径但不产生任何真实模型费用。"""

    policy_digest = "a" * 64

    def count_input_tokens(self, _request) -> int:
        return 10

    def worst_case_cost(self, _request, _profile) -> Decimal:
        return Decimal("0.001000")

    def actual_cost(self, _usage, _profile) -> Decimal:
        return Decimal("0.001000")


class _NoSkillPort:
    """正常 Copilot 方案不应执行 Skill；若调用则测试直接失败。"""

    async def invoke(self, **_kwargs):
        raise AssertionError("Copilot ScriptedModel case unexpectedly invoked a Skill")


def _copilot_task(bundle: EvidenceBundle, profile: SpecialistProfile) -> AgentTask:
    """复制生产 Copilot 的冻结 Task 组装，生成 ScriptedModel 的稳定 request_id。"""

    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    return AgentTask(
        task_id=f"live-ops-decision-support:{bundle.evidence_bundle_id}",
        task_kind=SpecialistTaskKind.LIVE_OPS_ADVICE,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        room_id=snapshot.scope.room_id,
        trace_id=snapshot.scope.trace_id,
        objective="Generate structured live-commerce options for human operator review.",
        input_snapshot={"evidence_bundle": bundle.model_dump(mode="json")["snapshot"]},
        initial_evidence_refs=_refs(bundle),
    )


def _build_bounded_copilot(
    bundle: EvidenceBundle,
    output: dict[str, Any],
    *,
    model_action: dict[str, Any] | None = None,
) -> tuple[LiveOpsDecisionSupport, ScriptedAgentModel, AgentTask, BoundedSpecialistRunner]:
    """装配真实 BoundedSpecialistRunner，禁止测试通过第二套模型循环旁路。"""

    profile = build_live_ops_decision_support_profile()
    task = _copilot_task(bundle, profile)
    facts: dict[Any, dict[str, ResolvedEvidence]] = {kind: {} for kind in EvidenceKind}
    for reference in _refs(bundle):
        facts[reference.kind][reference.evidence_id] = ResolvedEvidence(
            kind=reference.kind,
            evidence_id=reference.evidence_id,
            source_version=reference.source_version,
            digest=reference.digest,
            anchor_id=reference.anchor_id,
            room_id=reference.room_id,
            payload={"fixture": "phase14-copilot"},
        )
    outcome = model_action or {
        "kind": "FINAL",
        "final_output": output,
        "evidence_refs": [reference.model_dump(mode="json") for reference in _refs(bundle)],
    }
    request_id = f"{task.task_id}:{task.task_digest}:model:1"
    model = ScriptedAgentModel(
        outcomes={
            request_id: (
                ModelSuccess(
                    request_id=request_id,
                    model_id=profile.model_id,
                    output=outcome,
                    usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                    response_digest=canonical_json_sha256(outcome),
                    latency_ms=Decimal("1"),
                ),
            )
        }
    )
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=EvidenceResolverRegistry(
            {
                kind: _ResolvedEvidenceLoader(facts[kind])
                for kind in EvidenceKind
            }
        ),
        skill_port=_NoSkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda current_task: EvidenceBundleSnapshot.model_validate(
            bundle.snapshot
        ).scope.anchor_id,
        pricing_policy=_CopilotPricing(),
        clock=lambda: NOW,
    )
    return LiveOpsDecisionSupport(runner=runner, clock=lambda: NOW), model, task, runner


def test_live_ops_decision_support_profile_is_exactly_frozen() -> None:
    """新的人机协同 Profile 不复活 Phase 13 自主候选，且资源门限固定。"""

    profile = build_live_ops_decision_support_profile()

    assert profile.profile_id == "live_ops_decision_support"
    assert profile.profile_version == "1.0.0"
    assert profile.task_kind is SpecialistTaskKind.LIVE_OPS_ADVICE
    assert profile.max_model_calls == 2
    assert profile.max_skill_calls == 3
    assert profile.max_total_tokens == 4000
    assert profile.deadline_seconds == 5


def test_decision_option_is_closed_and_requires_evidence() -> None:
    """方案选项只能表达受限经营建议，不能携带工具、SQL 或自由动作字段。"""

    ref = EvidenceRef(
        kind="AUDIT",
        evidence_id="evidence-copilot-001",
        source_version="1.0.0",
        digest="a" * 64,
        room_id="room-copilot",
        anchor_id="anchor-copilot",
    )
    option = DecisionOption(
        option_id="hold-for-review",
        product_strategy=ProductStrategy.KEEP_CURRENT,
        backup_product_id=None,
        host_prompt="请运营确认当前库存事实。",
        timing=DecisionTiming.AFTER_OPERATOR_CONFIRMATION,
        risk_flags=("RECONCILIATION_REQUIRED",),
        evidence_refs=(ref,),
    )

    assert option.evidence_refs == (ref,)
    with pytest.raises(ValueError, match="extra|tool_calls"):
        DecisionOption.model_validate(
            {**option.model_dump(mode="json"), "tool_calls": ["set_price"]}
        )


def test_decision_option_rejects_risk_code_outside_frozen_whitelist() -> None:
    """风险码必须来自固定业务白名单，不能借任意大写字符串注入新语义。"""

    ref = EvidenceRef(
        kind="AUDIT",
        evidence_id="evidence-risk-code",
        source_version="1.0.0",
        digest="a" * 64,
        room_id="room-copilot",
        anchor_id="anchor-copilot",
    )
    with pytest.raises(ValueError, match="risk_flags"):
        DecisionOption(
            option_id="unknown-risk",
            product_strategy=ProductStrategy.KEEP_CURRENT,
            host_prompt="请运营确认当前库存事实。",
            timing=DecisionTiming.AFTER_OPERATOR_CONFIRMATION,
            risk_flags=("ARBITRARY_MODEL_FLAG",),
            evidence_refs=(ref,),
        )


def test_copilot_maps_successful_runner_result_to_structured_proposal() -> None:
    """成功模型结果必须保留任务、会话和完整 EvidenceRef 身份。"""

    bundle = _bundle()
    runner = _Runner(_ready_output(bundle))
    copilot = _copilot(runner)

    proposal = asyncio.run(copilot.propose(bundle))

    assert proposal.status is ProposalStatus.READY
    assert len(proposal.options) == 1
    assert proposal.evidence_bundle_id == bundle.evidence_bundle_id
    assert runner.task is not None
    assert runner.task.task_kind is SpecialistTaskKind.LIVE_OPS_ADVICE
    assert runner.task.initial_evidence_refs == _refs(bundle)


def test_model_failure_returns_degraded_deterministic_fact_summary() -> None:
    """模型失败只能降级展示事实摘要，不得伪装为已生成经营方案。"""

    bundle = _bundle()
    runner = _Runner(status=AgentResultStatus.MODEL_ERROR)
    copilot = _copilot(runner)

    proposal = asyncio.run(copilot.propose(bundle))

    assert proposal.status is ProposalStatus.DEGRADED
    assert proposal.options == ()
    assert proposal.degraded_reason == "MODEL_ERROR"
    assert proposal.fact_summary


def test_copilot_rejects_unknown_evidence_and_free_tool_fields() -> None:
    """模型输出的未知证据或自由工具字段必须降级，不能进入工作台方案。"""

    bundle = _bundle()
    output = _ready_output(bundle)
    output["options"][0]["evidence_refs"] = [
        {
            **output["options"][0]["evidence_refs"][0],
            "evidence_id": "forged-evidence",
        }
    ]
    output["tool_calls"] = [{"name": "set_price"}]
    proposal = asyncio.run(_copilot(_Runner(output)).propose(bundle))

    assert proposal.status is ProposalStatus.DEGRADED
    assert proposal.degraded_reason in {"INVALID_OUTPUT", "EVIDENCE_MISMATCH"}


def test_copilot_requires_each_option_to_close_over_all_bundle_evidence() -> None:
    """单个 option 不能只引用 Bundle 子集，避免遗漏关键冲突事实。"""

    bundle = _bundle()
    output = _ready_output(bundle)
    output["options"][0]["evidence_refs"] = output["options"][0]["evidence_refs"][:-1]
    proposal = asyncio.run(_copilot(_Runner(output)).propose(bundle))

    assert proposal.status is ProposalStatus.DEGRADED
    assert proposal.degraded_reason == "INVALID_OUTPUT"


def test_copilot_rejects_profile_with_forged_digest() -> None:
    """Copilot 必须绑定完整 Profile 摘要，不能只核对 ID、版本和任务类型。"""

    profile = build_live_ops_decision_support_profile()
    # StrictFrozenModel 禁止带 update 的 model_copy；这里显式构造一个带旧摘要的
    # 伪造对象，模拟进程边界收到不可信配置时的最小攻击输入。
    forged_data = dict(profile.__dict__)
    forged_data["max_skill_calls"] = 99
    forged = SpecialistProfile.model_construct(**forged_data)

    with pytest.raises(ValueError, match="profile"):
        LiveOpsDecisionSupport(runner=_Runner(), profile=forged)


def test_ineligible_bundle_degrades_before_runner() -> None:
    """证据明确不可提案时不得启动模型或任何 Skill。"""

    bundle = _bundle_with_eligibility(
        _bundle(),
        proposal_eligible=False,
        blocking_reasons=["WAITING_RECONCILIATION"],
    )
    runner = _Runner(_ready_output(bundle))

    proposal = asyncio.run(_copilot(runner).propose(bundle))

    assert proposal.status is ProposalStatus.DEGRADED
    assert proposal.degraded_reason == "PROPOSAL_INELIGIBLE"
    assert runner.task is None


def test_expired_bundle_degrades_before_runner() -> None:
    """Bundle 到期后即使 Snapshot 结构完整，也不得再次调用模型。"""

    bundle = _bundle()
    runner = _Runner(_ready_output(bundle))

    proposal = asyncio.run(
        _copilot(
            runner,
            clock=lambda: NOW + timedelta(seconds=10),
        ).propose(bundle)
    )

    assert proposal.status is ProposalStatus.DEGRADED
    assert proposal.degraded_reason == "EVIDENCE_EXPIRED"
    assert runner.task is None


def test_copilot_rejects_backup_outside_frozen_available_inventory() -> None:
    """备品 ID 必须与已验证库存快照中的可用备品精确匹配。"""

    bundle = _bundle()
    output = _ready_output(bundle)
    output["options"][0]["backup_product_id"] = "p999"

    proposal = asyncio.run(_copilot(_Runner(output)).propose(bundle))

    assert proposal.status is ProposalStatus.DEGRADED
    assert proposal.degraded_reason == "BACKUP_PRODUCT_MISMATCH"


def test_copilot_uses_shared_bounded_runner_and_scripted_model_without_network() -> None:
    """完整 Copilot 链路必须经过共享 Runner，ScriptedModel 只消费一次模型请求。"""

    bundle = _bundle()
    copilot, model, task, _runner = _build_bounded_copilot(bundle, _ready_output(bundle))

    proposal = asyncio.run(copilot.propose(bundle))

    assert proposal.status is ProposalStatus.READY
    assert model.call_count == 1
    assert task.task_id == f"live-ops-decision-support:{bundle.evidence_bundle_id}"


def test_shared_runner_rejects_write_skill_from_copilot_model() -> None:
    """Copilot 模型即使请求改价，也只能在共享 Runner 的白名单门禁处失败。"""

    bundle = _bundle()
    _copilot_facade, model, task, runner = _build_bounded_copilot(
        bundle,
        _ready_output(bundle),
        model_action={
            "kind": "CALL_SKILL",
            "skill_id": "set_product_price",
            "arguments": {"product_id": "p001", "price": "1.00", "expected_version": 2},
        },
    )

    # 直接访问被测试的共享 Runner 只用于观察封闭失败；没有任何 fallback 或 Skill Port 调用。
    result = asyncio.run(runner.run(task))

    assert result.status is AgentResultStatus.POLICY_DENIED
    assert result.failure is not None
    assert result.failure.code == "SKILL_NOT_ALLOWED"
    assert model.call_count == 1


def test_phase14_copilot_has_independent_persistent_budget_identity() -> None:
    """新 Copilot 只能消费 Phase 14 额度，不能借用 Phase 13 LIVE_OPS。"""

    store = InMemoryModelBudgetStore()
    store.reserve(
        "phase14-copilot-001",
        BudgetCandidate.PHASE14_COPILOT,
        Decimal("1.00"),
    )
    with pytest.raises(BudgetLimitExceeded):
        store.reserve(
            "phase14-copilot-002",
            BudgetCandidate.PHASE14_COPILOT,
            Decimal("0.01"),
        )
    assert store.snapshot().total_limit_cny == Decimal("4.00")
    assert store.snapshot().phase14_reserved_cny == Decimal("1.00")


def test_runner_routes_new_profile_to_phase14_budget_candidate() -> None:
    """同一 LIVE_OPS_ADVICE task kind 通过精确 Profile 身份区分两个阶段预算。"""

    task = AgentTask(
        task_id="task-phase14-budget",
        task_kind=SpecialistTaskKind.LIVE_OPS_ADVICE,
        profile_id="live_ops_decision_support",
        profile_version="1.0.0",
        room_id="room-phase14",
        trace_id="trace-phase14",
        objective="advice",
        input_snapshot={"evidence": "frozen"},
    )

    assert budget_candidate_for_task(task) is BudgetCandidate.PHASE14_COPILOT
