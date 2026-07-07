"""直播生命周期状态机。

生命周期决定工具可用范围。Phase 1 采用 fail-closed 策略：只有明确写入
状态机的跳转才允许执行，跳阶段、重复切换或未知值都会被拒绝。
"""

from src.state.models import LifecycleStage


class LifecycleTransitionError(ValueError):
    """生命周期非法切换错误。"""


ALLOWED_TRANSITIONS: dict[LifecycleStage, LifecycleStage] = {
    LifecycleStage.PRE_LIVE: LifecycleStage.ON_LIVE,
    LifecycleStage.ON_LIVE: LifecycleStage.POST_LIVE,
    LifecycleStage.POST_LIVE: LifecycleStage.PRE_LIVE,
}


def transition_lifecycle(current: LifecycleStage, target: LifecycleStage) -> LifecycleStage:
    """执行生命周期切换。

    返回目标阶段表示切换成功；抛出 LifecycleTransitionError 表示调用方
    试图越过流程边界，业务层应停止后续工具调用。
    """

    expected_target = ALLOWED_TRANSITIONS.get(current)
    if expected_target != target:
        raise LifecycleTransitionError(f"cannot transition lifecycle from {current} to {target}")
    return target
