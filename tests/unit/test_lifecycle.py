"""生命周期状态机测试。

生命周期控制哪些工具可见、哪些动作可执行。Phase 1 先把
PRE_LIVE -> ON_LIVE -> POST_LIVE -> PRE_LIVE 的合法路径固定下来。
"""

import pytest

from src.core.lifecycle import LifecycleTransitionError, transition_lifecycle
from src.state.models import LifecycleStage


def test_lifecycle_allows_ordered_transitions() -> None:
    """合法生命周期应能按直播流程顺序切换。"""

    assert transition_lifecycle(LifecycleStage.PRE_LIVE, LifecycleStage.ON_LIVE) == LifecycleStage.ON_LIVE
    assert transition_lifecycle(LifecycleStage.ON_LIVE, LifecycleStage.POST_LIVE) == LifecycleStage.POST_LIVE
    assert transition_lifecycle(LifecycleStage.POST_LIVE, LifecycleStage.PRE_LIVE) == LifecycleStage.PRE_LIVE


def test_lifecycle_rejects_skipped_transition() -> None:
    """跳过中间阶段的生命周期切换必须被拒绝。"""

    with pytest.raises(LifecycleTransitionError):
        transition_lifecycle(LifecycleStage.PRE_LIVE, LifecycleStage.POST_LIVE)


def test_lifecycle_rejects_same_stage_transition() -> None:
    """重复切换到当前阶段会隐藏调用方错误，应 fail-closed。"""

    with pytest.raises(LifecycleTransitionError):
        transition_lifecycle(LifecycleStage.PRE_LIVE, LifecycleStage.PRE_LIVE)
