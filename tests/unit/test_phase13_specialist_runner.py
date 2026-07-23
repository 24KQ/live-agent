"""Phase 13 Task 4 Evidence Resolver 与 Bounded Runner 测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from src.specialist_runtime.budget import InMemoryModelBudgetStore
from src.specialist_runtime.evidence import (
    AuditStoreEvidenceLoader,
    EvidenceResolutionError,
    EvidenceResolverRegistry,
    EvaluationStoreEvidenceLoader,
    EventStoreEvidenceLoader,
    MemoryStoreEvidenceLoader,
    PlanNodeStoreEvidenceLoader,
    PlanStoreEvidenceLoader,
    ReplayStoreEvidenceLoader,
    ResolvedEvidence,
    SkillAttemptStoreEvidenceLoader,
)
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import (
    AgentResultStatus,
    AgentTask,
    EvidenceKind,
    EvidenceRef,
    SpecialistTaskKind,
    canonical_json_sha256,
)
from src.specialist_runtime.profiles import SpecialistProfile
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
from src.specialist_runtime.runner import (
    BoundedSpecialistRunner,
    ProductionSpecialistFacade,
    SkillPolicyDeniedError,
    SkillRuntimeInvocationError,
)
from src.skill_runtime.catalog import get_default_skill_catalog


HASH_A = "a" * 64
PROMPT_TEXT = "Return one governed action from resolved evidence."


def _profile(
    *,
    allowed_skills: tuple[str, ...] = (),
    max_model_calls: int = 2,
    max_skill_calls: int = 1,
    result_schema: dict | None = None,
    skill_versions: dict[str, str] | None = None,
    max_output_tokens: int | None = None,
) -> SpecialistProfile:
    schema = result_schema or {
        "type": "object",
        "properties": {"decision": {"type": "string"}},
        "required": ["decision"],
        "additionalProperties": False,
    }
    return SpecialistProfile(
        profile_id="live-ops",
        profile_version="1.0.0",
        task_kind=SpecialistTaskKind.LIVE_OPS_ADVICE,
        model_id="deepseek-v4-flash",
        endpoint_host="api.deepseek.com",
        temperature=Decimal("0"),
        prompt_text=PROMPT_TEXT,
        prompt_hash=hashlib.sha256(PROMPT_TEXT.encode("utf-8")).hexdigest(),
        result_schema_hash=canonical_json_sha256(schema),
        result_schema=schema,
        allowed_skill_ids=allowed_skills,
        skill_versions=(
            skill_versions
            if skill_versions is not None
            else {
                skill_id: next(
                    manifest.version
                    for manifest in get_default_skill_catalog()
                    if manifest.skill_id == skill_id
                )
                for skill_id in allowed_skills
            }
        ),
        max_model_calls=max_model_calls,
        max_skill_calls=max_skill_calls,
        max_total_tokens=100,
        max_output_tokens=max_output_tokens,
        deadline_seconds=5,
        max_case_cost_cny=Decimal("0.10"),
    )


def _evidence_ref(*, kind: EvidenceKind = EvidenceKind.EVENT, digest: str = HASH_A) -> EvidenceRef:
    return EvidenceRef(
        kind=kind,
        evidence_id=f"{kind.value.lower()}-001",
        source_version="1.0.0",
        digest=digest,
        anchor_id="anchor-001",
        room_id="room-001",
    )


def _task(*, evidence_refs: tuple[EvidenceRef, ...] | None = None) -> AgentTask:
    return AgentTask(
        task_id="task-001",
        task_kind=SpecialistTaskKind.LIVE_OPS_ADVICE,
        profile_id="live-ops",
        profile_version="1.0.0",
        room_id="room-001",
        trace_id="trace-001",
        objective="生成安全建议",
        input_snapshot={"alert": "sold_out"},
        initial_evidence_refs=evidence_refs or (_evidence_ref(),),
        evaluation_case_id="case-001",
    )


class _Loader:
    def __init__(self, facts: dict[str, ResolvedEvidence]) -> None:
        self._facts = facts

    def load(self, evidence_id: str) -> ResolvedEvidence | None:
        return self._facts.get(evidence_id)


class _ScriptedPort:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self._outputs = list(outputs)
        self.calls = 0
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        output = self._outputs[self.calls]
        self.calls += 1
        return ModelSuccess(
            request_id=request.request_id,
            model_id=request.model_id,
            output=output,
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            response_digest=canonical_json_sha256(output),
            latency_ms=Decimal("1"),
        )


class _MissingUsagePort(_ScriptedPort):
    async def complete(self, request):
        self.calls += 1
        return ModelSuccess(
            request_id=request.request_id,
            model_id=request.model_id,
            output={"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}},
            usage=None,
            response_digest=HASH_A,
            latency_ms=Decimal("1"),
        )


class _FailingPort(_ScriptedPort):
    async def complete(self, request):
        self.calls += 1
        raise RuntimeError("secret model failure")


class _InvalidOutcomePort(_ScriptedPort):
    async def complete(self, request):
        self.calls += 1
        return None


class _MismatchedIdentityPort(_ScriptedPort):
    async def complete(self, request):
        self.calls += 1
        return ModelSuccess(
            request_id="other-request",
            model_id=request.model_id,
            output={"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}},
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            response_digest=HASH_A,
            latency_ms=Decimal("1"),
        )


class _OutputTokenOverrunPort(_ScriptedPort):
    """模拟 Provider 忽略请求输出上限但仍返回可解析结构的越界回执。"""

    async def complete(self, request):
        """返回总 token 尚可通过、单次输出却超过冻结上限的成功响应。"""

        self.requests.append(request)
        self.calls += 1
        return ModelSuccess(
            request_id=request.request_id,
            model_id=request.model_id,
            output={"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}},
            usage=ModelUsage(input_tokens=10, output_tokens=18, total_tokens=28),
            response_digest=HASH_A,
            latency_ms=Decimal("1"),
        )


class _SkillPort:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.invocation_indexes: list[int] = []

    async def invoke(
        self,
        *,
        skill_id: str,
        skill_version: str,
        arguments: Any,
        task: AgentTask,
        deadline_at: datetime,
        invocation_index: int,
        execution_id: str,
    ) -> dict[str, Any]:
        self.calls.append(skill_id)
        self.invocation_indexes.append(invocation_index)
        return {"summary": "skill-ok", "version": skill_version}


class _PolicySkillPort(_SkillPort):
    async def invoke(self, **kwargs):
        raise SkillPolicyDeniedError("lifecycle")


class _RuntimeFailureSkillPort(_SkillPort):
    async def invoke(self, **kwargs):
        failure = SimpleNamespace(
            category=SimpleNamespace(value="SIDE_EFFECT_UNKNOWN"),
            side_effect_state=SimpleNamespace(value="UNKNOWN"),
        )
        result = SimpleNamespace(
            error_code=SimpleNamespace(value="HANDLER_FAILED"),
            attempt_id="attempt-001",
            failure=failure,
        )
        raise SkillRuntimeInvocationError(result)


class _PricingPolicy:
    """测试使用的冻结价格策略，最坏费用与实际费用来自同一身份。"""

    policy_digest = HASH_A

    def __init__(self, cost_cny: Decimal) -> None:
        self._cost_cny = cost_cny

    def worst_case_cost(self, _request, _profile: SpecialistProfile) -> Decimal:
        return self._cost_cny

    def actual_cost(self, _usage: ModelUsage, _profile: SpecialistProfile) -> Decimal:
        return self._cost_cny

    def count_input_tokens(self, _request) -> int:
        return 10


def _resolver_registry() -> EvidenceResolverRegistry:
    loaders = {}
    for kind in EvidenceKind:
        ref = _evidence_ref(kind=kind)
        loaders[kind] = _Loader(
            {
                ref.evidence_id: ResolvedEvidence(
                    kind=kind,
                    evidence_id=ref.evidence_id,
                    source_version=ref.source_version,
                    digest=ref.digest,
                    anchor_id=ref.anchor_id,
                    room_id=ref.room_id,
                    payload={"kind": kind.value},
                )
            }
        )
    return EvidenceResolverRegistry(loaders)


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_registry_resolves_all_authoritative_evidence_kinds(kind: EvidenceKind) -> None:
    """八类 EvidenceRef 都必须经对应权威 loader 严格解析。"""

    resolved = _resolver_registry().resolve(
        _evidence_ref(kind=kind),
        expected_room_id="room-001",
        expected_anchor_id="anchor-001",
    )
    assert resolved.kind is kind


@pytest.mark.parametrize("field", ["digest", "source_version", "room_id", "anchor_id"])
def test_registry_rejects_forged_or_cross_scope_evidence(field: str) -> None:
    """摘要、版本或作用域任一不一致都必须 fail-closed。"""

    ref = _evidence_ref()
    payload = ref.model_dump(mode="json")
    payload[field] = HASH_A.replace("a", "b") if field == "digest" else "wrong"
    forged = EvidenceRef.model_validate(payload)
    with pytest.raises(EvidenceResolutionError, match=field):
        _resolver_registry().resolve(
            forged,
            expected_room_id="room-001",
            expected_anchor_id="anchor-001",
        )


def test_registry_rejects_roomless_fact_for_room_scoped_task() -> None:
    """room 为空不能绕过非空任务作用域。"""

    ref = _evidence_ref()
    roomless = ResolvedEvidence(
        kind=ref.kind,
        evidence_id=ref.evidence_id,
        source_version=ref.source_version,
        digest=ref.digest,
        anchor_id=ref.anchor_id,
        room_id=None,
        payload={},
    )
    loaders = {kind: _Loader({}) for kind in EvidenceKind}
    loaders[EvidenceKind.EVENT] = _Loader({ref.evidence_id: roomless})
    with pytest.raises(EvidenceResolutionError, match="room_id"):
        EvidenceResolverRegistry(loaders).resolve(
            ref,
            expected_room_id="room-001",
            expected_anchor_id="anchor-001",
        )


@pytest.mark.parametrize(
    ("loader_class", "kind"),
    [
        (EventStoreEvidenceLoader, EvidenceKind.EVENT),
        (PlanStoreEvidenceLoader, EvidenceKind.PLAN),
        (PlanNodeStoreEvidenceLoader, EvidenceKind.PLAN_NODE),
        (SkillAttemptStoreEvidenceLoader, EvidenceKind.SKILL_ATTEMPT),
        (AuditStoreEvidenceLoader, EvidenceKind.AUDIT),
        (ReplayStoreEvidenceLoader, EvidenceKind.REPLAY),
        (MemoryStoreEvidenceLoader, EvidenceKind.MEMORY),
        (EvaluationStoreEvidenceLoader, EvidenceKind.EVALUATION),
    ],
)
def test_named_store_loaders_project_only_their_authoritative_kind(loader_class, kind) -> None:
    """八个 Store adapter 固定 EvidenceKind，不能由 projector 改写来源类型。"""

    ref = _evidence_ref(kind=kind)
    resolved = ResolvedEvidence(
        kind=kind,
        evidence_id=ref.evidence_id,
        source_version=ref.source_version,
        digest=ref.digest,
        anchor_id=ref.anchor_id,
        room_id=ref.room_id,
        payload={"store": kind.value},
    )
    loader = loader_class(getter=lambda _id: object(), projector=lambda _record: resolved)
    assert loader.load(ref.evidence_id) == resolved


def _runner(
    model_outputs: list[dict[str, Any]],
    *,
    allowed_skills: tuple[str, ...] = (),
    cost_cny: Decimal = Decimal("0.01"),
    max_output_tokens: int | None = None,
    model_port: Any | None = None,
):
    profile = _profile(
        allowed_skills=allowed_skills,
        max_output_tokens=max_output_tokens,
    )
    # 测试装配允许替换 Port 以模拟 Provider 回执边界；Runner 本身仍只依赖公开 Port 协议。
    model = model_port or _ScriptedPort(model_outputs)
    skill = _SkillPort()
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=skill,
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(cost_cny),
        clock=lambda: datetime.now(timezone.utc),
    )
    return runner, model, skill


def test_runner_returns_success_for_schema_valid_final_action() -> None:
    """Runner 只接受结构化 FINAL，并保守结算模型费用。"""

    runner, model, _skill = _runner(
        [{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}]
    )
    result = asyncio.run(runner.run(_task()))

    assert result.status is AgentResultStatus.SUCCEEDED
    assert result.output == {"decision": "NO_ACTION"}
    assert model.calls == 1
    assert model.requests[0].messages[0].content == PROMPT_TEXT


def test_runner_caps_request_output_tokens_at_frozen_profile_limit() -> None:
    """独立输出上限必须小于剩余总 token 时生效，不能只依赖总额度。"""

    runner, model, _skill = _runner(
        [{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}],
        max_output_tokens=17,
    )

    result = asyncio.run(runner.run(_task()))

    assert result.status is AgentResultStatus.SUCCEEDED
    assert model.requests[0].max_output_tokens == 17


def test_runner_rejects_provider_usage_that_exceeds_frozen_output_limit() -> None:
    """Provider 即使返回成功也不能越过请求中的独立输出上限。"""

    runner, overrun_port, _skill = _runner(
        [{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}],
        max_output_tokens=17,
        model_port=_OutputTokenOverrunPort([]),
    )

    result = asyncio.run(runner.run(_task()))

    assert result.status is AgentResultStatus.BUDGET_EXCEEDED
    assert result.failure is not None
    assert result.failure.code == "OUTPUT_TOKEN_LIMIT_EXCEEDED"
    assert overrun_port.requests[0].max_output_tokens == 17


def test_first_model_call_may_use_more_than_average_but_not_case_cap() -> None:
    """单次费用可高于平均切片，只要不超过尚未消费的 case 总上限。"""

    runner, _model, _skill = _runner(
        [{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}],
        cost_cny=Decimal("0.08"),
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.SUCCEEDED
    assert result.cost_cny == Decimal("0.08")


@pytest.mark.parametrize(
    ("output", "status"),
    [
        ({"kind": "FINAL", "final_output": {"unknown": True}}, AgentResultStatus.INVALID_OUTPUT),
        ({"kind": "CALL_SKILL", "skill_id": "forbidden", "arguments": {}}, AgentResultStatus.POLICY_DENIED),
        ({"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}, "chain_of_thought": "secret"}, AgentResultStatus.INVALID_OUTPUT),
    ],
)
def test_runner_fails_closed_for_schema_policy_or_reasoning_attack(output, status) -> None:
    """非法 Schema、越权 Skill 和思维链字段都不能进入成功结果。"""

    runner, _model, skill = _runner([output])
    result = asyncio.run(runner.run(_task()))
    assert result.status is status
    assert skill.calls == []


def test_runner_executes_whitelisted_skill_with_bounded_second_model_call() -> None:
    """白名单 Skill 只能调用一次，随后由第二次模型调用形成 FINAL。"""

    runner, model, skill = _runner(
        [
            {
                "kind": "CALL_SKILL",
                "skill_id": "generate_on_live_prompt",
                "arguments": {"room_id": "room-001", "sold_out_product_id": "p001"},
            },
            {"kind": "FINAL", "final_output": {"decision": "HUMAN_ATTENTION"}},
        ],
        allowed_skills=("generate_on_live_prompt",),
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.SUCCEEDED
    assert model.calls == 2
    assert skill.calls == ["generate_on_live_prompt"]


def test_runner_rejects_catalog_version_that_differs_from_frozen_profile() -> None:
    """白名单 ID 相同但 Catalog 版本漂移时，Runner 必须在 Skill Port 前拒绝。"""

    profile = _profile(
        allowed_skills=("generate_on_live_prompt",),
        skill_versions={"generate_on_live_prompt": "9.9.9"},
    )
    model = _ScriptedPort(
        [
            {
                "kind": "CALL_SKILL",
                "skill_id": "generate_on_live_prompt",
                "arguments": {"room_id": "room-001", "sold_out_product_id": "p001"},
            }
        ]
    )
    skill = _SkillPort()
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=skill,
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )

    result = asyncio.run(runner.run(_task()))

    assert result.status is AgentResultStatus.POLICY_DENIED
    assert result.failure is not None
    assert result.failure.code == "SKILL_VERSION_MISMATCH"
    assert skill.calls == []


def test_runner_rejects_missing_usage_and_model_exception_without_pending_budget() -> None:
    """正式模式 usage 缺失或模型异常都返回失败，并闭合当前 request reservation。"""

    for port in (_MissingUsagePort([]), _FailingPort([])):
        profile = _profile()
        budget = InMemoryModelBudgetStore()
        runner = BoundedSpecialistRunner(
            orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
            model_port=port,
            budget_store=budget,
            evidence_registry=_resolver_registry(),
            skill_port=_SkillPort(),
            skill_catalog=get_default_skill_catalog(),
            trusted_anchor_resolver=lambda _task: "anchor-001",
            pricing_policy=_PricingPolicy(Decimal("0.01")),
        )
        result = asyncio.run(runner.run(_task()))
        assert result.status is AgentResultStatus.MODEL_ERROR
        assert budget.list_pending_reservations() == ()


def test_runner_rejects_mismatched_model_response_identity() -> None:
    """Port 返回其他请求的结果时必须保守结算并拒绝消费其输出。"""

    profile = _profile()
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=_MismatchedIdentityPort([]),
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.MODEL_ERROR
    assert result.failure.code == "MODEL_IDENTITY_MISMATCH"


def test_runner_converts_invalid_model_outcome_to_closed_failure() -> None:
    """错误 Port 返回非协议对象时不得让属性异常逃出 Runner。"""

    profile = _profile()
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=_InvalidOutcomePort([]),
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.MODEL_ERROR
    assert result.failure.code == "INVALID_MODEL_OUTCOME"


def test_runner_validates_catalog_schema_before_skill_port() -> None:
    """白名单字符串不足以授权，畸形参数必须在 Skill Port 前拒绝。"""

    runner, _model, skill = _runner(
        [{"kind": "CALL_SKILL", "skill_id": "generate_on_live_prompt", "arguments": {}}],
        allowed_skills=("generate_on_live_prompt",),
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.INVALID_OUTPUT
    assert skill.calls == []


def test_runner_rejects_request_when_worst_case_price_exceeds_remaining_case_cap() -> None:
    """最坏费用无法装入剩余 case 预算时，模型请求必须在发送前被阻断。"""

    profile = _profile()
    model = _ScriptedPort([{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}])
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.11")),
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.BUDGET_EXCEEDED
    assert model.calls == 0


def test_runner_records_known_price_overrun_and_stops_case() -> None:
    """实际费用意外高于预留时必须按实际值记账并阻止 case 继续。"""

    class OverrunPolicy(_PricingPolicy):
        def worst_case_cost(self, _request, _profile):
            return Decimal("0.01")

        def actual_cost(self, _usage, _profile):
            return Decimal("0.02")

    profile = _profile()
    budget = InMemoryModelBudgetStore()
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=_ScriptedPort([{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}]),
        budget_store=budget,
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=OverrunPolicy(Decimal("0")),
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.BUDGET_EXCEEDED
    assert result.failure.code == "PRICE_RESERVATION_OVERRUN"
    assert result.cost_cny == Decimal("0.02")
    assert budget.snapshot().phase13_committed_cny == Decimal("0.02")


def test_known_cost_settlement_failure_requires_reconciliation_without_downgrade() -> None:
    """已知费用写失败时保留 pending 预留和实际费用证据，不能改写成较低未知结算。"""

    class FailingSettleStore(InMemoryModelBudgetStore):
        def settle(self, request_id, actual_cost_cny):
            raise RuntimeError("store unavailable")

    class KnownCostPolicy(_PricingPolicy):
        def actual_cost(self, _usage, _profile):
            return Decimal("0.02")

    profile = _profile()
    budget = FailingSettleStore()
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=_ScriptedPort([{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}]),
        budget_store=budget,
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=KnownCostPolicy(Decimal("0.01")),
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.BUDGET_EXCEEDED
    assert result.failure.code == "BUDGET_RECONCILIATION_REQUIRED"
    assert result.cost_cny == Decimal("0.02")
    assert len(budget.list_pending_reservations()) == 1


def test_model_cancellation_preserves_cancel_when_budget_cleanup_fails() -> None:
    """模型调用取消时，即使预算 Store 故障也必须传播原始取消并保留 pending。"""

    class SlowPort(_ScriptedPort):
        async def complete(self, request):
            self.calls += 1
            await asyncio.sleep(10)

    class FailingSettleStore(InMemoryModelBudgetStore):
        def settle(self, request_id, actual_cost_cny):
            raise RuntimeError("store unavailable")

    async def scenario():
        profile = _profile()
        budget = FailingSettleStore()
        runner = BoundedSpecialistRunner(
            orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
            model_port=SlowPort([]),
            budget_store=budget,
            evidence_registry=_resolver_registry(),
            skill_port=_SkillPort(),
            skill_catalog=get_default_skill_catalog(),
            trusted_anchor_resolver=lambda _task: "anchor-001",
            pricing_policy=_PricingPolicy(Decimal("0.01")),
        )
        running = asyncio.create_task(runner.run(_task()))
        await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return budget

    budget = asyncio.run(scenario())
    assert len(budget.list_pending_reservations()) == 1


def test_runner_rejects_non_finite_pricing_policy_value_before_model() -> None:
    """冻结价格策略返回 NaN 时必须形成预算失败，不能让 Decimal 异常逃逸。"""

    profile = _profile()
    model = _ScriptedPort([{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}])
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("NaN")),
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.BUDGET_EXCEEDED
    assert result.failure is not None
    assert result.failure.code == "PRICE_PREFLIGHT_FAILED"
    assert model.calls == 0


def test_runner_rejects_non_sha256_pricing_policy_digest_at_assembly() -> None:
    """价格表身份必须是小写 SHA-256，不能仅凭长度伪造冻结策略。"""

    profile = _profile()
    policy = _PricingPolicy(Decimal("0.01"))
    policy.policy_digest = "z" * 64
    with pytest.raises(ValueError, match="SHA-256"):
        BoundedSpecialistRunner(
            orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
            model_port=_ScriptedPort([]),
            budget_store=InMemoryModelBudgetStore(),
            evidence_registry=_resolver_registry(),
            skill_port=_SkillPort(),
            skill_catalog=get_default_skill_catalog(),
            trusted_anchor_resolver=lambda _task: "anchor-001",
            pricing_policy=policy,
        )


def test_runner_prices_complete_request_and_denies_duplicate_task_execution() -> None:
    """预留必须看到完整请求；同一冻结 Task 重跑不能生成第二个付费请求。"""

    class CapturingPolicy(_PricingPolicy):
        def __init__(self) -> None:
            super().__init__(Decimal("0.01"))
            self.requests = []

        def worst_case_cost(self, request, profile):
            self.requests.append(request)
            assert request.messages
            assert request.max_output_tokens > 0
            return super().worst_case_cost(request, profile)

    profile = _profile()
    model = _ScriptedPort(
        [
            {"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}},
            {"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}},
        ]
    )
    policy = CapturingPolicy()
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=policy,
    )
    assert asyncio.run(runner.run(_task())).status is AgentResultStatus.SUCCEEDED
    second = asyncio.run(runner.run(_task()))
    assert second.status is AgentResultStatus.BUDGET_EXCEEDED
    assert second.failure.code == "REQUEST_REPLAY_DENIED"
    assert len(policy.requests) == 2
    assert model.calls == 1


def test_runner_deducts_input_tokens_before_setting_output_limit() -> None:
    """总 Token 硬上限必须在发送前扣除可信输入计数。"""

    class HighInputPolicy(_PricingPolicy):
        def count_input_tokens(self, _request) -> int:
            return 90

    profile = _profile()
    model = _ScriptedPort([{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}])
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=HighInputPolicy(Decimal("0.01")),
    )
    assert asyncio.run(runner.run(_task())).status is AgentResultStatus.SUCCEEDED
    assert model.requests[0].max_output_tokens == 10


def test_runner_allows_isolated_formal_budget_and_uuid_request_adapters() -> None:
    """正式 smoke 可注入独立账本候选和 UUID 请求身份，历史默认路径不受此扩展影响。"""

    class _FormalBudgetAdapter:
        """用最小适配器观察 Runner 传入的候选和请求身份，不复用 Phase 13 预算账本。"""

        def __init__(self) -> None:
            self.reserved: list[tuple[str, object, Decimal]] = []
            self.settled: list[tuple[str, Decimal | None]] = []

        def reserve(self, request_id: str, candidate: object, amount_cny: Decimal):
            """模拟正式账本在请求离开进程前创建唯一发送意图。"""

            self.reserved.append((request_id, candidate, amount_cny))
            return SimpleNamespace(created=True)

        def settle(self, request_id: str, actual_cost_cny: Decimal | None):
            """记录 Runner 已完成的本地结算，不在测试中产生外部费用。"""

            self.settled.append((request_id, actual_cost_cny))
            return SimpleNamespace()

        def release(self, request_id: str):
            """保留 Runner deadline 失败时所需的公开预算接口。"""

            return SimpleNamespace(request_id=request_id)

    formal_budget = _FormalBudgetAdapter()
    runner, model, _skill = _runner(
        [{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}]
    )
    profile = _profile()
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=formal_budget,
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
        budget_candidate_resolver=lambda _task: "PHASE16_OFFICIAL_ANALYST",
        request_id_factory=lambda _task, _execution_id, _index: "00000000-0000-0000-0000-000000000001",
    )

    result = asyncio.run(runner.run(_task()))

    assert result.status is AgentResultStatus.SUCCEEDED
    assert model.requests[0].request_id == "00000000-0000-0000-0000-000000000001"
    assert formal_budget.reserved == [
        (
            "00000000-0000-0000-0000-000000000001",
            "PHASE16_OFFICIAL_ANALYST",
            Decimal("0.01"),
        )
    ]
    assert formal_budget.settled == [
        ("00000000-0000-0000-0000-000000000001", Decimal("0.01"))
    ]


def test_runner_requires_trusted_anchor_and_passes_resolved_evidence_to_model() -> None:
    """可信 anchor 缺失时 fail-closed；成功解析的冻结事实必须进入模型上下文。"""

    profile = _profile()
    denied_model = _ScriptedPort([])
    denied = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=denied_model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: None,
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )
    denied_result = asyncio.run(denied.run(_task()))
    assert denied_result.status is AgentResultStatus.POLICY_DENIED
    assert denied_result.failure.code == "ANCHOR_RESOLUTION_FAILED"
    assert denied_model.calls == 0

    runner, model, _skill = _runner(
        [{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}]
    )
    assert asyncio.run(runner.run(_task())).status is AgentResultStatus.SUCCEEDED
    context = json.loads(model.requests[0].messages[1].content)
    assert context["resolved_evidence"][0]["payload"] == {"kind": "EVENT"}


def test_runner_preserves_intermediate_evidence_latency_and_fallback_accounting() -> None:
    """中间证据、耗时和 fallback 前已发生的模型费用都必须保留在结果中。"""

    initial = _evidence_ref(kind=EvidenceKind.EVENT)
    intermediate = _evidence_ref(kind=EvidenceKind.AUDIT)
    profile = _profile(allowed_skills=("generate_on_live_prompt",))
    model = _ScriptedPort(
        [
            {
                "kind": "CALL_SKILL",
                "skill_id": "generate_on_live_prompt",
                "arguments": {"room_id": "room-001", "sold_out_product_id": "p001"},
                "evidence_refs": [intermediate.model_dump(mode="json")],
            },
            {"kind": "FINAL", "final_output": {"bad": True}},
        ]
    )
    ticks = iter(
        datetime(2026, 7, 15, tzinfo=timezone.utc) + timedelta(milliseconds=value)
        for value in (0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
    )
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
        clock=lambda: next(ticks),
    )
    facade = ProductionSpecialistFacade(
        runner=runner,
        retained_profiles={"live-ops@1.0.0"},
        baseline=lambda _task: {"decision": "NO_ACTION"},
    )
    result = asyncio.run(facade.run(_task(evidence_refs=(initial,))))
    assert result.status is AgentResultStatus.FALLBACK
    assert result.failure is not None
    assert result.failure.code == "RESULT_SCHEMA_INVALID"
    assert result.model_calls == 2
    assert result.skill_calls == 1
    assert result.cost_cny == Decimal("0.02")
    assert result.latency_ms > 0
    assert intermediate in result.evidence_refs


def test_runner_uses_distinct_skill_idempotency_indexes_for_repeated_skill() -> None:
    """同一任务重复调用同一 Skill 时，调用序号必须进入 Runtime 幂等身份。"""

    profile = _profile(
        allowed_skills=("generate_on_live_prompt",),
        max_model_calls=3,
        max_skill_calls=2,
    )
    model = _ScriptedPort(
        [
            {"kind": "CALL_SKILL", "skill_id": "generate_on_live_prompt", "arguments": {"room_id": "room-001", "sold_out_product_id": "p001"}},
            {"kind": "CALL_SKILL", "skill_id": "generate_on_live_prompt", "arguments": {"room_id": "room-001", "sold_out_product_id": "p002"}},
            {"kind": "FINAL", "final_output": {"decision": "HUMAN_ATTENTION"}},
        ]
    )
    skill = _SkillPort()
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=skill,
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )
    assert asyncio.run(runner.run(_task())).status is AgentResultStatus.SUCCEEDED
    assert skill.invocation_indexes == [1, 2]


@pytest.mark.parametrize(
    ("skill_port", "expected_code"),
    [
        (_PolicySkillPort(), "SKILL_POLICY_DENIED"),
        (_RuntimeFailureSkillPort(), "SKILL_RUNTIME_FAILED"),
    ],
)
def test_runner_preserves_skill_policy_or_runtime_failure_classification(
    skill_port, expected_code
) -> None:
    """Skill 策略拒绝和副作用未知不能被误报为普通模型错误。"""

    profile = _profile(allowed_skills=("generate_on_live_prompt",))
    model = _ScriptedPort(
        [
            {
                "kind": "CALL_SKILL",
                "skill_id": "generate_on_live_prompt",
                "arguments": {"room_id": "room-001", "sold_out_product_id": "p001"},
            }
        ]
    )
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=skill_port,
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.POLICY_DENIED
    assert result.failure is not None
    assert result.failure.code == expected_code
    assert result.skill_calls == 1
    if expected_code == "SKILL_RUNTIME_FAILED":
        assert result.failure.details["attempt_id"] == "attempt-001"
        assert result.failure.details["failure_category"] == "SIDE_EFFECT_UNKNOWN"


def test_runner_rechecks_deadline_after_budget_reserve_before_model_send() -> None:
    """预算行锁等待耗尽 deadline 时应 release reservation，模型调用数保持 0。"""

    current = [datetime.now(timezone.utc)]

    class AdvancingBudget(InMemoryModelBudgetStore):
        def reserve(self, *args, **kwargs):
            claim = super().reserve(*args, **kwargs)
            current[0] += timedelta(seconds=10)
            return claim

    profile = _profile()
    model = _ScriptedPort([{"kind": "FINAL", "final_output": {"decision": "NO_ACTION"}}])
    budget = AdvancingBudget()
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=budget,
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
        clock=lambda: current[0],
    )
    result = asyncio.run(runner.run(_task()))
    assert result.status is AgentResultStatus.MODEL_ERROR
    assert result.model_calls == 0
    assert model.calls == 0
    assert budget.list_pending_reservations() == ()


def test_runner_converts_action_evidence_store_exception_to_policy_failure() -> None:
    """模型动作引用证据时，Store/投影异常也必须形成 fail-closed AgentResult。"""

    initial_ref = _evidence_ref(kind=EvidenceKind.PLAN)
    action_ref = _evidence_ref(kind=EvidenceKind.EVENT)

    class FailingLoader:
        def load(self, _evidence_id):
            raise RuntimeError("database unavailable")

    registry = _resolver_registry()
    loaders = dict(registry._loaders)  # 仅测试装配新的权威失败源，不修改 Registry。
    loaders[EvidenceKind.EVENT] = FailingLoader()
    profile = _profile()
    model = _ScriptedPort(
        [
            {
                "kind": "FINAL",
                "final_output": {"decision": "NO_ACTION"},
                "evidence_refs": [action_ref.model_dump(mode="json")],
            }
        ]
    )
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=EvidenceResolverRegistry(loaders),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )
    result = asyncio.run(runner.run(_task(evidence_refs=(initial_ref,))))
    assert result.status is AgentResultStatus.POLICY_DENIED
    assert result.failure is not None
    assert result.failure.code == "EVIDENCE_STORE_ERROR"
    assert action_ref not in result.evidence_refs


def test_runner_rejects_final_output_evidence_that_differs_from_resolved_action_evidence() -> None:
    """最终结果不得伪造与已解析动作证据不同的 EvidenceRef。"""

    trusted_ref = _evidence_ref(kind=EvidenceKind.EVENT)
    forged_ref = _evidence_ref(kind=EvidenceKind.EVENT, digest="b" * 64)
    result_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "evidence_refs"],
        "properties": {
            "decision": {"type": "string"},
            "evidence_refs": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "evidence_id", "source_version", "digest", "anchor_id", "room_id"],
                    "properties": {
                        "kind": {"type": "string"},
                        "evidence_id": {"type": "string"},
                        "source_version": {"type": "string"},
                        "digest": {"type": "string"},
                        "anchor_id": {"type": "string"},
                        "room_id": {"type": "string"},
                    },
                },
            },
        },
    }
    profile = _profile(result_schema=result_schema)
    model = _ScriptedPort(
        [
            {
                "kind": "FINAL",
                "evidence_refs": [trusted_ref.model_dump(mode="json")],
                "final_output": {
                    "decision": "NO_ACTION",
                    "evidence_refs": [forged_ref.model_dump(mode="json")],
                },
            }
        ]
    )
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )

    result = asyncio.run(runner.run(_task()))

    assert result.status is AgentResultStatus.POLICY_DENIED
    assert result.failure is not None
    assert result.failure.code == "RESULT_EVIDENCE_MISMATCH"


def test_runner_rejects_nested_result_evidence_ids_outside_resolved_action_evidence() -> None:
    """ReviewMemory 嵌套 evidence_ids 只能引用本轮 FINAL 动作已解析的证据。"""

    event_ref = _evidence_ref(kind=EvidenceKind.EVENT)
    audit_ref = _evidence_ref(kind=EvidenceKind.AUDIT)
    result_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["attribution", "evidence_ids"],
        "properties": {
            "attribution": {
                "type": "object",
                "additionalProperties": False,
                "required": ["evidence_ids"],
                "properties": {
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    }
                },
            },
            "evidence_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        },
    }
    profile = _profile(result_schema=result_schema)
    model = _ScriptedPort(
        [
            {
                "kind": "FINAL",
                "evidence_refs": [
                    event_ref.model_dump(mode="json"),
                    audit_ref.model_dump(mode="json"),
                ],
                "final_output": {
                    "attribution": {"evidence_ids": [event_ref.evidence_id, "forged-evidence"]},
                    "evidence_ids": [event_ref.evidence_id, audit_ref.evidence_id],
                },
            }
        ]
    )
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )

    result = asyncio.run(runner.run(_task(evidence_refs=(event_ref, audit_ref))))

    assert result.status is AgentResultStatus.POLICY_DENIED
    assert result.failure is not None
    assert result.failure.code == "RESULT_EVIDENCE_MISMATCH"


@pytest.mark.parametrize("evidence_ids", [[], "forged-evidence"])
def test_runner_rejects_empty_or_malformed_declared_result_evidence_ids(evidence_ids) -> None:
    """结果一旦声明 evidence_ids，即使为空或畸形也不能绕过权威证据等价校验。"""

    event_ref = _evidence_ref(kind=EvidenceKind.EVENT)
    result_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["evidence_ids"],
        "properties": {"evidence_ids": {}},
    }
    profile = _profile(result_schema=result_schema)
    model = _ScriptedPort(
        [
            {
                "kind": "FINAL",
                "evidence_refs": [event_ref.model_dump(mode="json")],
                "final_output": {"evidence_ids": evidence_ids},
            }
        ]
    )
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )

    result = asyncio.run(runner.run(_task(evidence_refs=(event_ref,))))

    assert result.status is AgentResultStatus.POLICY_DENIED
    assert result.failure is not None
    assert result.failure.code == "RESULT_EVIDENCE_MISMATCH"


def test_runner_rejects_declared_empty_evidence_ids_even_without_action_evidence() -> None:
    """空 evidence_ids 本身就是非法声明，不能因 FINAL 动作也无证据而被接受。"""

    result_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["evidence_ids"],
        "properties": {"evidence_ids": {"type": "array"}},
    }
    profile = _profile(result_schema=result_schema)
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=_ScriptedPort(
            [{"kind": "FINAL", "final_output": {"evidence_ids": []}}]
        ),
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=_resolver_registry(),
        skill_port=_SkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_PricingPolicy(Decimal("0.01")),
    )

    result = asyncio.run(runner.run(_task()))

    assert result.status is AgentResultStatus.POLICY_DENIED
    assert result.failure is not None
    assert result.failure.code == "RESULT_EVIDENCE_MISMATCH"


def test_formal_runner_never_calls_baseline_but_production_facade_can_fallback_for_retained_profile() -> None:
    """正式评估不 fallback；生产门面仅对 RETAINED Profile 显式返回 FALLBACK。"""

    runner, _model, _skill = _runner([{"kind": "FINAL", "final_output": {"bad": True}}])
    formal = asyncio.run(runner.run(_task()))
    assert formal.status is AgentResultStatus.INVALID_OUTPUT

    production_runner, _production_model, _production_skill = _runner(
        [{"kind": "FINAL", "final_output": {"bad": True}}]
    )
    facade = ProductionSpecialistFacade(
        runner=production_runner,
        retained_profiles={"live-ops@1.0.0"},
        baseline=lambda _task: {"decision": "NO_ACTION"},
    )
    fallback = asyncio.run(facade.run(_task()))
    assert fallback.status is AgentResultStatus.FALLBACK
