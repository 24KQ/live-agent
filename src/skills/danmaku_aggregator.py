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


def aggregate_with_semantic_fallback(
    events: list[DanmakuEvent],
    window_seconds: int = 5,
    clusterer: Any = None,
    llm_fallback: Any = None,
) -> list[DanmakuQuestionGroup]:
    """带语义聚类和 LLM 兜底的弹幕聚合。

    流程：
    1. 先用关键词分类（复用 aggregate_danmaku_questions）
    2. 收集所有 GENERAL 分类的弹幕
    3. 若 >= 5 条，先做语义聚类（DanmakuSemanticClusterer）
    4. 再对聚类后的未分类弹幕做 LLM 兜底（DanmakuLLMFallback）
    5. LLM 返回的分类合并到最终结果

    参数:
        events: 弹幕事件列表（同一直播间、同一 trace）
        window_seconds: 时间窗口
        clusterer: DanmakuSemanticClusterer 实例，为 None 时跳过语义聚类
        llm_fallback: DanmakuLLMFallback 实例，为 None 时跳过 LLM 兜底

    返回:
        聚合后的弹幕问题分组列表
    """
    if not events:
        raise ValueError("danmaku events cannot be empty")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be greater than 0")

    # 第一步：关键词分类聚合
    keyword_groups = aggregate_danmaku_questions(events, window_seconds=window_seconds)

    # 第二步：收集 GENERAL 弹幕内容
    general_contents: list[str] = []
    for event in events:
        if classify_danmaku_question(event.content) == DanmakuQuestionCategory.GENERAL:
            general_contents.append(event.content)

    if not general_contents:
        return keyword_groups

    # 第三步：语义聚类
    clustered_messages: list[str] = []
    if clusterer is not None and len(general_contents) >= 5:
        try:
            cluster_results = clusterer.cluster(general_contents, threshold=0.75)
            # 取每簇的代表消息（簇中第一条）
            for cr in cluster_results:
                if cr.messages:
                    clustered_messages.append(cr.messages[0])
        except Exception:
            # 聚类失败时用原始列表
            clustered_messages = general_contents
    else:
        clustered_messages = general_contents

    # 第四步：LLM 兜底分类
    llm_results: list[dict] = []
    if llm_fallback is not None and len(clustered_messages) >= 5:
        try:
            llm_results = llm_fallback.classify_unclassified(clustered_messages)
        except Exception:
            llm_results = []

    # 第五步：将 LLM 分类结果合并到 keyword_groups
    if llm_results:
        # 从 keyword_groups 中移除原有的 GENERAL 分组
        filtered_groups = [g for g in keyword_groups if g.category != DanmakuQuestionCategory.GENERAL]
        general_groups = [g for g in keyword_groups if g.category == DanmakuQuestionCategory.GENERAL]

        # 统计原有 GENERAL 条数
        original_general_count = sum(g.count for g in general_groups)

        # 按 LLM 分类重新分组
        llm_by_category: dict[DanmakuQuestionCategory, list[str]] = {}
        for item in llm_results:
            cat = item["category"]
            content = item["content"]
            if cat not in llm_by_category:
                llm_by_category[cat] = []
            llm_by_category[cat].append(content)

        # 添加 LLM 分类后的新分组
        for cat, contents in llm_by_category.items():
            filtered_groups.append(DanmakuQuestionGroup(
                room_id=events[0].room_id,
                trace_id=events[0].trace_id,
                category=cat,
                summary=_CATEGORY_SUMMARIES.get(cat, cat.value),
                count=len(contents),
                sample_contents=contents[:3],
                window_start=keyword_groups[0].window_start if keyword_groups else events[0].event_time,
                window_end=keyword_groups[0].window_end if keyword_groups else events[0].event_time,
            ))

        # 如果有未被 LLM 覆盖的 GENERAL（LLM 没返回的分类），保留
        llm_covered = sum(len(v) for v in llm_by_category.values())
        remaining_general = original_general_count - llm_covered
        if remaining_general > 0 and general_groups:
            filtered_groups.append(DanmakuQuestionGroup(
                room_id=events[0].room_id,
                trace_id=events[0].trace_id,
                category=DanmakuQuestionCategory.GENERAL,
                summary=_CATEGORY_SUMMARIES[DanmakuQuestionCategory.GENERAL],
                count=remaining_general,
                sample_contents=general_groups[0].sample_contents,
                window_start=general_groups[0].window_start,
                window_end=general_groups[0].window_end,
            ))

        # 按窗口时间排序
        filtered_groups.sort(key=lambda g: (g.window_start, g.category.value))
        return filtered_groups

    return keyword_groups
