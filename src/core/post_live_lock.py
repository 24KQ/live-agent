"""Phase 4A 播后写操作锁定。

进入 POST_LIVE 后，所有业务写操作（改价/切品/发券/建播）强制 block。
不受 trust_score 影响——播后锁定是生命周期级别的硬约束。
"""

from __future__ import annotations

from src.state.models import LifecycleStage


# POST_LIVE 中必须被 block 的业务写操作工具名
POST_LIVE_BLOCKED_TOOLS: set[str] = {
    "set_product_price",
    "switch_product",
    "setup_live_session",
    "generate_live_plan",
}


def is_post_live_blocked(tool_name: str, stage: LifecycleStage) -> bool:
    """判断指定工具在 POST_LIVE 阶段是否应被锁定。

    工具不在 POST_LIVE_BLOCKED_TOOLS 中时永远返回 False（放行）。
    """
    if stage != LifecycleStage.POST_LIVE:
        return False
    return tool_name in POST_LIVE_BLOCKED_TOOLS


def post_live_tool_mask(
    tool_name: str,
    stage: LifecycleStage,
    trust_score: float,
) -> str:
    """返回工具在当前生命周期的掩码级别。

    返回 "block" 表示该工具不可见/不可执行；
    返回 "visible" 表示该工具正常可用。
    """
    if is_post_live_blocked(tool_name, stage):
        return "block"
    return "visible"
