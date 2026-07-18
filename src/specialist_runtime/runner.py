"""Phase 13 共享受限 Specialist Runner 与生产建议门面。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import SchemaError as JsonSchemaError
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema.validators import Draft202012Validator
from pydantic import ValidationError

from src.specialist_runtime.budget import (
    BudgetCandidate,
    BudgetInvariantError,
    BudgetLimitExceeded,
    InMemoryModelBudgetStore,
)
from src.specialist_runtime.evidence import EvidenceResolutionError, EvidenceResolverRegistry
from src.specialist_runtime.model_port import (
    AgentModelPort,
    ModelFailure,
    ModelMessage,
    ModelRequest,
    ModelSuccess,
    ModelUsage,
)
from src.specialist_runtime.models import (
    AgentAction,
    AgentActionKind,
    AgentFailure,
    AgentResult,
    AgentResultStatus,
    AgentTask,
    EvidenceRef,
    SpecialistTaskKind,
    _plain_json,
)
from src.specialist_runtime.profiles import SpecialistProfile
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileResolutionError
from src.skill_runtime.models import SkillManifest
from src.skill_runtime.models import (
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
    SkillExecutionStatus,
    SkillErrorCode,
)
from src.state.models import LifecycleStage


class SpecialistSkillPort(Protocol):
    """Runner 调用受治理 Skill Runtime 的最小 async 入口。"""

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
        """执行一次白名单 Skill，不在 Port 内 fallback。"""


class ModelPricingPolicy(Protocol):
    """同一冻结价格表同时计算请求最坏费用和 usage 实际费用。"""

    policy_digest: str

    def worst_case_cost(self, request: ModelRequest, profile: SpecialistProfile) -> Decimal: ...

    def actual_cost(self, usage: ModelUsage, profile: SpecialistProfile) -> Decimal: ...

    def count_input_tokens(self, request: ModelRequest) -> int: ...


class SkillPolicyDeniedError(RuntimeError):
    """Skill Runtime 在 Handler 前按版本、生命周期、Schema 或门禁拒绝。"""


def _collect_result_evidence_ids(value: Any) -> tuple[set[str], bool, bool]:
    """递归提取 evidence_ids，并返回字段存在、重复或畸形等独立事实。"""

    collected: set[str] = set()
    invalid = False
    found = False
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "evidence_ids":
                found = True
                if not isinstance(item, (list, tuple)) or any(
                    not isinstance(evidence_id, str) or not evidence_id for evidence_id in item
                ):
                    invalid = True
                    continue
                if not item:
                    invalid = True
                if len(item) != len(set(item)):
                    invalid = True
                collected.update(item)
                continue
            nested, nested_invalid, nested_found = _collect_result_evidence_ids(item)
            collected.update(nested)
            invalid = invalid or nested_invalid
            found = found or nested_found
    elif isinstance(value, (list, tuple)):
        for item in value:
            nested, nested_invalid, nested_found = _collect_result_evidence_ids(item)
            collected.update(nested)
            invalid = invalid or nested_invalid
            found = found or nested_found
    return collected, invalid, found


class SkillRuntimeInvocationError(RuntimeError):
    """保留 SkillExecutionResult 的稳定失败事实，不携带原始异常。"""

    def __init__(self, result: Any) -> None:
        super().__init__("Skill Runtime execution failed")
        self.result = result


class RuntimeSkillPort:
    """把 Runner 的白名单调用适配到统一异步 SkillExecutor。"""

    _LIFECYCLES = {
        SpecialistTaskKind.LIVE_OPS_ADVICE: LifecycleStage.ON_LIVE,
        SpecialistTaskKind.PLAN_PROPOSAL: LifecycleStage.PRE_LIVE,
        SpecialistTaskKind.POST_LIVE_REVIEW: LifecycleStage.POST_LIVE,
    }

    def __init__(self, executor: Any) -> None:
        self._executor = executor

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
        call = SkillCall(
            skill_id=skill_id,
            version=skill_version,
            context=SkillExecutionContext(
                room_id=task.room_id,
                trace_id=task.trace_id,
                lifecycle=self._LIFECYCLES[task.task_kind],
                execution_route=SkillExecutionRoute.SKILL_RUNTIME,
                # 同一执行允许重复调用同一 Skill；执行身份与调用序号共同避免不同参数
                # 被误判成同一次幂等重放，也防止同一 AgentTask 的独立重跑复用旧 Attempt。
                idempotency_key=f"specialist:{execution_id}:{task.task_id}:{skill_id}:{invocation_index}",
                deadline_at=deadline_at,
            ),
            arguments=_plain_json(arguments),
        )
        result = await self._executor.execute(call)
        if result.status is not SkillExecutionStatus.SUCCESS:
            if result.error_code in {
                SkillErrorCode.SKILL_NOT_FOUND,
                SkillErrorCode.VERSION_MISMATCH,
                SkillErrorCode.LIFECYCLE_MISMATCH,
                SkillErrorCode.INVALID_ARGUMENTS,
                SkillErrorCode.IDEMPOTENCY_REQUIRED,
                SkillErrorCode.APPROVAL_REQUIRED,
                SkillErrorCode.APPROVAL_REJECTED,
            }:
                raise SkillPolicyDeniedError(str(result.error_code))
            raise SkillRuntimeInvocationError(result)
        return {} if result.output is None else _plain_json(result.output)


_CANDIDATES: Mapping[SpecialistTaskKind, BudgetCandidate] = MappingProxyType(
    {
        SpecialistTaskKind.LIVE_OPS_ADVICE: BudgetCandidate.LIVE_OPS,
        SpecialistTaskKind.PLAN_PROPOSAL: BudgetCandidate.PLANNER,
        SpecialistTaskKind.POST_LIVE_REVIEW: BudgetCandidate.REVIEW_MEMORY,
    }
)


def budget_candidate_for_task(task: AgentTask) -> BudgetCandidate:
    """按精确 Profile 身份解析预算，避免同 task kind 的跨阶段额度串用。"""

    if task.task_kind in {
        SpecialistTaskKind.CONFLICT_ANALYSIS,
        SpecialistTaskKind.LIVE_DECISION_PLANNING,
    }:
        # Phase 16 不能借用 Phase 13 候选或 Phase 14 Copilot 预算；Task 10 的专用账本
        # 完成前，让共享 Runner 返回可审计的预算拒绝而不是抛出未捕获 KeyError。
        raise BudgetInvariantError("Phase 16 task requires dedicated Phase 16 budget")
    if task.profile_id == "live_ops_decision_support" and task.profile_version == "1.0.0":
        return BudgetCandidate.PHASE14_COPILOT
    return _CANDIDATES[task.task_kind]


@dataclass
class _RunAudit:
    """Runner 内部累计的调用与费用事实，失败结果同样必须带出。"""

    model_calls: int = 0
    skill_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cny: Decimal = Decimal("0")
    started_at: datetime | None = None
    actions: list[AgentAction] = field(default_factory=list)
    evidence_refs: list[Any] = field(default_factory=list)


class BoundedSpecialistRunner:
    """按冻结 Profile 执行有限模型/Skill 循环，正式路径永不调用 baseline。"""

    def __init__(
        self,
        *,
        orchestrator: SpecialistOrchestrator,
        model_port: AgentModelPort,
        budget_store: InMemoryModelBudgetStore,
        evidence_registry: EvidenceResolverRegistry,
        skill_port: SpecialistSkillPort,
        skill_catalog: tuple[SkillManifest, ...] | list[SkillManifest] | Any,
        trusted_anchor_resolver: Callable[[AgentTask], str | None],
        pricing_policy: ModelPricingPolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._model_port = model_port
        self._budget_store = budget_store
        self._evidence_registry = evidence_registry
        self._skill_port = skill_port
        self._skill_catalog = {manifest.skill_id: manifest for manifest in skill_catalog}
        self._trusted_anchor_resolver = trusted_anchor_resolver
        # 价格表摘要是正式评估复现与预算结算共同依赖的冻结身份；仅校验长度会让
        # 任意 64 字符字符串冒充 SHA-256，进而削弱“同一价格策略”这一审计事实。
        if re.fullmatch(r"[0-9a-f]{64}", pricing_policy.policy_digest) is None:
            raise ValueError("pricing policy digest must be SHA-256")
        self._pricing_policy = pricing_policy
        self._pricing_policy_digest = pricing_policy.policy_digest
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(self, task: AgentTask) -> AgentResult:
        """执行单个任务；所有可预期失败都转换为封闭 AgentResult。"""
        started_at = self._clock()
        audit = _RunAudit(started_at=started_at)
        try:
            profile = self._orchestrator.resolve_profile(task)
        except SpecialistProfileResolutionError:
            return self._failure(task, AgentResultStatus.POLICY_DENIED, "PROFILE_DENIED", audit)
        deadline_at = self._clock() + timedelta(seconds=profile.deadline_seconds)
        # task_digest 绑定全部冻结任务事实；同一 Task 的重放命中同一预算请求并在发送前
        # 被拒绝，防止崩溃重试默默产生第二笔模型费用。Task 5 的 Evaluation Attempt
        # 会为确需独立执行的 case 创建不同冻结任务身份。
        execution_id = task.task_digest
        actions = audit.actions
        skill_outputs: list[dict[str, Any]] = []
        resolved_evidence: list[Any] = []
        try:
            trusted_anchor_id = self._trusted_anchor_resolver(task)
            if trusted_anchor_id is None:
                raise ValueError("trusted anchor is unavailable")
        except Exception:
            return self._failure(task, AgentResultStatus.POLICY_DENIED, "ANCHOR_RESOLUTION_FAILED", audit)
        try:
            resolved_evidence.extend(
                self._evidence_registry.resolve_many(
                    task.initial_evidence_refs,
                    expected_room_id=task.room_id,
                    expected_anchor_id=trusted_anchor_id,
                )
            )
            audit.evidence_refs.extend(task.initial_evidence_refs)
        except EvidenceResolutionError:
            return self._failure(task, AgentResultStatus.POLICY_DENIED, "EVIDENCE_DENIED", audit)
        except Exception:
            return self._failure(task, AgentResultStatus.POLICY_DENIED, "EVIDENCE_STORE_ERROR", audit)

        for model_index in range(profile.max_model_calls):
            remaining_seconds = (deadline_at - self._clock()).total_seconds()
            remaining_tokens = profile.max_total_tokens - audit.input_tokens - audit.output_tokens
            if remaining_seconds <= 0:
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "DEADLINE_EXCEEDED", audit)
            if remaining_tokens <= 0:
                return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "TOKEN_BUDGET_EXCEEDED", audit)
            request = self._model_request(
                task, profile, deadline_at, execution_id, model_index, actions,
                skill_outputs, resolved_evidence, remaining_tokens
            )
            try:
                input_tokens = self._pricing_policy.count_input_tokens(request)
                if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
                    raise BudgetInvariantError("token counter returned invalid input count")
            except Exception:
                return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "TOKEN_PREFLIGHT_FAILED", audit)
            max_output_tokens = remaining_tokens - input_tokens
            if max_output_tokens <= 0:
                return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "TOKEN_BUDGET_EXCEEDED", audit)
            request = self._model_request(
                task, profile, deadline_at, execution_id, model_index, actions,
                skill_outputs, resolved_evidence, max_output_tokens
            )
            remaining_case_cost = profile.max_case_cost_cny - audit.cost_cny
            try:
                self._assert_pricing_policy_frozen()
                per_call_reservation = self._validated_cost(
                    self._pricing_policy.worst_case_cost(request, profile),
                    allow_zero=False,
                )
            except Exception:
                return self._failure(
                    task, AgentResultStatus.BUDGET_EXCEEDED, "PRICE_PREFLIGHT_FAILED", audit
                )
            if per_call_reservation <= 0 or per_call_reservation > remaining_case_cost:
                return self._failure(
                    task, AgentResultStatus.BUDGET_EXCEEDED, "CASE_COST_BUDGET_EXCEEDED", audit
                )
            try:
                claim = self._budget_store.reserve(
                    request.request_id, budget_candidate_for_task(task), per_call_reservation
                )
                # 执行 ID 应保证本次请求唯一；若仍命中旧记录，宁可拒绝也不能再次发送
                # 一个账本无法区分的新外部付费请求。
                if not claim.created:
                    return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "REQUEST_REPLAY_DENIED", audit)
            except (BudgetLimitExceeded, BudgetInvariantError):
                return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_DENIED", audit)

            # reserve 可能等待数据库行锁；发送模型请求前必须重新读取绝对 deadline。
            remaining_seconds = (deadline_at - self._clock()).total_seconds()
            if remaining_seconds <= 0:
                self._budget_store.release(request.request_id)
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "DEADLINE_EXCEEDED", audit)

            audit.model_calls += 1
            try:
                outcome = await asyncio.wait_for(
                    self._model_port.complete(request), timeout=remaining_seconds
                )
            except TimeoutError:
                if not self._settle_unknown(request.request_id, per_call_reservation, audit):
                    return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_RECONCILIATION_REQUIRED", audit)
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "DEADLINE_EXCEEDED", audit)
            except asyncio.CancelledError:
                try:
                    self._budget_store.settle(request.request_id, actual_cost_cny=None)
                except Exception:
                    # pending reservation 由 Task 3 恢复扫描保守结算；不能覆盖原始取消。
                    pass
                raise
            except Exception:
                if not self._settle_unknown(request.request_id, per_call_reservation, audit):
                    return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_RECONCILIATION_REQUIRED", audit)
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "MODEL_PORT_ERROR", audit)

            if isinstance(outcome, ModelFailure):
                if outcome.request_id != request.request_id:
                    if not self._settle_unknown(request.request_id, per_call_reservation, audit):
                        return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_RECONCILIATION_REQUIRED", audit)
                    return self._failure(task, AgentResultStatus.MODEL_ERROR, "MODEL_IDENTITY_MISMATCH", audit)
                if not self._settle_unknown(request.request_id, per_call_reservation, audit):
                    return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_RECONCILIATION_REQUIRED", audit)
                return self._failure(task, AgentResultStatus.MODEL_ERROR, outcome.category.value, audit)
            if not isinstance(outcome, ModelSuccess):
                if not self._settle_unknown(request.request_id, per_call_reservation, audit):
                    return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_RECONCILIATION_REQUIRED", audit)
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "INVALID_MODEL_OUTCOME", audit)
            if outcome.request_id != request.request_id or outcome.model_id != profile.model_id:
                if not self._settle_unknown(request.request_id, per_call_reservation, audit):
                    return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_RECONCILIATION_REQUIRED", audit)
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "MODEL_IDENTITY_MISMATCH", audit)
            if outcome.usage is None:
                if not self._settle_unknown(request.request_id, per_call_reservation, audit):
                    return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_RECONCILIATION_REQUIRED", audit)
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "USAGE_REQUIRED", audit)
            audit.input_tokens += outcome.usage.input_tokens
            audit.output_tokens += outcome.usage.output_tokens
            if audit.input_tokens + audit.output_tokens > profile.max_total_tokens:
                if not self._settle_unknown(request.request_id, per_call_reservation, audit):
                    return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_RECONCILIATION_REQUIRED", audit)
                return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "TOKEN_BUDGET_EXCEEDED", audit)
            try:
                self._assert_pricing_policy_frozen()
                actual_cost = self._validated_cost(
                    self._pricing_policy.actual_cost(outcome.usage, profile),
                    allow_zero=True,
                )
            except Exception:
                if not self._settle_unknown(request.request_id, per_call_reservation, audit):
                    return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_RECONCILIATION_REQUIRED", audit)
                return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "COST_SETTLEMENT_FAILED", audit)
            audit.cost_cny += actual_cost
            try:
                self._budget_store.settle(request.request_id, actual_cost_cny=actual_cost)
            except Exception:
                return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "BUDGET_RECONCILIATION_REQUIRED", audit)
            if actual_cost > per_call_reservation:
                return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "PRICE_RESERVATION_OVERRUN", audit)
            if self._clock() >= deadline_at:
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "DEADLINE_EXCEEDED", audit)

            try:
                action = AgentAction.model_validate(_plain_json(outcome.output))
            except (ValidationError, ValueError):
                return self._failure(task, AgentResultStatus.INVALID_OUTPUT, "INVALID_ACTION", audit)
            try:
                resolved_evidence.extend(
                    self._evidence_registry.resolve_many(
                        action.evidence_refs,
                        expected_room_id=task.room_id,
                        expected_anchor_id=trusted_anchor_id,
                    )
                )
                actions.append(action)
                audit.evidence_refs.extend(action.evidence_refs)
            except EvidenceResolutionError:
                return self._failure(task, AgentResultStatus.POLICY_DENIED, "EVIDENCE_DENIED", audit)
            except Exception:
                return self._failure(task, AgentResultStatus.POLICY_DENIED, "EVIDENCE_STORE_ERROR", audit)

            if action.kind is AgentActionKind.FINAL:
                try:
                    validator = Draft202012Validator(_plain_json(profile.result_schema))
                    validator.check_schema(_plain_json(profile.result_schema))
                    final_output = _plain_json(action.final_output)
                    validator.validate(final_output)
                except (JsonSchemaValidationError, JsonSchemaError):
                    return self._failure(task, AgentResultStatus.INVALID_OUTPUT, "RESULT_SCHEMA_INVALID", audit)
                if isinstance(final_output, Mapping) and "evidence_refs" in final_output:
                    try:
                        result_evidence = tuple(
                            EvidenceRef.model_validate(item) for item in final_output["evidence_refs"]
                        )
                    except (TypeError, ValidationError):
                        return self._failure(
                            task,
                            AgentResultStatus.POLICY_DENIED,
                            "RESULT_EVIDENCE_MISMATCH",
                            audit,
                        )
                    # 结果 Schema 只能约束字段形状，不能证明证据已由权威 Resolver 解析。
                    # 因此最终输出若显式携带证据，必须与本次 FINAL 动作中已解析的引用逐项一致，
                    # 防止模型在嵌套结果中替换 digest、room 或 source_version 绕过证据门禁。
                    if result_evidence != action.evidence_refs:
                        return self._failure(
                            task,
                            AgentResultStatus.POLICY_DENIED,
                            "RESULT_EVIDENCE_MISMATCH",
                            audit,
                        )
                result_evidence_ids, invalid_evidence_ids, has_evidence_ids = (
                    _collect_result_evidence_ids(final_output)
                )
                if has_evidence_ids:
                    action_evidence_ids = {ref.evidence_id for ref in action.evidence_refs}
                    # ReviewMemory 使用嵌套 evidence_ids 而不是完整 EvidenceRef；这里把所有层级
                    # 收敛到已由 Resolver 验证的 FINAL 动作证据集合，拒绝未知、缺失或歧义 ID。
                    if (
                        invalid_evidence_ids
                        or len(action_evidence_ids) != len(action.evidence_refs)
                        or result_evidence_ids != action_evidence_ids
                    ):
                        return self._failure(
                            task,
                            AgentResultStatus.POLICY_DENIED,
                            "RESULT_EVIDENCE_MISMATCH",
                            audit,
                        )
                return self._success(task, profile, action, actions, audit)
            if action.kind is AgentActionKind.ABSTAIN:
                return self._failure(
                    task, AgentResultStatus.ABSTAINED, action.reason_code or "ABSTAINED", audit
                )
            if action.skill_id not in profile.allowed_skill_ids:
                return self._failure(task, AgentResultStatus.POLICY_DENIED, "SKILL_NOT_ALLOWED", audit)
            if audit.skill_calls >= profile.max_skill_calls:
                return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "SKILL_BUDGET_EXCEEDED", audit)
            manifest = self._skill_catalog.get(action.skill_id)
            if manifest is None:
                return self._failure(task, AgentResultStatus.POLICY_DENIED, "UNKNOWN_SKILL", audit)
            if manifest.version != profile.skill_versions[action.skill_id]:
                # Profile 在评估开始前冻结精确版本；Catalog 后续升级不能静默改变候选权限或行为。
                return self._failure(
                    task,
                    AgentResultStatus.POLICY_DENIED,
                    "SKILL_VERSION_MISMATCH",
                    audit,
                )
            try:
                Draft202012Validator(_plain_json(manifest.parameter_schema)).validate(
                    _plain_json(action.arguments)
                )
            except JsonSchemaValidationError:
                return self._failure(task, AgentResultStatus.INVALID_OUTPUT, "SKILL_ARGUMENTS_INVALID", audit)
            remaining_seconds = (deadline_at - self._clock()).total_seconds()
            if remaining_seconds <= 0:
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "DEADLINE_EXCEEDED", audit)
            audit.skill_calls += 1
            try:
                skill_output = await asyncio.wait_for(
                    self._skill_port.invoke(
                        skill_id=action.skill_id,
                        skill_version=manifest.version,
                        arguments=action.arguments,
                        task=task,
                        deadline_at=deadline_at,
                        invocation_index=audit.skill_calls,
                        execution_id=execution_id,
                    ),
                    # RuntimeSkillPort 使用精确 deadline 闭合 Attempt；外层只给它一个
                    # 很小的清理宽限，防止同一时刻的取消抢先留下 INTENT_RECORDED。
                    timeout=remaining_seconds + 0.1,
                )
            except TimeoutError:
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "SKILL_DEADLINE_EXCEEDED", audit)
            except SkillPolicyDeniedError:
                return self._failure(task, AgentResultStatus.POLICY_DENIED, "SKILL_POLICY_DENIED", audit)
            except SkillRuntimeInvocationError as error:
                result = error.result
                failure = result.failure
                details = {
                    "error_code": None if result.error_code is None else result.error_code.value,
                    "attempt_id": result.attempt_id,
                    "failure_category": (
                        None if failure is None else failure.category.value
                    ),
                    "side_effect_state": (
                        None if failure is None else failure.side_effect_state.value
                    ),
                }
                return self._failure(
                    task,
                    AgentResultStatus.POLICY_DENIED,
                    "SKILL_RUNTIME_FAILED",
                    audit,
                    details=details,
                )
            except Exception:
                return self._failure(task, AgentResultStatus.MODEL_ERROR, "SKILL_PORT_ERROR", audit)
            skill_outputs.append(skill_output)

        return self._failure(task, AgentResultStatus.BUDGET_EXCEEDED, "MODEL_CALL_BUDGET_EXCEEDED", audit)

    def _settle_unknown(self, request_id: str, reserved: Decimal, audit: _RunAudit) -> bool:
        try:
            self._budget_store.settle(request_id, actual_cost_cny=None)
        except Exception:
            # reservation 保持 RESERVED，Task 3 的 pending 扫描可在重启后保守结算。
            return False
        audit.cost_cny += reserved
        return True

    def _assert_pricing_policy_frozen(self) -> None:
        if self._pricing_policy.policy_digest != self._pricing_policy_digest:
            raise BudgetInvariantError("pricing policy digest changed during run")

    @staticmethod
    def _validated_cost(value: Any, *, allow_zero: bool) -> Decimal:
        try:
            amount = Decimal(value)
            quantized = amount.quantize(Decimal("0.000001"))
        except Exception as error:
            raise BudgetInvariantError("pricing policy returned invalid cost") from error
        if (
            not amount.is_finite()
            or amount < 0
            or (amount == 0 and not allow_zero)
            or amount != quantized
        ):
            raise BudgetInvariantError("pricing policy returned invalid cost")
        return amount

    def _success(
        self,
        task: AgentTask,
        profile: SpecialistProfile,
        action: AgentAction,
        actions: list[AgentAction],
        audit: _RunAudit,
    ) -> AgentResult:
        return AgentResult(
            task_id=task.task_id,
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            status=AgentResultStatus.SUCCEEDED,
            output=_plain_json(action.final_output),
            actions=tuple(actions),
            evidence_refs=tuple(
                dict.fromkeys(
                    (*task.initial_evidence_refs, *(ref for item in actions for ref in item.evidence_refs))
                )
            ),
            summary="Specialist completed with schema-valid output",
            model_calls=audit.model_calls,
            skill_calls=audit.skill_calls,
            total_tokens=audit.input_tokens + audit.output_tokens,
            input_tokens=audit.input_tokens,
            output_tokens=audit.output_tokens,
            cost_cny=audit.cost_cny,
            latency_ms=self._latency_ms(audit),
        )

    def _failure(
        self,
        task: AgentTask,
        status: AgentResultStatus,
        code: str,
        audit: _RunAudit | None = None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        facts = audit or _RunAudit()
        return AgentResult(
            task_id=task.task_id,
            profile_id=task.profile_id,
            profile_version=task.profile_version,
            status=status,
            failure=AgentFailure(code=code, details={} if details is None else dict(details)),
            actions=tuple(facts.actions),
            evidence_refs=tuple(dict.fromkeys(facts.evidence_refs)),
            summary=code,
            model_calls=facts.model_calls,
            skill_calls=facts.skill_calls,
            input_tokens=facts.input_tokens,
            output_tokens=facts.output_tokens,
            total_tokens=facts.input_tokens + facts.output_tokens,
            cost_cny=facts.cost_cny,
            latency_ms=self._latency_ms(facts),
        )

    def _latency_ms(self, audit: _RunAudit) -> Decimal:
        if audit.started_at is None:
            return Decimal("0")
        elapsed = max((self._clock() - audit.started_at).total_seconds(), 0)
        return Decimal(str(elapsed * 1000)).quantize(Decimal("0.001"))

    @staticmethod
    def _model_request(
        task: AgentTask,
        profile: SpecialistProfile,
        deadline_at: datetime,
        execution_id: str,
        model_index: int,
        actions: list[AgentAction],
        skill_outputs: list[dict[str, Any]],
        resolved_evidence: list[Any],
        remaining_tokens: int,
    ) -> ModelRequest:
        context = {
            "objective": task.objective,
            "input_snapshot": _plain_json(task.input_snapshot),
            "prior_actions": [action.model_dump(mode="json") for action in actions],
            "skill_outputs": skill_outputs,
            # 只注入已经过 Store 身份、摘要与作用域校验的冻结投影，不让模型自行按 ID
            # 读取权威 Store，也不暴露 resolver 或任意查询能力。
            "resolved_evidence": [item.model_dump(mode="json") for item in resolved_evidence],
        }
        return ModelRequest(
            request_id=f"{task.task_id}:{execution_id}:model:{model_index + 1}",
            endpoint_host=profile.endpoint_host,
            model_id=profile.model_id,
            temperature=profile.temperature,
            prompt_hash=profile.prompt_hash,
            result_schema_hash=profile.result_schema_hash,
            messages=(
                # 发送的正文与 Profile 中的 prompt_hash 同时被冻结；仅传摘要无法约束真实模型输入。
                ModelMessage(role="system", content=profile.prompt_text),
                ModelMessage(
                    role="user",
                    content=json.dumps(context, ensure_ascii=False, sort_keys=True),
                ),
            ),
            max_output_tokens=remaining_tokens,
            deadline_at=deadline_at,
        )


class ProductionSpecialistFacade:
    """仅生产建议路径使用的显式 fallback 门面。"""

    def __init__(
        self,
        *,
        runner: BoundedSpecialistRunner,
        retained_profiles: set[str],
        baseline: Callable[[AgentTask], Any],
    ) -> None:
        self._runner = runner
        self._retained_profiles = frozenset(retained_profiles)
        self._baseline = baseline

    async def run(self, task: AgentTask) -> AgentResult:
        result = await self._runner.run(task)
        identity = f"{task.profile_id}@{task.profile_version}"
        if result.status is AgentResultStatus.SUCCEEDED or identity not in self._retained_profiles:
            return result
        fallback_started = self._runner._clock()
        output = self._baseline(task)
        fallback_latency = max((self._runner._clock() - fallback_started).total_seconds(), 0)
        return AgentResult(
            task_id=task.task_id,
            profile_id=task.profile_id,
            profile_version=task.profile_version,
            status=AgentResultStatus.FALLBACK,
            output=output,
            actions=result.actions,
            evidence_refs=result.evidence_refs,
            failure=result.failure,
            summary=f"Retained Specialist failed ({result.failure.code if result.failure else 'UNKNOWN'}); deterministic baseline used",
            model_calls=result.model_calls,
            skill_calls=result.skill_calls,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            latency_ms=result.latency_ms + Decimal(str(fallback_latency * 1000)),
            cost_cny=result.cost_cny,
        )
