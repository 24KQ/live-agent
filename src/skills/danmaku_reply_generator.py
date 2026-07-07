"""Phase 2C 弹幕参考回复生成器。

本阶段回复使用固定模板，不接 LLM。输出必须定位为“主播参考话术”，不能自动
发送给观众，也不能替主播承诺价格、库存、物流时效或售后政策。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.skills.danmaku_aggregator import DanmakuQuestionCategory, DanmakuQuestionGroup


class DanmakuReply(BaseModel):
    """弹幕问题的参考回复。"""

    model_config = ConfigDict(frozen=True)

    room_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    category: DanmakuQuestionCategory
    summary: str = Field(..., min_length=1)
    reply_text: str = Field(..., min_length=1)
    risk_tips: list[str] = Field(default_factory=list)
    confidence: float = Field(..., ge=0, le=1)
    requires_human_review: bool


_TEMPLATES: dict[DanmakuQuestionCategory, tuple[str, list[str], float, bool]] = {
    DanmakuQuestionCategory.PRICE: (
        "大家问价格比较多，主播可以提示：这款价格请以直播间当前展示为准，有变化我会马上口播同步。",
        ["不要承诺低于页面展示的价格", "如有优惠叠加，需以当前活动规则为准"],
        0.88,
        False,
    ),
    DanmakuQuestionCategory.STOCK: (
        "库存问题较集中，主播可以提示：当前库存以页面展示为准，想要的朋友建议尽快拍下。",
        ["不要承诺长期有货", "售罄或补货信息需以系统状态为准"],
        0.84,
        False,
    ),
    DanmakuQuestionCategory.PROMOTION: (
        "优惠问题较多，主播可以提示：大家先看直播间可领取的券和活动说明，能用的优惠我会逐个提醒。",
        ["不要虚构不存在的优惠", "优惠门槛和有效期需以页面规则为准"],
        0.82,
        False,
    ),
    DanmakuQuestionCategory.LOGISTICS: (
        "物流问题较多，主播可以提示：发货和到货时间以订单页展示为准，不同地区可能会有差异。",
        ["不要承诺确定到货日期", "偏远地区和特殊天气需人工确认"],
        0.78,
        False,
    ),
    DanmakuQuestionCategory.USAGE: (
        "使用方法问题较多，主播可以结合当前商品演示核心步骤，并提醒大家以商品说明为准。",
        ["涉及安全或安装步骤时需谨慎说明", "不确定的适用场景交给人工确认"],
        0.76,
        False,
    ),
    DanmakuQuestionCategory.AFTER_SALES: (
        "售后问题较多，主播可以提示：售后政策请以店铺和平台规则为准，有具体订单问题建议联系客服。",
        ["不要替平台或商家承诺超出规则的售后", "具体订单问题必须引导客服处理"],
        0.74,
        False,
    ),
    DanmakuQuestionCategory.GENERAL: (
        "有一类问题暂时无法稳定归类，建议主播先复述观众问题，再人工确认后回答。",
        ["需要人工确认后再回复", "不要根据模板编造不确定答案"],
        0.55,
        True,
    ),
}


def generate_danmaku_reply(group: DanmakuQuestionGroup) -> DanmakuReply:
    """根据聚合问题生成确定性参考回复。"""

    reply_text, risk_tips, confidence, requires_human_review = _TEMPLATES[group.category]
    return DanmakuReply(
        room_id=group.room_id,
        trace_id=group.trace_id,
        category=group.category,
        summary=group.summary,
        reply_text=reply_text,
        risk_tips=risk_tips,
        confidence=confidence,
        requires_human_review=requires_human_review,
    )
