"""Phase 2C 弹幕参考回复生成测试。

本阶段不接 LLM，回复生成必须是确定性的模板逻辑。这样测试稳定，也能确保
回复只作为主播参考，不会替主播自动承诺价格、库存或售后政策。
"""

from datetime import datetime, timezone

from src.skills.danmaku_aggregator import DanmakuQuestionCategory, DanmakuQuestionGroup
from src.skills.danmaku_reply_generator import generate_danmaku_reply


def make_group(category: DanmakuQuestionCategory, count: int = 3) -> DanmakuQuestionGroup:
    """构造聚合问题分组，供回复生成器单元测试使用。"""

    return DanmakuQuestionGroup(
        room_id="room-demo-001",
        trace_id="trace-danmaku-reply",
        category=category,
        summary="测试问题",
        count=count,
        sample_contents=["测试弹幕"],
        window_start=datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 7, 20, 0, 5, tzinfo=timezone.utc),
    )


def test_generate_danmaku_reply_for_price_question_is_stable_reference_text() -> None:
    """价格问题应生成稳定参考话术，并提醒以直播间当前展示为准。"""

    reply = generate_danmaku_reply(make_group(DanmakuQuestionCategory.PRICE))

    assert reply.category == DanmakuQuestionCategory.PRICE
    assert "价格" in reply.reply_text
    assert "直播间当前展示" in reply.reply_text
    assert reply.requires_human_review is False
    assert reply.confidence >= 0.8


def test_generate_danmaku_reply_for_general_question_requires_human_review() -> None:
    """未命中明确分类的问题应提示主播人工确认，避免系统编造不确定答案。"""

    reply = generate_danmaku_reply(make_group(DanmakuQuestionCategory.GENERAL, count=1))

    assert reply.category == DanmakuQuestionCategory.GENERAL
    assert reply.requires_human_review is True
    assert reply.confidence < 0.7
    assert any("人工确认" in tip for tip in reply.risk_tips)
