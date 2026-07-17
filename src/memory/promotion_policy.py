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

    def __init__(
        self,
        *,
        store: InMemoryMemoryCandidateStore,
        active_memory_port: Any | None,
        eligibility_store: Any,
        decision_trace_resolver: Any,
    ) -> None:
        self._store = store
        self._active_memory_port = active_memory_port
        self._eligibility_store = eligibility_store
        self._decision_trace_resolver = decision_trace_resolver

    def promote(
        self,
        command: MemoryPromotionCommand,
        *,
        operator_id: str,
    ) -> PromotionResult:
        """先校验资格事实和人工意图，再执行唯一的 active-memory 写入。"""

        if command.expected_status is not MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR:
            raise ValueError("operator confirmation is required before promotion")
        if not operator_id or not operator_id.strip():
            raise ValueError("operator_id is required")
        intent = self._eligibility_store.get_confirmation_intent(command.command_id)
        if intent is None or intent.candidate_id != command.candidate_id or intent.expected_version != command.expected_version or intent.operator_id != operator_id:
            raise ValueError("promotion requires a matching confirmation intent")
        replay = self._store.get_command_result(command.command_id)
        if replay is not None:
            if replay.candidate_id != command.candidate_id:
                raise ValueError("promotion command conflicts with candidate")
            return replay
        eligibility = self._eligibility_store.get_eligibility(command.candidate_id)
        if eligibility is None or eligibility.status is not MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR:
            raise ValueError("promotion requires a persisted eligible fact")
        candidate = self._store.get(command.candidate_id)
        if candidate.status is MemoryCandidateStatus.APPLIED and candidate.version == command.expected_version + 1:
            # active-memory 写入和候选 CAS 已完成、但命令账本落盘前进程退出时，
            # 直接补记 APPLIED 结果，避免重启后重复执行或错误拒绝原确认。
            result = PromotionResult(candidate_id=candidate.candidate_id, status=candidate.status, reason_code="APPLIED", version=candidate.version)
            return self._store.record_command_result(command.command_id, result)
        if candidate.version != command.expected_version:
            raise ValueError("expected_version does not match memory candidate")
        if candidate.status is not command.expected_status:
            raise ValueError("expected_status does not match memory candidate")
        if eligibility.candidate_version != candidate.version:
            raise ValueError("eligibility fact version does not match memory candidate")
        traces = []
        for trace_id in eligibility.evidence_ids:
            trace = self._decision_trace_resolver.resolve(trace_id)
            if trace is None or trace.get("anchor_id") != candidate.anchor_id or trace.get("room_id") != candidate.room_id:
                raise ValueError("trusted decision trace is missing or out of scope")
            traces.append(trace)
        if len({str(trace.get("trace_id") or "") for trace in traces}) < 2:
            raise ValueError("trusted decision traces are not independent")
        if not set(candidate.preferred_product_ids).issubset(set(eligibility.product_whitelist) - {"__EMPTY__"}):
            result = PromotionResult(candidate_id=candidate.candidate_id, status=MemoryCandidateStatus.STAGED, reason_code="PRODUCT_WHITELIST_MISMATCH", version=candidate.version)
            return self._store.record_command_result(command.command_id, result)
        if self._active_memory_port is None:
            result = PromotionResult(candidate_id=candidate.candidate_id, status=MemoryCandidateStatus.STAGED, reason_code="ACTIVE_MEMORY_PORT_UNAVAILABLE", version=candidate.version)
            return self._store.record_command_result(command.command_id, result)
        lock_factory = getattr(self._active_memory_port, "promotion_scope_lock", None)
        if not callable(lock_factory):
            result = PromotionResult(candidate_id=candidate.candidate_id, status=MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR, reason_code="ACTIVE_MEMORY_PORT_NOT_COORDINATED", version=candidate.version)
            return self._store.record_command_result(command.command_id, result)
        with lock_factory(candidate.anchor_id, candidate.room_id):
            if self._has_active_conflict(candidate):
                result = PromotionResult(candidate_id=candidate.candidate_id, status=MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR, reason_code="ACTIVE_MEMORY_CONFLICT", version=candidate.version)
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

    def _has_active_conflict(self, candidate: Any) -> bool:
        """只检查同一作用域内已由本策略生成的结构化记忆，避免覆盖相反历史事实。"""

        list_memories = getattr(self._active_memory_port, "list_memories", None)
        if not callable(list_memories):
            return True
        for entry in list_memories(candidate.anchor_id, candidate.room_id):
            metadata = dict(getattr(entry, "metadata", {}) or {})
            if not metadata:
                return True
            if metadata.get("promotion_candidate_id") == candidate.candidate_id:
                continue
            if "promotion_candidate_id" not in metadata:
                return True
            if metadata.get("preferred_category") != candidate.preferred_category:
                continue
            existing_products = set(metadata.get("preferred_product_ids", ()))
            if existing_products.intersection(candidate.preferred_product_ids):
                return True
        return False
