"""Phase 11A SkillExecutor。

统一单次执行核心，按 Design 固定顺序校验：
版本匹配 -> 生命周期 -> Schema -> 门禁/审批 -> 幂等键 -> Handler。

所有前置校验失败返回结构化的 SkillExecutionResult，不调用 Handler。
异步入口使用 asyncio.to_thread；同步适配器直接调用内部核心。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from jsonschema import Draft202012Validator, ValidationError as JsonSchemaError

from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.models import (
    ApprovalSource,
    SkillCall,
    SkillExecutionResult,
    SkillExecutionStatus,
    SkillErrorCode,
    SkillExecutionContext,
    SkillManifest,
)


# ── 内部 Handler 注册表 ──────────────────────────────────────────────


class _SkillHandler(ABC):
    """Skill Handler 抽象基类。具体 Handler 见 pre_live_handlers.py。"""

    @abstractmethod
    def execute(self, skill_id: str, arguments: dict[str, Any], context: SkillExecutionContext) -> dict[str, Any]:
        """执行业务逻辑，返回 JSON 安全的结果字典。"""


# 全局 Handler 注册表，由预注册或外部装配填充
_HANDLERS: dict[str, _SkillHandler] = {}


def register_handler(skill_id: str, handler: _SkillHandler) -> None:
    """注册 Handler 到全局注册表。"""
    _HANDLERS[skill_id] = handler


def get_handler(skill_id: str) -> _SkillHandler | None:
    """获取 Handler，不存在时返回 None。"""
    return _HANDLERS.get(skill_id)


# ── 内部执行核心 ─────────────────────────────────────────────────────


class SkillExecutor:
    """唯一 Skill 执行器。异步入口和同步适配器共享同一内部核心。"""

    def __init__(self) -> None:
        self._catalog = {m.skill_id: m for m in get_default_skill_catalog()}

    # ── 公开异步接口 ──────────────────────────────────────────────

    async def execute(self, call: SkillCall) -> SkillExecutionResult:
        """异步单次执行。使用 asyncio.to_thread 委托同步核心。"""
        return await asyncio.to_thread(self._execute_once, call)

    # ── 同步内部核心 ──────────────────────────────────────────────

    def _execute_once(self, call: SkillCall) -> SkillExecutionResult:
        """按固定顺序校验并执行。

        1. 版本匹配
        2. 生命周期匹配
        3. Schema 参数校验
        4. 门禁与审批
        5. 幂等键检查
        6. Handler 调用
        """
        # ── Step 1: 查找 Manifest ────────────────────────────────
        manifest = self._catalog.get(call.skill_id)
        if manifest is None:
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.ERROR,
                error_code=SkillErrorCode.SKILL_NOT_FOUND,
                summary=f"未注册 skill_id: {call.skill_id}",
                audit_id=None,
            )

        # ── Step 2: 版本匹配 ─────────────────────────────────────
        if call.version != manifest.version:
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.ERROR,
                error_code=SkillErrorCode.VERSION_MISMATCH,
                summary=f"版本不匹配: 期望 {manifest.version}，收到 {call.version}",
                audit_id=None,
            )

        # ── Step 3: 生命周期匹配 ─────────────────────────────────
        ctx_life = call.context.lifecycle
        if ctx_life not in manifest.lifecycle:
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.ERROR,
                error_code=SkillErrorCode.LIFECYCLE_MISMATCH,
                summary=f"生命周期不匹配: {ctx_life} 不在 {manifest.lifecycle}",
                audit_id=None,
            )

        # ── Step 4: Schema 参数校验 ──────────────────────────────
        if manifest.parameter_schema:
            try:
                validator = Draft202012Validator(manifest.parameter_schema)
                validator.validate(call.arguments)
            except JsonSchemaError as exc:
                return SkillExecutionResult(
                    skill_id=call.skill_id,
                    version=call.version,
                    status=SkillExecutionStatus.ERROR,
                    error_code=SkillErrorCode.INVALID_ARGUMENTS,
                    summary=f"参数不合法: {exc.message}",
                    audit_id=None,
                )

        # ── Step 5: 门禁与审批 ───────────────────────────────────
        from src.core.security_hooks import GateDecision

        if manifest.gate_decision == GateDecision.HARD_GATE:
            approval = call.context.approval
            if approval is None:
                return SkillExecutionResult(
                    skill_id=call.skill_id,
                    version=call.version,
                    status=SkillExecutionStatus.PENDING,
                    error_code=SkillErrorCode.APPROVAL_REQUIRED,
                    summary="高风险 Skill 需要审批",
                    audit_id=None,
                )
            if approval.decision != "APPROVED":
                return SkillExecutionResult(
                    skill_id=call.skill_id,
                    version=call.version,
                    status=SkillExecutionStatus.ERROR,
                    error_code=SkillErrorCode.APPROVAL_REJECTED,
                    summary=f"审批被拒绝: {approval.decision} (来源: {approval.source})",
                    audit_id=None,
                )

        # ── Step 6: 幂等键检查 ──────────────────────────────────
        if manifest.requires_idempotency_key and not call.context.idempotency_key:
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.ERROR,
                error_code=SkillErrorCode.IDEMPOTENCY_REQUIRED,
                summary="该 Skill 需要幂等键",
                audit_id=None,
            )

        # ── Step 7: Handler 查找与执行 ──────────────────────────
        handler = _HANDLERS.get(call.skill_id)
        if handler is None:
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.ERROR,
                error_code=SkillErrorCode.HANDLER_NOT_FOUND,
                summary=f"Handler 未注册: {call.skill_id}",
                audit_id=None,
            )

        try:
            output = handler.execute(call.skill_id, call.arguments, call.context)
        except Exception as exc:
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.ERROR,
                error_code=SkillErrorCode.HANDLER_FAILED,
                summary=f"Handler 执行异常: {type(exc).__name__}",
                output=None,
                audit_id=None,
            )

        return SkillExecutionResult(
            skill_id=call.skill_id,
            version=call.version,
            status=SkillExecutionStatus.SUCCESS,
            output=output,
            summary="执行成功",
            audit_id=None,
        )


# ── 同步适配器 ────────────────────────────────────────────────────────


class SyncSkillExecutorAdapter:
    """供播前同步 Graph 使用，不携带任何校验或路由逻辑。"""

    def __init__(self, executor: SkillExecutor | None = None) -> None:
        self._executor = executor or SkillExecutor()

    def execute(self, call: SkillCall) -> SkillExecutionResult:
        return self._executor._execute_once(call)
