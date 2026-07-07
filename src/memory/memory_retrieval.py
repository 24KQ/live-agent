"""Phase 3B 增强记忆检索。

检索层把数据库中的原始记忆转换成结构化命中结果，包含有效权重、排序分和脱敏解释。
它不直接改写排品，也不回显完整记忆正文，避免未来接入真实历史数据后把敏感文本扩散到
主播提示或审计摘要之外。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from src.memory.memory_decay import MemoryDecayPolicy
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemoryStatus


@dataclass(frozen=True)
class MemoryHit:
    """单条增强检索命中结果。"""

    memory: AnchorMemoryEntry
    effective_weight: Decimal
    relevance_score: Decimal
    explanation: str


class MemoryRetriever:
    """从 MemoryStore 读取记忆，并应用 Phase 3B 排序策略。"""

    def __init__(self, memory_store, decay_policy: MemoryDecayPolicy | None = None) -> None:
        self._memory_store = memory_store
        self._decay_policy = decay_policy or MemoryDecayPolicy()

    def retrieve(
        self,
        *,
        anchor_id: str,
        room_id: str,
        layer: MemoryLayer | None = None,
        reference_time: datetime | None = None,
    ) -> list[MemoryHit]:
        """读取主播记忆并返回结构化命中。

        Store 负责参数化 SQL 与跨主播校验；检索层只处理排序与解释，便于后续替换为
        pgvector/embedding 检索时仍复用同一输出契约。
        """

        memories = self._memory_store.list_memories(anchor_id=anchor_id, room_id=room_id, layer=layer)
        return rank_memory_hits(
            memories,
            room_id=room_id,
            reference_time=reference_time,
            decay_policy=self._decay_policy,
        )


def rank_memory_hits(
    memories: Iterable[AnchorMemoryEntry],
    *,
    room_id: str,
    reference_time: datetime | None = None,
    decay_policy: MemoryDecayPolicy | None = None,
) -> list[MemoryHit]:
    """按有效权重、房间匹配、层级和时间生成稳定排序的命中列表。"""

    policy = decay_policy or MemoryDecayPolicy()
    current_time = reference_time or datetime.now(timezone.utc)
    hits = [_build_hit(memory, room_id=room_id, reference_time=current_time, decay_policy=policy) for memory in memories]
    return sorted(
        hits,
        key=lambda hit: (
            -hit.relevance_score,
            -hit.effective_weight,
            -_timestamp(hit.memory.created_at),
            hit.memory.memory_key or hit.memory.memory_id or "",
        ),
    )


def _build_hit(
    memory: AnchorMemoryEntry,
    *,
    room_id: str,
    reference_time: datetime,
    decay_policy: MemoryDecayPolicy,
) -> MemoryHit:
    """把一条原始记忆转换成可排序、可解释的命中结果。"""

    effective_weight = decay_policy.effective_weight(memory, reference_time=reference_time)
    room_bonus = Decimal("0.0500") if memory.room_id == room_id else Decimal("0.0000")
    layer_bonus = {
        MemoryLayer.L1: Decimal("0.0300"),
        MemoryLayer.L2: Decimal("0.0200"),
        MemoryLayer.L3: Decimal("0.0100"),
    }[memory.layer]
    relevance_score = (effective_weight + room_bonus + layer_bonus).quantize(Decimal("0.0001"))
    return MemoryHit(
        memory=memory,
        effective_weight=effective_weight,
        relevance_score=relevance_score,
        explanation=_build_explanation(memory),
    )


def _build_explanation(memory: AnchorMemoryEntry) -> str:
    """生成脱敏解释，只输出结构化字段，不复制完整记忆正文。"""

    metadata = memory.metadata
    parts = [f"{memory.layer.value}/{memory.source.value}", f"状态={memory.status.value}"]
    preferred_category = metadata.get("preferred_category") or metadata.get("preferred_categories")
    preferred_tags = metadata.get("preferred_tags") or metadata.get("preferred_tag")
    preferred_product_ids = metadata.get("preferred_product_ids") or metadata.get("preferred_product_id")
    if preferred_category:
        parts.append(f"类目={_summary_value(preferred_category)}")
    if preferred_tags:
        parts.append(f"标签={_summary_value(preferred_tags)}")
    if preferred_product_ids:
        parts.append(f"商品ID={_summary_value(preferred_product_ids)}")
    if memory.status == MemoryStatus.SUPPRESSED and memory.suppressed_reason:
        parts.append("已被后续反馈压低")
    return " 命中 ".join([parts[0], "、".join(parts[1:])])


def _summary_value(value: object) -> str:
    """把 metadata 中的字符串或数组转成短摘要，避免输出任意长文本。"""

    if isinstance(value, list):
        return ",".join(str(item) for item in value[:3])
    return str(value)


def _timestamp(value: datetime) -> float:
    """把时间转换为可排序时间戳；分数相同时新记忆优先。"""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()
