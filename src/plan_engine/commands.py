"""Phase 12A 人工命令模型与命令服务边界。

命令服务本身不保存第二份账本，也不直接修改节点。它只负责构造严格、不可变且
JSON-safe 的命令事实，并把命令连同调用时钟提交给 PlanStore；幂等、版本/状态校验
和状态写入必须在 Store 的同一原子边界内完成。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.plan_engine.models import (
    FrozenDict,
    FrozenList,
    PlanCommandType,
    PlanNodeState,
)
from src.plan_engine.store import PlanStore


APPROVAL_COMMAND_TTL = timedelta(minutes=10)
RECONCILIATION_COMMAND_TTL = timedelta(minutes=30)


def _freeze_command_json(value: Any) -> Any:
    """递归验证并冻结命令载荷，禁止隐式类型转换和提交后原地篡改。

    人工命令会进入长期审计账本，因此这里只接受 JSON 原生标量、字符串键映射和
    数组；bytes、tuple、NaN、Infinity 及非字符串 key 均 fail-closed。
    """
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("命令 JSON 浮点数必须是有限值")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("命令 JSON 对象 key 必须是字符串")
        return FrozenDict(
            {key: _freeze_command_json(item) for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, tuple)):
        return FrozenList(_freeze_command_json(item) for item in value)
    raise ValueError(f"命令载荷不是 JSON-safe 类型: {type(value).__name__}")


def _empty_command_payload() -> FrozenDict:
    """为省略 payload 的命令返回独立冻结空对象。"""
    return FrozenDict()


class PlanCommand(BaseModel):
    """提交给权威命令账本的完整不可变人工意图。

    ``expected_plan_version`` 与 ``expected_node_status`` 是调用方读取后携带的乐观
    并发事实，不是服务端提示。Store 必须在写状态前重新核对；不匹配只记录拒绝结果。
    """

    model_config = ConfigDict(frozen=True)

    command_id: str = Field(..., min_length=1)
    command_type: PlanCommandType
    plan_run_id: str = Field(..., min_length=1)
    expected_plan_version: int = Field(..., ge=1, strict=True)
    node_id: str | None = Field(default=None, min_length=1)
    expected_node_status: PlanNodeState | None = None
    payload: Any = Field(default_factory=_empty_command_payload)
    issued_at: datetime

    @field_validator("payload", mode="after")
    @classmethod
    def _payload_must_be_frozen_json(cls, value: Any) -> Any:
        """把调用方载荷复制并冻结，使账本摘要不受外部引用修改影响。"""
        return _freeze_command_json(value)

    @field_validator("issued_at")
    @classmethod
    def _issued_at_must_be_aware(cls, value: datetime) -> datetime:
        """命令签发时间统一规范为 UTC，确保 TTL 比较不依赖进程本地时区。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("issued_at 必须包含时区")
        return value.astimezone(timezone.utc)

    @property
    def ttl(self) -> timedelta:
        """由命令种类返回服务端固定 TTL，调用方不能通过 payload 延长有效期。"""
        if self.command_type is PlanCommandType.RECONCILE:
            return RECONCILIATION_COMMAND_TTL
        return APPROVAL_COMMAND_TTL


class PlanCommandResult(BaseModel):
    """命令首次执行的冻结结果；重复 command_id 必须原样重放此事实。"""

    model_config = ConfigDict(frozen=True)

    command_id: str = Field(..., min_length=1)
    command_type: PlanCommandType
    plan_run_id: str = Field(..., min_length=1)
    accepted: bool
    reason: str = Field(..., min_length=1)
    plan_version: int = Field(..., ge=1)
    node_id: str | None = None
    resulting_node_status: PlanNodeState | None = None
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def _completed_at_must_be_aware(cls, value: datetime) -> datetime:
        """账本结果时间统一保存为 UTC，便于跨进程重放和审计。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completed_at 必须包含时区")
        return value.astimezone(timezone.utc)


class PlanCommandLedgerView(BaseModel):
    """PlanQueryService 返回的扁平、冻结且 JSON-safe 的命令账本视图。

    视图同时展示首次请求的乐观并发事实与首次结果，``expires_at`` 明确记录服务端
    固定 TTL 的截止时间。它不暴露 Store 锁、内部记录对象或可变 payload 引用。
    """

    model_config = ConfigDict(frozen=True)

    command_id: str = Field(..., min_length=1)
    command_type: PlanCommandType
    plan_run_id: str = Field(..., min_length=1)
    expected_plan_version: int = Field(..., ge=1)
    node_id: str | None = None
    expected_node_status: PlanNodeState | None = None
    payload: Any = Field(default_factory=_empty_command_payload)
    issued_at: datetime
    expires_at: datetime
    accepted: bool
    reason: str = Field(..., min_length=1)
    plan_version: int = Field(..., ge=1)
    resulting_node_status: PlanNodeState | None = None
    completed_at: datetime

    @field_validator("payload", mode="after")
    @classmethod
    def _ledger_payload_must_be_frozen_json(cls, value: Any) -> Any:
        """为每次查询重建冻结 payload，避免复用命令对象内部容器引用。"""
        return _freeze_command_json(value)

    @field_validator("issued_at", "expires_at", "completed_at")
    @classmethod
    def _ledger_times_must_be_aware(cls, value: datetime) -> datetime:
        """账本中的签发、过期与完成时间全部规范为 UTC。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("命令账本时间必须包含时区")
        return value.astimezone(timezone.utc)


class CommandReconciler(Protocol):
    """CommandService 使用的最小一致性入口，不暴露 checkpointer 实现。"""

    def reconcile_before_command(self, command: PlanCommand) -> Any:
        """在命令进入权威账本前检查目标计划与 checkpoint。"""


class CommandService:
    """人工命令的薄服务层，把所有权威判断委托给 PlanStore。"""

    def __init__(
        self,
        store: PlanStore,
        reconciler: CommandReconciler | None = None,
    ) -> None:
        """注入权威 Store 与可选统一对账入口，不维护进程内幂等缓存。"""
        self._store = store
        self._reconciler = reconciler

    def submit(
        self,
        command: PlanCommand,
        *,
        now: datetime | None = None,
    ) -> PlanCommandResult:
        """提交命令并返回首次账本结果；可注入时钟仅用于确定性测试。"""
        if self._reconciler is not None:
            # 对账发生在 Store 的任何命令状态修改前。异常直接向上传播并阻止命令
            # 入账，不能在一致性状态未知时尝试“先执行再补检查”。
            self._reconciler.reconcile_before_command(command)
        submitted_at = now or datetime.now(timezone.utc)
        return self._store.submit_command(command=command, now=submitted_at)
