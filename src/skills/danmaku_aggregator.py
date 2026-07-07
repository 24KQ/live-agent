"""Phase 2C 弹幕问题聚合器。

聚合器使用确定性关键词规则，不调用 LLM、不做向量检索。这样做的好处是：
播中高频事件处理足够快、测试结果稳定、后续接入 Kafka 后也能先用规则层兜底。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from src.skills.danmaku_events import DanmakuEvent


class DanmakuQuestionCategory(StrEnum):
    """弹幕问题的确定性分类。"""

    PRICE = "price"
    STOCK = "stock"
    PROMOTION = "promotion"
    LOGISTICS = "logistics"
    USAGE = "usage"
    AFTER_SALES = "after_sales"
    GENERAL = "general"


class DanmakuQuestionGroup(BaseModel):
    """同一时间窗口内的同类弹幕问题摘要。"""

    model_config = ConfigDict(frozen=True)

    room_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    category: DanmakuQuestionCategory
    summary: str = Field(..., min_length=1)
    count: int = Field(..., ge=1)
    sample_contents: list[str] = Field(default_factory=list)
    window_start: datetime
    window_end: datetime


@dataclass
class _MutableGroup:
    """聚合过程中的可变暂存结构。

    对外返回的 `DanmakuQuestionGroup` 是 frozen 模型；内部先用可变结构累加计数
    和样例内容，最后再一次性转换，避免在循环里反复复制 Pydantic 对象。
    """

    room_id: str
    trace_id: str
    category: DanmakuQuestionCategory
    window_start: datetime
    window_end: datetime
    count: int = 0
    sample_contents: list[str] = field(default_factory=list)


_CATEGORY_RULES: list[tuple[DanmakuQuestionCategory, tuple[str, ...]]] = [
    (DanmakuQuestionCategory.PRICE, ("价格", "多少钱", "多少米", "几块", "贵", "便宜")),
    (DanmakuQuestionCategory.STOCK, ("库存", "有货", "还有吗", "还有没有", "卖完", "售罄", "缺货")),
    (DanmakuQuestionCategory.PROMOTION, ("优惠", "券", "活动", "满减", "折扣", "赠品", "福利")),
    (DanmakuQuestionCategory.LOGISTICS, ("发货", "快递", "物流", "包邮", "几天到", "运费")),
    (DanmakuQuestionCategory.USAGE, ("怎么用", "使用", "教程", "适合", "用法", "安装")),
    (DanmakuQuestionCategory.AFTER_SALES, ("售后", "退货", "退款", "保修", "换货", "质量问题")),
]

_CATEGORY_SUMMARIES: dict[DanmakuQuestionCategory, str] = {
    DanmakuQuestionCategory.PRICE: "价格相关问题",
    DanmakuQuestionCategory.STOCK: "库存相关问题",
    DanmakuQuestionCategory.PROMOTION: "优惠活动相关问题",
    DanmakuQuestionCategory.LOGISTICS: "物流发货相关问题",
    DanmakuQuestionCategory.USAGE: "使用方法相关问题",
    DanmakuQuestionCategory.AFTER_SALES: "售后保障相关问题",
    DanmakuQuestionCategory.GENERAL: "通用问题",
}


def classify_danmaku_question(content: str) -> DanmakuQuestionCategory:
    """用关键词把弹幕内容映射到固定分类。

    分类顺序是业务优先级：价格、库存和优惠通常是直播间最高频问题，应先命中。
    未命中任何规则时归入 GENERAL，并在回复生成阶段要求人工复核。
    """

    normalized = content.strip().lower()
    for category, keywords in _CATEGORY_RULES:
        if any(keyword in normalized for keyword in keywords):
            return category
    return DanmakuQuestionCategory.GENERAL


def aggregate_danmaku_questions(events: list[DanmakuEvent], window_seconds: int = 5) -> list[DanmakuQuestionGroup]:
    """按固定时间窗口聚合同类弹幕问题。

    同一次聚合必须只处理一个直播间和一个 trace_id。这样审计链路才可回放，也能
    避免把不同直播间或不同批次的弹幕混在一起生成错误参考回复。
    """

    if not events:
        raise ValueError("danmaku events cannot be empty")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be greater than 0")

    room_ids = {event.room_id for event in events}
    if len(room_ids) != 1:
        raise ValueError("danmaku events must have the same room_id")

    trace_ids = {event.trace_id for event in events}
    if len(trace_ids) != 1:
        raise ValueError("danmaku events must have the same trace_id")

    ordered_events = sorted(events, key=lambda event: event.event_time)
    base_time = ordered_events[0].event_time
    groups: dict[tuple[int, DanmakuQuestionCategory], _MutableGroup] = {}

    for event in ordered_events:
        category = classify_danmaku_question(event.content)
        window_index = int((event.event_time - base_time).total_seconds() // window_seconds)
        key = (window_index, category)
        if key not in groups:
            window_start = base_time + timedelta(seconds=window_index * window_seconds)
            groups[key] = _MutableGroup(
                room_id=event.room_id,
                trace_id=event.trace_id,
                category=category,
                window_start=window_start,
                window_end=window_start + timedelta(seconds=window_seconds),
            )

        group = groups[key]
        group.count += 1
        if len(group.sample_contents) < 3:
            group.sample_contents.append(event.content)

    return [
        DanmakuQuestionGroup(
            room_id=group.room_id,
            trace_id=group.trace_id,
            category=group.category,
            summary=_CATEGORY_SUMMARIES[group.category],
            count=group.count,
            sample_contents=group.sample_contents,
            window_start=group.window_start,
            window_end=group.window_end,
        )
        for group in sorted(groups.values(), key=lambda item: (item.window_start, item.category.value))
    ]
