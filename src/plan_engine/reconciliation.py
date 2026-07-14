"""Phase 12A PlanStore 与 LangGraph checkpoint 的公开一致性边界。

PlanStore 是执行事实权威源，checkpoint 只保存 Graph 控制位置和计划引用。本模块
只调用 checkpointer.get_tuple()，不读取或修改官方私有表；任何 checkpoint 领先
都持久化为 INTERNAL_INVARIANT，绝不从 checkpoint 反向补造 NodeRun。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.plan_engine.commands import PlanCommand
from src.plan_engine.models import (
    FrozenDict,
    NodeRunView,
    PlanNodeState,
    PlanRunState,
)
from src.plan_engine.store import PlanStore


RECONCILIATION_INTERVAL_SECONDS = 30
"""D-034 固定的后台兜底扫描周期；调用方负责调度，本模块不创建隐藏线程。"""


class CheckpointControlPosition(StrEnum):
    """checkpoint 允许声明的批次终点，不表达中间业务执行状态。"""

    CARD_BATCH_SUCCEEDED = "CARD_BATCH_SUCCEEDED"
    CARD_BATCH_FAILED = "CARD_BATCH_FAILED"


class ReconciliationCategory(StrEnum):
    """对账调用的稳定分类，供 Graph、日志和验收报告统一消费。"""

    NO_CHECKPOINT = "NO_CHECKPOINT"
    CONSISTENT = "CONSISTENT"
    REPLAY_REUSE = "REPLAY_REUSE"
    INTERNAL_INVARIANT = "INTERNAL_INVARIANT"


class PlanCheckpointReference(BaseModel):
    """Graph checkpoint 中唯一允许保存的 PlanEngine 引用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_run_id: str = Field(..., min_length=1)
    plan_version: int = Field(..., ge=1, strict=True)
    control_position: CheckpointControlPosition


class PlanReconciliationOutcome(BaseModel):
    """一次对账的冻结结果；不携带 Store 连接或 checkpoint 内部对象。"""

    model_config = ConfigDict(frozen=True)

    plan_run_id: str = Field(..., min_length=1)
    plan_version: int = Field(..., ge=1)
    category: ReconciliationCategory
    plan_state: PlanRunState
    cards_snapshot: tuple[Any, ...] = Field(default_factory=tuple)
    audit_summary: Any = Field(default_factory=FrozenDict)
    failure_signature: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("cards_snapshot", mode="after")
    @classmethod
    def _freeze_cards_snapshot(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        """复用卡片必须重新走严格 JSON 冻结，避免 Graph 修改 NodeRun 输出。"""
        validated = NodeRunView(
            node_run_id="reconciliation-view",
            plan_run_id="reconciliation-view",
            node_id="reconciliation-view",
            attempt_number=1,
            state=PlanNodeState.SUCCEEDED,
            output=list(value),
        ).output
        return tuple(validated or ())

    @field_validator("audit_summary", mode="after")
    @classmethod
    def _freeze_audit_summary(cls, value: Any) -> Any:
        """审计摘要保持 JSON object 语义且创建后不可原地修改。"""
        validated = NodeRunView(
            node_run_id="reconciliation-audit",
            plan_run_id="reconciliation-audit",
            node_id="reconciliation-audit",
            attempt_number=1,
            state=PlanNodeState.SUCCEEDED,
            output=value,
        ).output
        if not isinstance(validated, FrozenDict):
            raise ValueError("audit_summary 必须是 JSON object")
        return validated


class PublicCheckpointer(Protocol):
    """Reconciliation Service 依赖的官方最小读取接口。"""

    def get_tuple(self, config: dict[str, Any]) -> Any:
        """按 LangGraph RunnableConfig 返回最新 CheckpointTuple。"""


class _InvalidCheckpointReference(ValueError):
    """表示 checkpoint 中存在引用字段但无法通过受控 Schema。"""


class PlanReconciliationService:
    """集中比较 PlanStore 与 checkpoint，并执行唯一允许的恢复动作。"""

    def __init__(
        self,
        *,
        store: PlanStore,
        checkpointer: PublicCheckpointer,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """冻结权威 Store、公开 checkpointer 和可测试 UTC 时钟。"""
        self._store = store
        self._checkpointer = checkpointer
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def reconcile(self, plan_run_id: str) -> PlanReconciliationOutcome:
        """对账单个 PlanRun；checkpoint 不可用异常直接上抛，不伪装业务一致。"""
        plan_run = self._store.get_plan_run(plan_run_id)
        try:
            reference = self._read_checkpoint_reference(plan_run.trace_id)
        except _InvalidCheckpointReference:
            return self._record_invariant(
                plan_run_id=plan_run.plan_run_id,
                reason="INVALID_CHECKPOINT_REFERENCE",
                reference=None,
            )

        if reference is None:
            if plan_run.reconciliation_required:
                # 事故发生后 checkpoint 证据消失不能自动解除阻断。返回持久化签名，
                # 等待人工提供可验证证据，而不是把“读不到”当成已经恢复。
                return PlanReconciliationOutcome(
                    plan_run_id=plan_run.plan_run_id,
                    plan_version=plan_run.current_version,
                    category=ReconciliationCategory.INTERNAL_INVARIANT,
                    plan_state=plan_run.state,
                    audit_summary={"reason": "RECONCILIATION_EVIDENCE_MISSING"},
                    failure_signature=plan_run.reconciliation_signature,
                )
            if plan_run.state in {PlanRunState.SUCCEEDED, PlanRunState.FAILED}:
                return self._replay_reuse(plan_run.plan_run_id)
            return self._outcome(
                plan_run_id=plan_run.plan_run_id,
                plan_version=plan_run.current_version,
                category=ReconciliationCategory.NO_CHECKPOINT,
                plan_state=plan_run.state,
                audit_summary={"checkpoint": "ABSENT_OR_BEHIND"},
            )

        if (
            reference.plan_run_id != plan_run.plan_run_id
            or reference.plan_version != plan_run.current_version
        ):
            return self._record_invariant(
                plan_run_id=plan_run.plan_run_id,
                reason="CHECKPOINT_REFERENCE_MISMATCH",
                reference=reference,
            )

        expected_position = (
            CheckpointControlPosition.CARD_BATCH_SUCCEEDED
            if plan_run.state is PlanRunState.SUCCEEDED
            else CheckpointControlPosition.CARD_BATCH_FAILED
            if plan_run.state is PlanRunState.FAILED
            else None
        )
        if expected_position is None:
            return self._record_invariant(
                plan_run_id=plan_run.plan_run_id,
                reason="CHECKPOINT_AHEAD_OF_PLANSTORE",
                reference=reference,
            )
        if reference.control_position is not expected_position:
            return self._record_invariant(
                plan_run_id=plan_run.plan_run_id,
                reason="CHECKPOINT_PLANSTORE_OUTCOME_CONFLICT",
                reference=reference,
            )

        if plan_run.reconciliation_required:
            plan_run = self._store.clear_reconciliation_failure(
                plan_run_id=plan_run.plan_run_id,
                now=self._aware_now(),
            )
        cards = (
            self._cards_snapshot(plan_run.plan_run_id)
            if plan_run.state is PlanRunState.SUCCEEDED
            else ()
        )
        return self._outcome(
            plan_run_id=plan_run.plan_run_id,
            plan_version=plan_run.current_version,
            category=ReconciliationCategory.CONSISTENT,
            plan_state=plan_run.state,
            cards_snapshot=cards,
            audit_summary={"checkpoint": reference.control_position.value},
        )

    def reconcile_startup(self) -> tuple[PlanReconciliationOutcome, ...]:
        """服务启动时扫描非终态或仍有事故的计划。"""
        return self._reconcile_scan()

    def reconcile_active_plans_once(self) -> tuple[PlanReconciliationOutcome, ...]:
        """供外部调度器每 30 秒调用；不创建常驻线程。"""
        return self._reconcile_scan()

    def reconcile_before_command(
        self,
        command: PlanCommand,
    ) -> PlanReconciliationOutcome:
        """人工命令进入账本前复用同一单计划对账逻辑。"""
        return self.reconcile(command.plan_run_id)

    def _reconcile_scan(self) -> tuple[PlanReconciliationOutcome, ...]:
        """集中实现启动/周期扫描，确保两个入口不会产生策略漂移。"""
        return tuple(
            self.reconcile(plan_run.plan_run_id)
            for plan_run in self._store.list_plan_runs()
        )

    def _read_checkpoint_reference(
        self,
        thread_id: str,
    ) -> PlanCheckpointReference | None:
        """通过公开 CheckpointTuple 读取 channel_values 中的最小引用。"""
        checkpoint_tuple = self._checkpointer.get_tuple(
            {"configurable": {"thread_id": thread_id}}
        )
        if checkpoint_tuple is None:
            return None
        checkpoint = getattr(checkpoint_tuple, "checkpoint", None)
        if not isinstance(checkpoint, Mapping):
            return None
        channel_values = checkpoint.get("channel_values")
        if not isinstance(channel_values, Mapping):
            return None
        raw_reference = channel_values.get("plan_checkpoint_reference")
        if raw_reference is None:
            return None
        try:
            return PlanCheckpointReference.model_validate(raw_reference)
        except ValidationError:
            # 非法引用本身就是 checkpoint 领先/损坏证据。使用合成引用无法保留
            # 原始敏感载荷，因此交给稳定摘要路径记录最小失败事实。
            raise _InvalidCheckpointReference(
                "checkpoint 中的 PlanCheckpointReference 非法"
            )

    def _replay_reuse(self, plan_run_id: str) -> PlanReconciliationOutcome:
        """PlanStore 领先时只读取成功 NodeRun，绝不调用 Skill 或修改 checkpoint。"""
        plan_run = self._store.get_plan_run(plan_run_id)
        cards = (
            self._cards_snapshot(plan_run_id)
            if plan_run.state is PlanRunState.SUCCEEDED
            else ()
        )
        return self._outcome(
            plan_run_id=plan_run.plan_run_id,
            plan_version=plan_run.current_version,
            category=ReconciliationCategory.REPLAY_REUSE,
            plan_state=plan_run.state,
            cards_snapshot=cards,
            audit_summary={
                "checkpoint": "BEHIND",
                "reused_node_runs": len(cards),
            },
        )

    def _cards_snapshot(self, plan_run_id: str) -> tuple[Any, ...]:
        """从 COLLECT 的最新成功 NodeRun 提取卡片；缺失即视为权威证据不完整。"""
        collect_node = next(
            (
                node
                for node in self._store.list_nodes(plan_run_id)
                if node.logical_key == "collect-card-results"
            ),
            None,
        )
        if collect_node is None:
            raise ValueError("PlanStore 成功计划缺少 COLLECT_CARD_RESULTS 节点")
        successful_runs = [
            node_run
            for node_run in self._store.list_node_runs(
                plan_run_id,
                collect_node.node_id,
            )
            if node_run.state is PlanNodeState.SUCCEEDED
        ]
        if not successful_runs:
            raise ValueError("PlanStore 成功计划缺少 COLLECT_CARD_RESULTS NodeRun")
        output = successful_runs[-1].output
        if not isinstance(output, Mapping) or not isinstance(output.get("cards"), list):
            raise ValueError("COLLECT_CARD_RESULTS 输出缺少 cards 快照")
        return tuple(output["cards"])

    def _record_invariant(
        self,
        *,
        plan_run_id: str,
        reason: str,
        reference: PlanCheckpointReference | None,
    ) -> PlanReconciliationOutcome:
        """生成稳定签名并让 PlanStore 原子持久化事故与安全冻结。"""
        failure = {
            "category": ReconciliationCategory.INTERNAL_INVARIANT.value,
            "reason": reason,
            "checkpoint_reference": (
                None if reference is None else reference.model_dump(mode="json")
            ),
        }
        signature = sha256(
            json.dumps(
                failure,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        persisted = self._store.record_reconciliation_failure(
            plan_run_id=plan_run_id,
            failure=failure,
            signature=signature,
            now=self._aware_now(),
        )
        return self._outcome(
            plan_run_id=persisted.plan_run_id,
            plan_version=persisted.current_version,
            category=ReconciliationCategory.INTERNAL_INVARIANT,
            plan_state=persisted.state,
            audit_summary={"reason": reason},
            failure_signature=signature,
        )

    @staticmethod
    def _outcome(
        *,
        plan_run_id: str,
        plan_version: int,
        category: ReconciliationCategory,
        plan_state: PlanRunState,
        cards_snapshot: tuple[Any, ...] = (),
        audit_summary: dict[str, Any],
        failure_signature: str | None = None,
    ) -> PlanReconciliationOutcome:
        """集中构造冻结结果，避免不同恢复分支遗漏身份或审计字段。"""
        return PlanReconciliationOutcome(
            plan_run_id=plan_run_id,
            plan_version=plan_version,
            category=category,
            plan_state=plan_state,
            cards_snapshot=cards_snapshot,
            audit_summary=audit_summary,
            failure_signature=failure_signature,
        )

    def _aware_now(self) -> datetime:
        """读取并规范注入时钟，拒绝无法跨进程比较的 naive 时间。"""
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Reconciliation 时钟必须包含时区")
        return value.astimezone(timezone.utc)


__all__ = [
    "CheckpointControlPosition",
    "PlanCheckpointReference",
    "PlanReconciliationOutcome",
    "PlanReconciliationService",
    "RECONCILIATION_INTERVAL_SECONDS",
    "ReconciliationCategory",
]
