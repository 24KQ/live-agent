"""Phase 12B Event Inbox 与 EventApplication 显式状态机。

状态转移集中在本模块，Store 只能调用白名单函数推进记录。冲突投递属于事件事实
校验失败，可由登记事务直接把 Inbox 收敛到 ``CONFLICT``；其他业务推进都必须遵循
这里的普通状态边，避免 Worker 或 Adapter 自行发明隐式状态。
"""

from __future__ import annotations

from enum import StrEnum


class EventStateTransitionError(ValueError):
    """事件或 Application 请求了未被设计允许的状态转移。"""


class EventInboxState(StrEnum):
    """Event Inbox 事实从接收到终态的受控状态集合。"""

    RECEIVED = "RECEIVED"
    VERIFIED = "VERIFIED"
    CONFLICT = "CONFLICT"
    PROCESSING = "PROCESSING"
    WAITING_HUMAN = "WAITING_HUMAN"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


class EventOccurrenceKind(StrEnum):
    """一次传输投递相对首次事件事实的分类。"""

    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"


class EventApplicationState(StrEnum):
    """一个事件应用到一个 root plan 的受控处理阶段。"""

    PENDING = "PENDING"
    FREEZING = "FREEZING"
    EMERGENCY_RUNNING = "EMERGENCY_RUNNING"
    WAITING_RECONCILIATION = "WAITING_RECONCILIATION"
    REPLAN_READY = "REPLAN_READY"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


_INBOX_TRANSITIONS: dict[EventInboxState, frozenset[EventInboxState]] = {
    EventInboxState.RECEIVED: frozenset(
        {
            EventInboxState.VERIFIED,
            EventInboxState.CONFLICT,
            EventInboxState.FAILED,
        }
    ),
    EventInboxState.VERIFIED: frozenset(
        {
            EventInboxState.PROCESSING,
            EventInboxState.CONFLICT,
            EventInboxState.WAITING_HUMAN,
            EventInboxState.FAILED,
        }
    ),
    EventInboxState.CONFLICT: frozenset(
        {
            EventInboxState.WAITING_HUMAN,
            EventInboxState.FAILED,
        }
    ),
    EventInboxState.PROCESSING: frozenset(
        {
            # 可恢复处理失败时先回到 VERIFIED，再由下一次 claim 生成新 fencing token。
            EventInboxState.VERIFIED,
            EventInboxState.CONFLICT,
            EventInboxState.WAITING_HUMAN,
            EventInboxState.APPLIED,
            EventInboxState.FAILED,
        }
    ),
    EventInboxState.WAITING_HUMAN: frozenset(
        {
            EventInboxState.VERIFIED,
            EventInboxState.APPLIED,
            EventInboxState.FAILED,
        }
    ),
    EventInboxState.APPLIED: frozenset(),
    EventInboxState.FAILED: frozenset(),
}


_APPLICATION_TRANSITIONS: dict[
    EventApplicationState,
    frozenset[EventApplicationState],
] = {
    EventApplicationState.PENDING: frozenset(
        {
            EventApplicationState.FREEZING,
            EventApplicationState.WAITING_RECONCILIATION,
            EventApplicationState.FAILED,
        }
    ),
    EventApplicationState.FREEZING: frozenset(
        {
            EventApplicationState.EMERGENCY_RUNNING,
            EventApplicationState.WAITING_RECONCILIATION,
            EventApplicationState.FAILED,
        }
    ),
    EventApplicationState.EMERGENCY_RUNNING: frozenset(
        {
            EventApplicationState.WAITING_RECONCILIATION,
            EventApplicationState.REPLAN_READY,
            EventApplicationState.FAILED,
        }
    ),
    EventApplicationState.WAITING_RECONCILIATION: frozenset(
        {
            EventApplicationState.EMERGENCY_RUNNING,
            EventApplicationState.REPLAN_READY,
            EventApplicationState.FAILED,
        }
    ),
    EventApplicationState.REPLAN_READY: frozenset(
        {
            EventApplicationState.APPLIED,
            EventApplicationState.FAILED,
        }
    ),
    EventApplicationState.APPLIED: frozenset(),
    EventApplicationState.FAILED: frozenset(),
}


def assert_inbox_transition(
    current: EventInboxState,
    target: EventInboxState,
) -> None:
    """校验普通 Inbox 转移；非法边不做自动修正或隐式跳转。"""
    if target not in _INBOX_TRANSITIONS[current]:
        raise EventStateTransitionError(
            f"非法 EventInbox 状态转移: {current.value} -> {target.value}"
        )


def assert_application_transition(
    current: EventApplicationState,
    target: EventApplicationState,
) -> None:
    """校验 EventApplication 转移并拒绝终态回退。"""
    if target not in _APPLICATION_TRANSITIONS[current]:
        raise EventStateTransitionError(
            f"非法 EventApplication 状态转移: {current.value} -> {target.value}"
        )
