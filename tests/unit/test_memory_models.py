"""Phase 3A 记忆与信任领域模型测试。

这些测试先定义记忆层的数据契约：哪些字段必须存在、哪些枚举值允许进入系统、
trust_score 的边界在哪里。后续实现必须先通过这些边界校验，避免脏数据写入 PostgreSQL。
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.memory.models import (
    AnchorAction,
    AnchorMemoryEntry,
    BusinessResult,
    DecisionTraceRecord,
    MemoryLayer,
    MemorySource,
    TrustState,
)


def test_memory_entry_accepts_l1_l2_l3_layers() -> None:
    """L1/L2/L3 三层记忆都应可被建模，便于后续区分显式偏好、行为归纳和长期总结。"""

    entries = [
        AnchorMemoryEntry(
            memory_key=f"memory-{layer.value.lower()}",
            anchor_id="anchor-001",
            room_id="room-001",
            layer=layer,
            content=f"{layer.value} 样例记忆",
            metadata={"preferred_category": "厨房"},
            confidence=Decimal("0.80"),
            evidence_weight=Decimal("0.70"),
            source=MemorySource.USER_STATED,
        )
        for layer in (MemoryLayer.L1, MemoryLayer.L2, MemoryLayer.L3)
    ]

    assert [entry.layer for entry in entries] == [MemoryLayer.L1, MemoryLayer.L2, MemoryLayer.L3]
    assert all(entry.embedding is None for entry in entries)


def test_memory_entry_rejects_empty_or_unknown_values() -> None:
    """空主播、空内容、未知层级和越界置信度都必须在模型层被拒绝。"""

    with pytest.raises(ValidationError):
        AnchorMemoryEntry(
            anchor_id="",
            layer="L9",
            content="",
            confidence=Decimal("1.20"),
            evidence_weight=Decimal("-0.10"),
            source="unknown",
        )


def test_memory_models_reject_whitespace_only_identifiers() -> None:
    """关键 ID 只包含空白时也必须拒绝，避免后续查询和审计出现不可追踪记录。"""

    with pytest.raises(ValidationError):
        AnchorMemoryEntry(
            anchor_id="   ",
            layer=MemoryLayer.L1,
            content="有效内容",
            source=MemorySource.USER_STATED,
        )

    with pytest.raises(ValidationError):
        DecisionTraceRecord(
            trace_id=" ",
            anchor_id="anchor-001",
            room_id="room-001",
            recommendation={"first_product_id": "p003"},
            anchor_action=AnchorAction.ACCEPTED,
            business_result=BusinessResult.GOOD,
            lift=Decimal("0.12"),
            trust_delta=Decimal("0.05"),
            final_trust_score=Decimal("0.75"),
        )


def test_trust_state_is_clamped_by_model_contract() -> None:
    """trust_score 只能落在 0.0-1.0，默认值用于新主播的保守起点。"""

    assert TrustState(anchor_id="anchor-001").trust_score == Decimal("0.70")

    with pytest.raises(ValidationError):
        TrustState(anchor_id="anchor-001", trust_score=Decimal("1.01"))

    with pytest.raises(ValidationError):
        TrustState(anchor_id="anchor-001", trust_score=Decimal("-0.01"))


def test_decision_trace_record_requires_feedback_and_result() -> None:
    """Decision Trace 必须能同时记录建议、主播动作、业务结果和信任分变化。"""

    record = DecisionTraceRecord(
        trace_id="trace-001",
        anchor_id="anchor-001",
        room_id="room-001",
        recommendation={"first_product_id": "p003"},
        anchor_action=AnchorAction.ACCEPTED,
        business_result=BusinessResult.GOOD,
        lift=Decimal("0.12"),
        trust_delta=Decimal("0.05"),
        final_trust_score=Decimal("0.75"),
    )

    assert record.anchor_action == AnchorAction.ACCEPTED
    assert record.business_result == BusinessResult.GOOD
    assert record.final_trust_score == Decimal("0.75")
