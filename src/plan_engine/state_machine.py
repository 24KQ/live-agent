"""Phase 12A D-015 节点状态迁移的唯一验证入口。"""

from __future__ import annotations

from collections.abc import Mapping

from src.plan_engine.models import PlanNodeState, PlanRunState


class PlanInvariantError(RuntimeError):
    """表示 Store、Worker 或命令层试图破坏已冻结的计划状态不变量。"""


class PlanStateMachine:
    """集中维护 D-015 的有向状态图，禁止调用方自行推断可达终态。

    所有状态写入都应先经过此类；这让未来 Store 的并发 claim、超时回收和人工命令
    共享同一份白名单，而非在多个路径中出现语义不同的 if/else 分支。
    """

    _ALLOWED_NODE_TRANSITIONS: Mapping[PlanNodeState, frozenset[PlanNodeState]] = {
        PlanNodeState.PENDING: frozenset(
            {
                PlanNodeState.READY,
                PlanNodeState.FROZEN,
                PlanNodeState.INVALIDATED,
                PlanNodeState.SKIPPED,
            }
        ),
        PlanNodeState.READY: frozenset(
            {
                PlanNodeState.RUNNING,
                PlanNodeState.FROZEN,
                PlanNodeState.INVALIDATED,
                PlanNodeState.SKIPPED,
            }
        ),
        PlanNodeState.RUNNING: frozenset(
            {
                PlanNodeState.SUCCEEDED,
                PlanNodeState.FAILED,
                PlanNodeState.RETRY_WAIT,
                PlanNodeState.WAITING_APPROVAL,
                PlanNodeState.WAITING_RECONCILIATION,
                PlanNodeState.FROZEN,
            }
        ),
        PlanNodeState.RETRY_WAIT: frozenset(
            {PlanNodeState.READY, PlanNodeState.FROZEN, PlanNodeState.FAILED}
        ),
        PlanNodeState.WAITING_APPROVAL: frozenset(
            {PlanNodeState.READY, PlanNodeState.FROZEN, PlanNodeState.FAILED}
        ),
        PlanNodeState.WAITING_RECONCILIATION: frozenset(
            {PlanNodeState.SUCCEEDED, PlanNodeState.FAILED}
        ),
        PlanNodeState.FROZEN: frozenset(
            {PlanNodeState.INVALIDATED, PlanNodeState.SKIPPED}
        ),
    }

    @classmethod
    def transition_node(
        cls,
        current: PlanNodeState | str,
        target: PlanNodeState | str,
    ) -> PlanNodeState:
        """验证一条节点状态边；合法时返回规范枚举，非法时 fail-closed。"""
        current_state = cls._coerce_node_state(current, "当前")
        target_state = cls._coerce_node_state(target, "目标")
        if target_state not in cls._ALLOWED_NODE_TRANSITIONS.get(current_state, frozenset()):
            raise PlanInvariantError(
                f"节点状态不允许从 {current_state.value} 迁移到 {target_state.value}"
            )
        return target_state

    @staticmethod
    def _coerce_node_state(value: PlanNodeState | str, label: str) -> PlanNodeState:
        """拒绝未知字符串，防止存储层把拼写错误转成新的隐式状态。"""
        try:
            return PlanNodeState(value)
        except (TypeError, ValueError) as exc:
            raise PlanInvariantError(f"{label}节点状态非法: {value}") from exc


def validate_plan_run_state(value: PlanRunState | str) -> PlanRunState:
    """验证 PlanRun 聚合状态只属于 ACTIVE/FROZEN/SUCCEEDED/FAILED 四种事实。"""
    try:
        return PlanRunState(value)
    except (TypeError, ValueError) as exc:
        raise PlanInvariantError(f"PlanRun 聚合状态非法: {value}") from exc
