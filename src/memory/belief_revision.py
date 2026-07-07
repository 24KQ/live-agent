"""Phase 3B 记忆冲突修正服务。

冲突修正遵循“保留证据、降低影响”的原则：旧记忆不删除，只标记为 suppressed；
新记忆作为 active 写入。这样后续审计仍能看见历史偏好如何被反馈修正。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.memory.models import AnchorMemoryEntry, MemoryStatus


_CONFLICT_FIELD_GROUPS = {
    "preferred_category": ("preferred_category", "preferred_categories"),
    "preferred_tags": ("preferred_tag", "preferred_tags"),
    "preferred_product_ids": ("preferred_product_id", "preferred_product_ids"),
}


@dataclass(frozen=True)
class BeliefRevisionResult:
    """一次冲突修正的结果摘要。"""

    new_memory_id: str
    new_memory_key: str | None
    suppressed_memory_keys: list[str]
    conflict_fields: list[str]


class BeliefRevisionService:
    """根据新反馈修正主播记忆。"""

    def __init__(self, memory_store) -> None:
        self._memory_store = memory_store

    def revise_preference(
        self,
        *,
        anchor_id: str,
        room_id: str,
        new_memory: AnchorMemoryEntry,
        reason: str,
    ) -> BeliefRevisionResult:
        """写入新偏好，并 suppress 与其冲突的旧偏好。

        这里显式校验 anchor/room，避免调用方传入其他主播或其他直播间的记忆，
        导致画像串号。冲突原因只使用结构化字段和调用方给出的脱敏说明。
        """

        _ensure_same_scope(anchor_id=anchor_id, room_id=room_id, new_memory=new_memory)
        if not reason or not reason.strip():
            raise ValueError("revision reason must not be empty")

        existing_memories = self._memory_store.list_memories(anchor_id=anchor_id, room_id=room_id)
        conflicts = detect_conflicting_memories(existing_memories, new_memory)
        conflict_fields = sorted({field for memory in conflicts for field in _changed_fields(memory, new_memory)})
        conflicts_with_reasons = [
            (memory, _build_suppressed_reason(memory, new_memory, reason))
            for memory in conflicts
            if memory.memory_key is not None
        ]
        clean_new_memory = new_memory.model_copy(update={"status": MemoryStatus.ACTIVE, "suppressed_reason": None})
        atomic_revision = getattr(self._memory_store, "revise_memories_atomically", None)
        if callable(atomic_revision):
            new_memory_id = atomic_revision(clean_new_memory, conflicts_with_reasons)
        else:
            # 单元测试替身可以只实现最小 Store 协议；真实 MemoryStore 会走上面的原子事务路径。
            for memory, suppress_reason in conflicts_with_reasons:
                self._memory_store.suppress_memory(memory.memory_key, suppress_reason)
            new_memory_id = self._memory_store.write_memory(clean_new_memory)
        return BeliefRevisionResult(
            new_memory_id=new_memory_id,
            new_memory_key=new_memory.memory_key,
            suppressed_memory_keys=[memory.memory_key for memory, _ in conflicts_with_reasons if memory.memory_key],
            conflict_fields=conflict_fields,
        )


def detect_conflicting_memories(
    existing_memories: list[AnchorMemoryEntry],
    new_memory: AnchorMemoryEntry,
) -> list[AnchorMemoryEntry]:
    """找出与新偏好同组但取值不同的 active 旧记忆。"""

    new_group = new_memory.metadata.get("conflict_group")
    if not new_group:
        return []
    conflicts: list[AnchorMemoryEntry] = []
    for memory in existing_memories:
        if memory.status == MemoryStatus.SUPPRESSED:
            continue
        if memory.memory_key == new_memory.memory_key:
            continue
        if memory.metadata.get("conflict_group") != new_group:
            continue
        if _changed_fields(memory, new_memory):
            conflicts.append(memory)
    return conflicts


def _ensure_same_scope(*, anchor_id: str, room_id: str, new_memory: AnchorMemoryEntry) -> None:
    """确保新记忆属于当前主播和直播间。"""

    if new_memory.anchor_id != anchor_id:
        raise ValueError("anchor_id mismatch for memory revision")
    if new_memory.room_id != room_id:
        raise ValueError("room_id mismatch for memory revision")


def _changed_fields(old_memory: AnchorMemoryEntry, new_memory: AnchorMemoryEntry) -> list[str]:
    """返回同一 conflict_group 中发生变化的偏好字段。"""

    changed: list[str] = []
    for canonical_field, aliases in _CONFLICT_FIELD_GROUPS.items():
        old_values = _normalize_metadata_aliases(old_memory.metadata, aliases)
        new_values = _normalize_metadata_aliases(new_memory.metadata, aliases)
        if old_values and new_values and old_values != new_values:
            changed.append(canonical_field)
    return changed


def _normalize_metadata_aliases(metadata: dict[str, object], aliases: tuple[str, ...]) -> tuple[str, ...]:
    """按同一偏好维度合并单数/复数字段，避免字段别名导致漏检冲突。"""

    values: list[str] = []
    for alias in aliases:
        values.extend(_normalize_metadata_values(metadata.get(alias)))
    return tuple(sorted(set(values)))


def _normalize_metadata_values(value: object) -> tuple[str, ...]:
    """把 metadata 中的字符串或数组统一为可比较的有序 tuple。"""

    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(sorted(str(item) for item in value))
    return (str(value),)


def _build_suppressed_reason(old_memory: AnchorMemoryEntry, new_memory: AnchorMemoryEntry, reason: str) -> str:
    """生成不含原始记忆正文的冲突修正原因。"""

    group = new_memory.metadata.get("conflict_group")
    fields = ",".join(_changed_fields(old_memory, new_memory))
    return f"conflict_group={group}; changed_fields={fields}; reason={reason.strip()}"
