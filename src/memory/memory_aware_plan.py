"""Phase 3A 记忆感知播前排品。

本模块复用 Phase 2A 的确定性排品结果，再用主播记忆做轻量、可解释的排序调整。
它不调用 LLM、不做语义检索，只读取结构化 metadata 和记忆正文中的显式关键词。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.memory.memory_store import MemoryStore
from src.memory.memory_retrieval import MemoryHit, MemoryRetriever, rank_memory_hits
from src.memory.models import AnchorMemoryEntry
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem, generate_live_plan
from src.skills.product_catalog import CatalogProduct


@dataclass(frozen=True)
class MemoryInfluence:
    """单个商品受到记忆影响后的评分摘要。"""

    score: Decimal
    reasons: list[str]


class MemoryAwarePlanService:
    """读取主播记忆并生成记忆感知排品。"""

    def __init__(self, memory_store: MemoryStore) -> None:
        self._memory_store = memory_store

    def generate_plan(
        self,
        anchor_id: str,
        room_id: str,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> LivePlanDraft:
        """查询主播记忆后生成排品。

        这里优先查询与当前直播间绑定的记忆，同时保留 anchor 级长期记忆；Store 会按
        created_at 排序，排品层只关心内容和权重。
        """

        # Phase 3B 起优先使用增强检索。检索层负责衰减、状态压低和脱敏解释，
        # 排品层只消费结构化命中结果，避免把 Store 的数据库排序当作业务排序。
        memory_hits = MemoryRetriever(self._memory_store).retrieve(anchor_id=anchor_id, room_id=room_id)
        return apply_memory_hits_to_live_plan(
            room_id=room_id,
            products=products,
            trace_id=trace_id,
            memory_hits=memory_hits,
        )


def apply_memory_to_live_plan(
    room_id: str,
    products: list[CatalogProduct],
    trace_id: str,
    memories: list[AnchorMemoryEntry],
) -> LivePlanDraft:
    """在既有排品基础上叠加记忆影响。

    如果没有命中任何记忆，直接返回 Phase 2A 的原始排品，保证新增能力不会改变无记忆场景。
    如果有命中，按“记忆分高优先、原始排品顺序兜底”的规则重排，并把命中原因写入 reason。
    """

    if not memories:
        return generate_live_plan(room_id=room_id, products=products, trace_id=trace_id)
    return apply_memory_hits_to_live_plan(
        room_id=room_id,
        products=products,
        trace_id=trace_id,
        memory_hits=rank_memory_hits(memories, room_id=room_id),
    )


def apply_memory_hits_to_live_plan(
    room_id: str,
    products: list[CatalogProduct],
    trace_id: str,
    memory_hits: list[MemoryHit],
) -> LivePlanDraft:
    """在既有排品基础上叠加增强检索命中。

    该入口消费 Phase 3B 的 `MemoryHit`，使用已完成衰减计算的 `effective_weight`。
    suppressed 记忆仍会被纳入审计解释，但权重已经很低，不应继续主导排品。
    """

    base_plan = generate_live_plan(room_id=room_id, products=products, trace_id=trace_id)
    if not memory_hits:
        return base_plan

    base_items = {item.product_id: item for item in base_plan.items}
    base_rank = {item.product_id: item.rank for item in base_plan.items}
    influences = {
        product.product_id: _calculate_memory_hit_influence(product, memory_hits)
        for product in products
    }

    if all(influence.score == Decimal("0.00") for influence in influences.values()):
        return base_plan

    ordered_products = sorted(
        products,
        key=lambda product: (
            -influences[product.product_id].score,
            base_rank.get(product.product_id, 9999),
            product.product_id,
        ),
    )
    items: list[LivePlanItem] = []
    for rank, product in enumerate(ordered_products, start=1):
        base_item = base_items[product.product_id]
        influence = influences[product.product_id]
        reason = base_item.reason
        if influence.reasons:
            reason = f"{reason} 记忆影响：{'；'.join(influence.reasons[:2])}"
        items.append(
            LivePlanItem(
                rank=rank,
                product_id=product.product_id,
                product_name=base_item.product_name,
                role=base_item.role,
                reason=reason,
            )
        )

    # 使用 LivePlanDraft 重新封装，保证返回类型和 Phase 2A 完全兼容。
    return LivePlanDraft(room_id=room_id, trace_id=trace_id, items=items)


def _calculate_memory_influence(
    product: CatalogProduct,
    memories: list[AnchorMemoryEntry],
) -> MemoryInfluence:
    """计算单个商品的记忆加权分。

    保留该函数是为了兼容 Phase 3A 期间的内部调用习惯；Phase 3B 的公开入口会先把
    原始记忆转换成 MemoryHit，再使用衰减后的有效权重计算影响。
    """

    return _calculate_memory_hit_influence(product, rank_memory_hits(memories, room_id=""))


def _calculate_memory_hit_influence(
    product: CatalogProduct,
    memory_hits: list[MemoryHit],
) -> MemoryInfluence:
    """计算单个商品受到增强检索命中的加权分。"""

    score = Decimal("0.00")
    reasons: list[str] = []
    for hit in memory_hits:
        memory = hit.memory
        weight = hit.effective_weight
        metadata = memory.metadata
        preferred_product_ids = _as_list(metadata.get("preferred_product_ids") or metadata.get("preferred_product_id"))
        preferred_categories = _as_list(metadata.get("preferred_categories") or metadata.get("preferred_category"))
        preferred_tags = _as_list(metadata.get("preferred_tags") or metadata.get("preferred_tag"))

        matched_fields: list[str] = []
        if product.product_id in preferred_product_ids:
            score += Decimal("30.00") * weight
            matched_fields.append(f"商品ID={product.product_id}")
        if product.category in preferred_categories:
            score += Decimal("20.00") * weight
            matched_fields.append(f"类目={product.category}")
        if set(product.tags).intersection(preferred_tags):
            matched_tags = sorted(set(product.tags).intersection(preferred_tags))
            score += Decimal("10.00") * weight
            matched_fields.append(f"标签={','.join(matched_tags)}")
        if product.category and product.category in memory.content:
            score += Decimal("5.00") * weight
            matched_fields.append(f"正文类目={product.category}")

        if matched_fields:
            # 面向主播和审计的解释只输出结构化命中摘要，不直接回显 memory.content，避免未来
            # 记忆内容包含敏感文本时经由排品理由扩散。
            reasons.append(f"{hit.explanation}；商品匹配 {'、'.join(matched_fields)}")
    return MemoryInfluence(score=score.quantize(Decimal("0.01")), reasons=reasons)


def _as_list(value: object) -> list[str]:
    """把 metadata 中可能出现的字符串或数组统一转换成字符串列表。"""

    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]
