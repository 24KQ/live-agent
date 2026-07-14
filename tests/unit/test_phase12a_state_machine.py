"""Phase 12A D-015 与 Phase 12B 协作冻结的集中状态机契约测试。"""

from itertools import product

import pytest

from src.plan_engine.models import PlanNodeState, PlanRunState
from src.plan_engine.state_machine import (
    PlanInvariantError,
    PlanStateMachine,
    validate_plan_run_state,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (PlanNodeState.PENDING, PlanNodeState.READY),
        (PlanNodeState.READY, PlanNodeState.RUNNING),
        (PlanNodeState.RUNNING, PlanNodeState.SUCCEEDED),
        (PlanNodeState.RUNNING, PlanNodeState.FAILED),
        (PlanNodeState.RUNNING, PlanNodeState.RETRY_WAIT),
        (PlanNodeState.RUNNING, PlanNodeState.WAITING_APPROVAL),
        (PlanNodeState.RUNNING, PlanNodeState.WAITING_RECONCILIATION),
        (PlanNodeState.RUNNING, PlanNodeState.FROZEN),
        (PlanNodeState.PENDING, PlanNodeState.FROZEN),
        (PlanNodeState.READY, PlanNodeState.FROZEN),
        (PlanNodeState.RETRY_WAIT, PlanNodeState.READY),
        (PlanNodeState.RETRY_WAIT, PlanNodeState.FROZEN),
        (PlanNodeState.RETRY_WAIT, PlanNodeState.FAILED),
        (PlanNodeState.WAITING_APPROVAL, PlanNodeState.READY),
        (PlanNodeState.WAITING_APPROVAL, PlanNodeState.FROZEN),
        (PlanNodeState.WAITING_APPROVAL, PlanNodeState.FAILED),
        (PlanNodeState.WAITING_RECONCILIATION, PlanNodeState.SUCCEEDED),
        (PlanNodeState.WAITING_RECONCILIATION, PlanNodeState.FAILED),
        (PlanNodeState.PENDING, PlanNodeState.INVALIDATED),
        (PlanNodeState.READY, PlanNodeState.SKIPPED),
        (PlanNodeState.FROZEN, PlanNodeState.INVALIDATED),
    ],
)
def test_state_machine_allows_only_registered_transitions(
    current: PlanNodeState,
    target: PlanNodeState,
) -> None:
    """D-015 与协作冻结新增的每条白名单边都必须由集中状态机接受。"""
    assert PlanStateMachine.transition_node(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (PlanNodeState.PENDING, PlanNodeState.SKIPPED),
        (PlanNodeState.READY, PlanNodeState.INVALIDATED),
        (PlanNodeState.FROZEN, PlanNodeState.SKIPPED),
    ],
)
def test_state_machine_allows_remaining_d015_invalidation_edges(
    current: PlanNodeState,
    target: PlanNodeState,
) -> None:
    """PENDING、READY、FROZEN 都必须完整支持 INVALIDATED 与 SKIPPED 两条边。"""
    assert PlanStateMachine.transition_node(current, target) is target


_ALLOWED_TRANSITIONS: frozenset[tuple[PlanNodeState, PlanNodeState]] = frozenset(
    {
        (PlanNodeState.PENDING, PlanNodeState.READY),
        (PlanNodeState.PENDING, PlanNodeState.FROZEN),
        (PlanNodeState.PENDING, PlanNodeState.INVALIDATED),
        (PlanNodeState.PENDING, PlanNodeState.SKIPPED),
        (PlanNodeState.READY, PlanNodeState.RUNNING),
        (PlanNodeState.READY, PlanNodeState.FROZEN),
        (PlanNodeState.READY, PlanNodeState.INVALIDATED),
        (PlanNodeState.READY, PlanNodeState.SKIPPED),
        (PlanNodeState.RUNNING, PlanNodeState.SUCCEEDED),
        (PlanNodeState.RUNNING, PlanNodeState.FAILED),
        (PlanNodeState.RUNNING, PlanNodeState.RETRY_WAIT),
        (PlanNodeState.RUNNING, PlanNodeState.WAITING_APPROVAL),
        (PlanNodeState.RUNNING, PlanNodeState.WAITING_RECONCILIATION),
        (PlanNodeState.RUNNING, PlanNodeState.FROZEN),
        (PlanNodeState.RETRY_WAIT, PlanNodeState.READY),
        (PlanNodeState.RETRY_WAIT, PlanNodeState.FROZEN),
        (PlanNodeState.RETRY_WAIT, PlanNodeState.FAILED),
        (PlanNodeState.WAITING_APPROVAL, PlanNodeState.READY),
        (PlanNodeState.WAITING_APPROVAL, PlanNodeState.FROZEN),
        (PlanNodeState.WAITING_APPROVAL, PlanNodeState.FAILED),
        (PlanNodeState.WAITING_RECONCILIATION, PlanNodeState.SUCCEEDED),
        (PlanNodeState.WAITING_RECONCILIATION, PlanNodeState.FAILED),
        (PlanNodeState.FROZEN, PlanNodeState.INVALIDATED),
        (PlanNodeState.FROZEN, PlanNodeState.SKIPPED),
    }
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current, target in product(PlanNodeState, repeat=2)
        if (current, target) not in _ALLOWED_TRANSITIONS
    ],
)
def test_state_machine_rejects_every_unregistered_transition(
    current: PlanNodeState,
    target: PlanNodeState,
) -> None:
    """穷举 11x11 状态矩阵补集，继续拒绝协作冻结之外的未审查迁移边。"""
    with pytest.raises(PlanInvariantError, match="不允许"):
        PlanStateMachine.transition_node(current, target)


def test_plan_run_state_excludes_partial_success() -> None:
    """聚合状态只表达四种运行结论，首期不得以部分成功掩盖失败节点。"""
    assert validate_plan_run_state(PlanRunState.ACTIVE) is PlanRunState.ACTIVE
    with pytest.raises(PlanInvariantError, match="PlanRun"):
        validate_plan_run_state("PARTIAL_SUCCESS")
