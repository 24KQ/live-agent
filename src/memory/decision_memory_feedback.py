"""把 Decision Trace 反馈归纳为 Phase 3B 结构化记忆。

本模块只白名单提取 recommendation 中的安全字段，不复制完整话术、主播原话、
订单信息或外部平台字段，避免把未来可能含敏感信息的 Trace 直接写入长期记忆。
"""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any

from src.memory.models import AnchorAction, AnchorMemoryEntry, BusinessResult, MemoryLayer, MemorySource
from src.skills.product_catalog import CatalogProduct


class DecisionTraceMemoryFeedbackService:
    """根据主播反馈和业务结果生成 L2 记忆。"""

    def __init__(self, memory_store=None) -> None:
        self._memory_store = memory_store

    def build_feedback_memory(
        self,
        *,
        trace_id: str,
        anchor_id: str,
        room_id: str,
        anchor_action: AnchorAction,
        business_result: BusinessResult,
        recommendation: dict[str, Any],
        lift: Decimal,
        catalog_products: list[CatalogProduct] | None = None,
    ) -> AnchorMemoryEntry:
        """把一次脱敏反馈构造成可写入的 L2 记忆。"""

        if not catalog_products:
            raise ValueError("catalog_products must be provided to build feedback memory")
        metadata = _safe_recommendation_metadata(recommendation, catalog_products=catalog_products)
        metadata.update(
            {
                "feedback_trace_id": trace_id,
                "anchor_action": anchor_action.value,
                "business_result": business_result.value,
                "lift": str(lift),
            }
        )
        confidence, evidence_weight = _feedback_strength(anchor_action, business_result)
        return AnchorMemoryEntry(
            memory_key=f"phase3b-feedback-{_safe_key(anchor_id)}-{_safe_key(room_id)}-{_safe_key(trace_id)}",
            anchor_id=anchor_id,
            room_id=room_id,
            layer=MemoryLayer.L2,
            content=_feedback_content(anchor_action, business_result),
            metadata=metadata,
            confidence=confidence,
            evidence_weight=evidence_weight,
            source=MemorySource.SYSTEM_OBSERVED,
        )

    def write_feedback_memory(self, memory: AnchorMemoryEntry) -> str:
        """写入反馈记忆；没有注入 Store 时明确失败，避免误以为已经落库。"""

        if self._memory_store is None:
            raise RuntimeError("DecisionTraceMemoryFeedbackService requires MemoryStore to write memory")
        return self._memory_store.write_memory(memory)


def _safe_recommendation_metadata(
    recommendation: dict[str, Any],
    *,
    catalog_products: list[CatalogProduct] | None,
) -> dict[str, Any]:
    """只提取允许进入长期记忆的结构化字段，并按货盘做值级过滤。"""

    allowed_categories, allowed_tags, allowed_product_ids = _catalog_allowlists(catalog_products)
    metadata: dict[str, Any] = {}
    category_values = _safe_values(
        recommendation.get("preferred_categories") or recommendation.get("preferred_category"),
        allowed_values=allowed_categories,
    )
    tag_values = _safe_values(
        recommendation.get("preferred_tags") or recommendation.get("preferred_tag"),
        allowed_values=allowed_tags,
    )
    product_values = _safe_values(
        recommendation.get("preferred_product_ids") or recommendation.get("preferred_product_id"),
        allowed_values=allowed_product_ids,
    )
    conflict_group = _safe_identifier(recommendation.get("conflict_group"))

    if category_values:
        metadata["preferred_category"] = category_values[0]
    if tag_values:
        metadata["preferred_tags"] = tag_values
    if product_values:
        metadata["preferred_product_ids"] = product_values
    if conflict_group:
        metadata["conflict_group"] = conflict_group
    return metadata


def _catalog_allowlists(catalog_products: list[CatalogProduct] | None) -> tuple[set[str], set[str], set[str]]:
    """从当前货盘中提取允许写入长期记忆的类目、标签和商品 ID。"""

    if not catalog_products:
        return set(), set(), set()
    categories = {product.category for product in catalog_products}
    tags = {tag for product in catalog_products for tag in product.tags}
    product_ids = {product.product_id for product in catalog_products}
    return categories, tags, product_ids


def _safe_values(value: object, *, allowed_values: set[str]) -> list[str]:
    """把推荐字段值过滤成安全短字符串，并在有货盘时强制要求命中货盘。"""

    values = _as_list(value)
    safe: list[str] = []
    for item in values:
        cleaned = _safe_text(item)
        if cleaned is None:
            continue
        if allowed_values and cleaned not in allowed_values:
            continue
        if cleaned not in safe:
            safe.append(cleaned)
    return safe


def _as_list(value: object) -> list[object]:
    """把单值或数组统一为数组；其他类型会在后续安全转换中被丢弃。"""

    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _safe_text(value: object) -> str | None:
    """限制长期记忆中的结构化值长度和字符形态，避免任意文本扩散。"""

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 32:
        return None
    lowered = cleaned.lower()
    if any(marker in lowered for marker in ("token", "secret", "password", "\\", "/", "://")):
        return None
    return cleaned


def _safe_identifier(value: object) -> str | None:
    """只允许短 ASCII 标识符作为冲突分组，防止把自然语言文本写进 key 字段。"""

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cleaned):
        return None
    return cleaned


def _feedback_strength(anchor_action: AnchorAction, business_result: BusinessResult) -> tuple[Decimal, Decimal]:
    """根据反馈质量生成确定性置信度和证据权重。"""

    if anchor_action == AnchorAction.ACCEPTED and business_result == BusinessResult.GOOD:
        return Decimal("0.92"), Decimal("0.88")
    if anchor_action == AnchorAction.ACCEPTED and business_result == BusinessResult.BAD:
        return Decimal("0.65"), Decimal("0.75")
    if anchor_action == AnchorAction.REJECTED and business_result == BusinessResult.ANCHOR_RIGHT:
        return Decimal("0.88"), Decimal("0.82")
    return Decimal("0.70"), Decimal("0.60")


def _feedback_content(anchor_action: AnchorAction, business_result: BusinessResult) -> str:
    """生成简短脱敏记忆正文，不包含具体主播原话或订单数据。"""

    return f"基于脱敏 Decision Trace 反馈生成的 L2 记忆：{anchor_action.value}/{business_result.value}。"


def _safe_key(value: str) -> str:
    """把 trace_id 转成适合 memory_key 的稳定片段。"""

    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip())
    if not cleaned:
        raise ValueError("trace_id must not be empty")
    return cleaned[:120]
