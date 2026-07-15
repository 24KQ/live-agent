"""Phase 11B 原生 async SkillExecutor。

本模块是 Skill Runtime 唯一的执行入口。它负责门禁、绝对 deadline、Attempt
意图与终态证据，但不拥有重试、回退或 Replan 策略。业务 Handler 只能执行一次，
平台相关失败必须以 ``FailureFact`` 返回给上层的集中恢复策略。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError as JsonSchemaError

from src.core.security_hooks import evaluate_tool_gate
from src.skill_runtime.attempt_store import (
    AttemptInvariantError,
    AttemptRecord,
    AttemptState,
    AttemptStore,
    InMemoryAttemptStore,
    OperationRequest,
)
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.models import (
    AdapterSuccess,
    AuthorizationRequirement,
    FailureCategory,
    FailureFact,
    SideEffectState,
    SkillCall,
    SkillErrorCode,
    SkillExecutionContext,
    SkillExecutionResult,
    SkillExecutionStatus,
    SkillManifest,
)
from src.skill_runtime.policy_view import (
    SkillPolicyView,
    assert_policy_view_matches_catalog,
    get_default_skill_policy_view,
)


class _SkillHandler(ABC):
    """Skill Handler 的原生 async 单次尝试抽象。

    Handler 不得在内部重试、切换 Legacy 路由或伪造审批。涉及平台状态时，它应当
    调用业务域 Port，并原样返回 ``AdapterSuccess`` 或 ``FailureFact``；纯确定性
    能力可直接返回 JSON 安全字典或携带 audit_id 的内部结果。
    """

    @abstractmethod
    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> "_SkillHandlerResult | AdapterSuccess | FailureFact | dict[str, Any]":
        """执行一次业务编排，禁止由 Handler 决定自动恢复动作。"""


@dataclass(frozen=True)
class _SkillHandlerResult:
    """确定性 Handler 的输出封装，避免把兼容审计 ID 混入业务快照。"""

    output: dict[str, Any]
    audit_id: str | None = None


# 兼容既有启动装配的全局注册表。Executor 在构造时复制快照，因此后续重注册
# 只影响新的装配实例，不会改变已经开始的调用路径。
_HANDLERS: dict[str, _SkillHandler] = {}


def register_handler(skill_id: str, handler: _SkillHandler) -> None:
    """注册兼容 Handler；Phase 11B 后续批次会逐步改用局部统一装配。"""
    _HANDLERS[skill_id] = handler


def get_handler(skill_id: str) -> _SkillHandler | None:
    """读取兼容注册表，测试可在保存原对象后验证装配快照不被污染。"""
    return _HANDLERS.get(skill_id)


class SkillExecutor:
    """唯一的原生 async 单次执行核心。

    对具有幂等键的调用，Attempt Store 在 Handler 前持久化操作意图。重复调用只
    重放已记录的事实，不再调用 Handler，从而避免同一个业务 Operation 产生第二
    次外部副作用。没有幂等键的纯读取/确定性 Skill 保持单次执行，不伪造 Operation。
    """

    def __init__(
        self,
        handlers: Mapping[str, _SkillHandler] | None = None,
        *,
        attempt_store: AttemptStore | None = None,
        policy_view: SkillPolicyView | None = None,
    ) -> None:
        self._catalog = {manifest.skill_id: manifest for manifest in get_default_skill_catalog()}
        # Executor 与 Hook/Flow 共用同一治理投影类型；快照只在构造时生成一次。
        self._policy_view = policy_view or get_default_skill_policy_view()
        assert_policy_view_matches_catalog(
            tuple(self._catalog.values()),
            self._policy_view,
        )
        self._handlers = dict(_HANDLERS if handlers is None else handlers)
        # 默认内存 Store 只保证当前 Executor 实例的安全重放；生产装配将在后续任务
        # 显式注入 PostgreSQL Store，不能把该实现误当成跨进程互斥机制。
        self._attempt_store = attempt_store or InMemoryAttemptStore()

    async def execute(self, call: SkillCall) -> SkillExecutionResult:
        """按固定门禁顺序执行一次 async Handler，绝不隐式重试或回退。"""
        manifest_or_result = self._validate_call(call)
        if isinstance(manifest_or_result, SkillExecutionResult):
            return manifest_or_result
        manifest = manifest_or_result

        handler = self._handlers.get(call.skill_id)
        if handler is None:
            return self._error(
                call,
                SkillErrorCode.HANDLER_NOT_FOUND,
                f"Handler 未注册: {call.skill_id}",
            )

        record: AttemptRecord | None = None
        if call.context.idempotency_key:
            try:
                claim = self._attempt_store.claim_or_replay(self._operation_request(call))
            except AttemptInvariantError:
                return self._error(
                    call,
                    SkillErrorCode.HANDLER_FAILED,
                    "Skill execution intent conflicts with an existing operation",
                )
            if not claim.created:
                return self._replay_result(call, claim.record)
            record = claim.record

        remaining = (call.context.deadline_at - datetime.now(timezone.utc)).total_seconds()
        timeout = min(remaining, manifest.max_attempt_seconds)
        if timeout <= 0:
            # 对带幂等键的调用，必须先原子创建或重放 Operation，才能避免第二次请求
            # 因 deadline 到期覆盖首次的成功、失败或副作用未知事实。首次到期调用会
            # 写入“未发送”终态，但绝不调用 Handler；无幂等键的确定性调用不写 Store。
            return self._complete_failure(
                call,
                record,
                self._not_sent_deadline_failure(record),
                summary="Skill deadline expired before handler execution",
            )

        handler_context = (
            call.context
            if record is None
            else call.context.model_copy(update={"attempt_id": record.attempt_id})
        )

        try:
            # wait_for 只管理协作式 async 边界。它不会把超时解释为“未发生副作用”，
            # 因此 TimeoutError 必须按发送后未知闭合 Attempt，交由未来对账处理。
            outcome = await asyncio.wait_for(
                handler.execute(call.skill_id, call.arguments, handler_context),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return self._complete_failure(
                call,
                record,
                self._unknown_failure(record),
                summary="Skill handler timed out after execution started",
            )
        except Exception:
            # 业务异常不能泄露异常文本或参数；既然 Handler 已开始，使用内部不变量
            # 失败闭合已有 Attempt，阻止同一幂等键被当作未执行而再次发送。
            return self._complete_failure(
                call,
                record,
                self._internal_handler_failure(record),
                summary="Handler execution failed",
            )

        if isinstance(outcome, FailureFact):
            failure = self._normalize_handler_failure(outcome, record)
            return self._complete_failure(
                call,
                record,
                failure,
                summary="Skill handler returned a failure fact",
            )

        if isinstance(outcome, AdapterSuccess):
            output = outcome.output
            audit_id = None
        elif isinstance(outcome, _SkillHandlerResult):
            output = outcome.output
            audit_id = outcome.audit_id
        else:
            output = outcome
            audit_id = None

        try:
            if record is not None:
                # Attempt Store 的终态必须包含重放公开结果所需的全部事实。业务输出
                # 与兼容 ToolCallAudit 的关联分层保存，避免重放 setup 时丢失首次
                # audit_id 并错误表现为新的、未审计执行。
                self._attempt_store.complete_success(
                    record.attempt_id,
                    self._success_terminal_payload(output, audit_id),
                )
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.SUCCESS,
                output=output,
                summary="执行成功",
                audit_id=audit_id,
                attempt_id=None if record is None else record.attempt_id,
            )
        except (AttemptInvariantError, ValueError, TypeError):
            # 非 JSON 输出、Store 终态冲突和不合法结果都不得作为成功返回。若已有
            # Attempt，需要闭合为不可自动重放的内部失败；闭合再次失败时 fail-closed。
            return self._complete_failure(
                call,
                record,
                self._internal_handler_failure(record),
                summary="Handler execution failed",
            )

    def _validate_call(self, call: SkillCall) -> SkillManifest | SkillExecutionResult:
        """执行 Handler 前的固定前置校验，保持 Phase 11A 的稳定错误顺序。"""
        manifest = self._catalog.get(call.skill_id)
        if manifest is None:
            return self._error(
                call,
                SkillErrorCode.SKILL_NOT_FOUND,
                f"未注册 skill_id: {call.skill_id}",
            )
        if call.version != manifest.version:
            return self._error(
                call,
                SkillErrorCode.VERSION_MISMATCH,
                f"版本不匹配: 期望 {manifest.version}，收到 {call.version}",
            )
        if call.context.lifecycle not in manifest.lifecycle:
            return self._error(
                call,
                SkillErrorCode.LIFECYCLE_MISMATCH,
                f"生命周期不匹配: {call.context.lifecycle} 不在 {manifest.lifecycle}",
            )
        if manifest.parameter_schema:
            try:
                Draft202012Validator(manifest.parameter_schema).validate(call.arguments)
            except JsonSchemaError as exc:
                return self._error(
                    call,
                    SkillErrorCode.INVALID_ARGUMENTS,
                    f"参数不合法: {exc.message}",
                )
        if manifest.requires_idempotency_key and not call.context.idempotency_key:
            return self._error(call, SkillErrorCode.IDEMPOTENCY_REQUIRED, "该 Skill 需要幂等键")

        authorization = self._validate_authorization(call, manifest)
        if isinstance(authorization, SkillExecutionResult):
            return authorization
        gate = evaluate_tool_gate(
            self._policy_view.get(call.skill_id),
            confirmed=authorization,
        )
        if gate.allowed:
            return manifest
        approval = call.context.approval
        if gate.requires_confirmation and approval is None:
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.PENDING,
                error_code=SkillErrorCode.APPROVAL_REQUIRED,
                summary="高风险 Skill 需要审批",
                audit_id=None,
            )
        if gate.requires_confirmation and approval is not None:
            return self._error(
                call,
                SkillErrorCode.APPROVAL_REJECTED,
                f"审批被拒绝: {approval.decision} (来源: {approval.source})",
            )
        return self._error(
            call,
            SkillErrorCode.APPROVAL_REJECTED,
            f"安全门禁拒绝执行: {gate.reason}",
        )

    def _validate_authorization(
        self,
        call: SkillCall,
        manifest: SkillManifest,
    ) -> bool | SkillExecutionResult:
        """在 Attempt 意图前验证 Manifest 声明的可信授权来源。

        Tool gate 仍负责通用风险策略，本方法只处理 Runtime 已冻结的授权证据。事件
        授权与人工审批不能相互替代或同时出现；售罄 CAS 还必须把事件观察版本绑定到
        ``expected_version``，防止同一可信事件被搬运到另一资源版本的写请求。
        """
        approval = call.context.approval
        event_authorization = call.context.event_authorization
        if approval is not None and event_authorization is not None:
            return self._error(
                call,
                SkillErrorCode.APPROVAL_REJECTED,
                "授权来源冲突: approval 与 event_authorization 不能同时提供",
            )

        approved_human = (
            approval is not None
            and approval.provenance_verified
            and approval.decision == "APPROVED"
        )
        requirement = manifest.authorization_requirement
        if requirement is AuthorizationRequirement.NONE:
            return approved_human

        if requirement is AuthorizationRequirement.HUMAN_APPROVAL:
            if approval is None:
                return self._pending_authorization(call, "该 Skill 需要可信人工审批")
            if approved_human:
                return True
            return self._error(
                call,
                SkillErrorCode.APPROVAL_REJECTED,
                "人工审批未通过或来源不可信",
            )

        if approved_human:
            return True
        if approval is not None:
            return self._error(
                call,
                SkillErrorCode.APPROVAL_REJECTED,
                "人工审批未通过或来源不可信",
            )
        if event_authorization is None:
            return self._pending_authorization(
                call,
                "该 Skill 需要可信事件授权或人工审批",
            )
        if not event_authorization.provenance_verified:
            return self._error(
                call,
                SkillErrorCode.APPROVAL_REJECTED,
                "事件授权来源身份未通过验证",
            )
        expected_version = call.arguments.get("expected_version")
        if (
            type(expected_version) is not int
            or expected_version != event_authorization.observed_version
        ):
            return self._error(
                call,
                SkillErrorCode.APPROVAL_REJECTED,
                "事件观察版本与 CAS expected_version 不一致",
            )
        return True

    @staticmethod
    def _pending_authorization(
        call: SkillCall,
        summary: str,
    ) -> SkillExecutionResult:
        """构造不会写 Attempt 的统一授权等待结果。"""
        return SkillExecutionResult(
            skill_id=call.skill_id,
            version=call.version,
            status=SkillExecutionStatus.PENDING,
            error_code=SkillErrorCode.APPROVAL_REQUIRED,
            summary=summary,
            audit_id=None,
        )

    @staticmethod
    def _operation_request(call: SkillCall) -> OperationRequest:
        """从已冻结调用生成不可变 Operation 意图，禁止业务参数篡改可信上下文。"""
        assert call.context.idempotency_key is not None
        return OperationRequest(
            skill_id=call.skill_id,
            skill_version=call.version,
            room_id=call.context.room_id,
            idempotency_key=call.context.idempotency_key,
            deadline_at=call.context.deadline_at,
            intent_payload=dict(call.arguments),
        )

    @staticmethod
    def _error(
        call: SkillCall,
        error_code: SkillErrorCode,
        summary: str,
    ) -> SkillExecutionResult:
        """构造不含内部异常细节的前置失败结果。"""
        return SkillExecutionResult(
            skill_id=call.skill_id,
            version=call.version,
            status=SkillExecutionStatus.ERROR,
            error_code=error_code,
            summary=summary,
            audit_id=None,
        )

    @staticmethod
    def _failure_result(
        call: SkillCall,
        failure: FailureFact,
        *,
        error_code: SkillErrorCode,
        summary: str,
    ) -> SkillExecutionResult:
        """将结构化失败事实映射为受控 Runtime 结果，不附加恢复动作。"""
        return SkillExecutionResult(
            skill_id=call.skill_id,
            version=call.version,
            status=SkillExecutionStatus.ERROR,
            error_code=error_code,
            summary=summary,
            audit_id=None,
            attempt_id=failure.attempt_id,
            failure=failure,
        )

    @staticmethod
    def _not_sent_deadline_failure(record: AttemptRecord | None) -> FailureFact:
        """构造发送前 deadline 失败，并关联首次已写入的 Operation（若存在）。"""
        return FailureFact(
            category=FailureCategory.TRANSIENT_INFRA,
            external_code="runtime.deadline_before_handler",
            side_effect_state=SideEffectState.NOT_SENT,
            attempt_id=record.attempt_id if record is not None else f"not-sent-{uuid4()}",
        )

    @staticmethod
    def _unknown_failure(record: AttemptRecord | None) -> FailureFact:
        """Handler 已启动后超时，保守记录为副作用未知并阻止同键重放。"""
        return FailureFact(
            category=FailureCategory.SIDE_EFFECT_UNKNOWN,
            external_code="runtime.handler_timeout_unknown",
            side_effect_state=SideEffectState.UNKNOWN,
            attempt_id=record.attempt_id if record is not None else f"unknown-{uuid4()}",
        )

    @staticmethod
    def _internal_handler_failure(record: AttemptRecord | None) -> FailureFact:
        """将未预期 Handler/结果错误脱敏为内部不变量事实。"""
        return FailureFact(
            category=FailureCategory.INTERNAL_INVARIANT,
            external_code="runtime.handler_failed",
            side_effect_state=SideEffectState.UNKNOWN,
            attempt_id=record.attempt_id if record is not None else f"internal-{uuid4()}",
        )

    @staticmethod
    def _normalize_handler_failure(
        failure: FailureFact,
        record: AttemptRecord | None,
    ) -> FailureFact:
        """拒绝 Handler 伪造其他 Attempt ID，确保终态只能闭合当前 Operation。"""
        if record is None or failure.attempt_id == record.attempt_id:
            return failure
        return FailureFact(
            category=FailureCategory.INTERNAL_INVARIANT,
            external_code="runtime.failure_attempt_mismatch",
            side_effect_state=SideEffectState.UNKNOWN,
            attempt_id=record.attempt_id,
        )

    def _complete_failure(
        self,
        call: SkillCall,
        record: AttemptRecord | None,
        failure: FailureFact,
        *,
        summary: str,
    ) -> SkillExecutionResult:
        """先闭合已有 Attempt，再返回失败；终态冲突必须 fail-closed。"""
        if record is not None:
            try:
                self._attempt_store.complete_failure(record.attempt_id, failure)
            except AttemptInvariantError:
                failure = FailureFact(
                    category=FailureCategory.INTERNAL_INVARIANT,
                    external_code="runtime.attempt_completion_conflict",
                    side_effect_state=SideEffectState.UNKNOWN,
                    attempt_id=record.attempt_id,
                )
                summary = "Skill execution attempt could not be closed"
        return self._failure_result(
            call,
            failure,
            error_code=SkillErrorCode.HANDLER_FAILED,
            summary=summary,
        )

    def _replay_result(self, call: SkillCall, record: AttemptRecord) -> SkillExecutionResult:
        """重放唯一 Attempt 的已知事实，绝不再次调用 Handler 或 Adapter。"""
        if record.state == AttemptState.SUCCEEDED:
            output, audit_id = self._unpack_success_terminal_payload(record.terminal_payload)
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.SUCCESS,
                output=output,
                summary="执行结果已重放",
                audit_id=audit_id,
                attempt_id=record.attempt_id,
            )
        if record.failure is not None:
            return self._failure_result(
                call,
                record.failure,
                error_code=SkillErrorCode.HANDLER_FAILED,
                summary="执行失败事实已重放",
            )
        return self._error(
            call,
            SkillErrorCode.HANDLER_FAILED,
            "Skill execution attempt is still in progress",
        )

    @staticmethod
    def _success_terminal_payload(
        output: dict[str, Any],
        audit_id: str | None,
    ) -> dict[str, Any]:
        """以私有固定包络持久化 Runtime 重放所需的完整成功事实。"""
        return {
            "__skill_runtime_output__": output,
            "__skill_runtime_audit_id__": audit_id,
        }

    @staticmethod
    def _unpack_success_terminal_payload(
        payload: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str | None]:
        """读取新包络；兼容 Task 2 已存在的普通成功 payload 供独立 Store 使用。"""
        if payload is not None and "__skill_runtime_output__" in payload:
            output = payload["__skill_runtime_output__"]
            if not isinstance(output, dict):
                raise AttemptInvariantError("invalid skill runtime terminal output")
            audit_id = payload.get("__skill_runtime_audit_id__")
            if audit_id is not None and not isinstance(audit_id, str):
                raise AttemptInvariantError("invalid skill runtime terminal audit id")
            return output, audit_id
        return payload or {}, None


class SyncSkillExecutorAdapter:
    """同步 Graph 的受限桥接器，只复用 async 核心且拒绝嵌套事件循环。"""

    def __init__(self, executor: SkillExecutor | None = None) -> None:
        self._executor = executor or SkillExecutor()

    def execute(self, call: SkillCall) -> SkillExecutionResult:
        """在无运行中事件循环的同步边界执行；async 调用方必须直接 await。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._executor.execute(call))
        raise RuntimeError(
            "SyncSkillExecutorAdapter cannot run inside an active event loop; await SkillExecutor.execute instead"
        )
