"""Phase 13 基于独立 DecisionTrace 的确定性记忆晋升策略。"""

from __future__ import annotations

from typing import Any

from src.memory.candidate_store import (
    InMemoryMemoryCandidateStore,
    MemoryCandidateStatus,
    MemoryPromotionCommand,
    PromotionResult,
)
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource


class PromotionPolicy:
    """只在已证明的低风险结构化事实下作出晋升决定，其他情况保持暂存。"""

    def __init__(self, *, store: InMemoryMemoryCandidateStore, active_memory_port: Any | None) -> None:
        self._store = store
        self._active_memory_port = active_memory_port

    def promote(
        self,
        command: MemoryPromotionCommand,
        *,
        decision_traces: tuple[dict[str, Any], ...],
        product_whitelist: set[str],
    ) -> PromotionResult:
        """先校验命令版本，再按独立证据、作用域和货盘白名单作出可重放决定。"""

        replay = self._store.get_command_result(command.command_id)
        if replay is not None:
            return replay
        candidate = self._store.get(command.candidate_id)
        if candidate.version != command.expected_version:
            raise ValueError("expected_version does not match memory candidate")
        if candidate.status is not command.expected_status:
            raise ValueError("expected_status does not match memory candidate")
        ids = {str(trace.get("decision_trace_id") or "") for trace in decision_traces}
        matching = tuple(
            trace
            for trace in decision_traces
            if trace.get("anchor_id") == candidate.anchor_id
            and trace.get("room_id") == candidate.room_id
            and str(trace.get("decision_trace_id") or "") in candidate.evidence_ids
        )
        if len(ids & set(candidate.evidence_ids)) < 2 or len(matching) < 2:
            result = PromotionResult(
                candidate_id=candidate.candidate_id,
                status=MemoryCandidateStatus.STAGED,
                reason_code="INSUFFICIENT_INDEPENDENT_EVIDENCE",
                version=candidate.version,
            )
            return self._store.record_command_result(command.command_id, result)
        if not set(candidate.preferred_product_ids).issubset(product_whitelist):
            result = PromotionResult(candidate_id=candidate.candidate_id, status=MemoryCandidateStatus.STAGED, reason_code="PRODUCT_WHITELIST_MISMATCH", version=candidate.version)
            return self._store.record_command_result(command.command_id, result)
        if self._active_memory_port is None:
            result = PromotionResult(candidate_id=candidate.candidate_id, status=MemoryCandidateStatus.STAGED, reason_code="ACTIVE_MEMORY_PORT_UNAVAILABLE", version=candidate.version)
            return self._store.record_command_result(command.command_id, result)
        entry = AnchorMemoryEntry(
            memory_key=f"phase13-promoted-{candidate.candidate_id}",
            anchor_id=candidate.anchor_id,
            room_id=candidate.room_id,
            layer=MemoryLayer.L2,
            content=f"确定性播后偏好：{candidate.preferred_category}。",
            metadata={"preferred_category": candidate.preferred_category, "preferred_tags": list(candidate.preferred_tags), "preferred_product_ids": list(candidate.preferred_product_ids), "promotion_candidate_id": candidate.candidate_id},
            confidence=candidate.confidence,
            evidence_weight=candidate.confidence,
            source=MemorySource.SYSTEM_OBSERVED,
        )
        self._active_memory_port.write_memory(entry)
        applied = self._store.transition(candidate.candidate_id, status=MemoryCandidateStatus.APPLIED)
        result = PromotionResult(candidate_id=applied.candidate_id, status=applied.status, reason_code="APPLIED", version=applied.version)
        return self._store.record_command_result(command.command_id, result)
