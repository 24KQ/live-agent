"""Phase 5H Harness Agent 审计 writer 单元测试。

这些测试先固定审计闭环的外部行为：无数据库依赖时必须能 dry-run，注入 store 时必须生成
ToolCallAuditStore / DecisionTraceStore 可接受的结构，并且审计 payload 不能带敏感信息。
"""

from __future__ import annotations

import json
from typing import Any

from src.audit.tool_call_audit import AuditEvent
from src.core.on_live_harness_audit import OnLiveHarnessAuditWriter
from src.memory.models import DecisionTraceRecord
from src.state.models import ActionType


def _base_state(**overrides: Any) -> dict[str, Any]:
    """构造最小 Harness state，便于不同测试只覆盖关心字段。"""
    state: dict[str, Any] = {
        "room_id": "room-5h",
        "trace_id": "trace-5h",
        "trust_score": 0.72,
        "iteration": 1,
        "completed_nodes": ["load_context", "agent_reasoning", "write_audit"],
        "context_summary": "库存售罄，需要给主播建议",
        "pending_tool_call": None,
        "tool_policy": None,
        "observations": [],
        "executed_tools": [],
        "final_suggestion": None,
        "agent_status": "no_action",
        "error": None,
    }
    state.update(overrides)
    return state


class FakeAuditStore:
    """记录传入的 AuditEvent，避免单元测试依赖 PostgreSQL。"""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record_event(self, event: AuditEvent) -> str:
        self.events.append(event)
        return f"audit-{len(self.events)}"


class FakeDecisionTraceStore:
    """记录传入的 DecisionTraceRecord，验证 DecisionTrace 写入内容。"""

    def __init__(self) -> None:
        self.records: list[DecisionTraceRecord] = []

    def record_trace(self, record: DecisionTraceRecord) -> str:
        self.records.append(record)
        return f"decision-{len(self.records)}"


def test_dry_run_writer_returns_structured_payload_without_stores() -> None:
    """无真实 store 时返回 dry_run，不阻断 CLI 和单元测试。"""
    writer = OnLiveHarnessAuditWriter()
    result = writer.write(_base_state(final_suggestion="本轮无需干预", agent_status="final_answer"))

    assert result["audit_status"] == "dry_run"
    assert result["audit_ids"]
    assert result["decision_trace_ids"]
    assert result["audit_payload"]["decision_trace_dry_run"]["recommendation"]["final_suggestion"] == "本轮无需干预"


def test_tool_call_state_generates_audit_event_compatible_payload() -> None:
    """工具调用结果应转换为 ToolCallAuditStore 可写入的 AuditEvent。"""
    audit_store = FakeAuditStore()
    writer = OnLiveHarnessAuditWriter(audit_store=audit_store)
    result = writer.write(
        _base_state(
            pending_tool_call={
                "tool_name": "recommend_backup_product",
                "arguments": {"sold_out_product_id": "p001"},
                "risk_level": "MEDIUM",
            },
            tool_policy={"status": "auto_execute", "tool_name": "recommend_backup_product"},
            executed_tools=[
                {
                    "tool_name": "recommend_backup_product",
                    "status": "success",
                    "backup_product_id": "p002",
                }
            ],
            observations=[{"tool_name": "recommend_backup_product", "success": True}],
            final_suggestion="建议切换到 p002",
            agent_status="final_answer",
        )
    )

    assert result["audit_status"] == "recorded"
    assert result["audit_ids"] == ["audit-1"]
    assert audit_store.events[0].action_type == ActionType.RECOMMEND_BACKUP_PRODUCT
    assert audit_store.events[0].request_payload["pending_tool_call"]["tool_name"] == "recommend_backup_product"
    assert audit_store.events[0].result_payload["final_suggestion"] == "建议切换到 p002"


def test_final_answer_writes_real_decision_trace_when_anchor_and_store_exist() -> None:
    """存在 anchor_id 和 DecisionTraceStore 时写入真实 DecisionTraceRecord。"""
    decision_store = FakeDecisionTraceStore()
    writer = OnLiveHarnessAuditWriter(decision_trace_store=decision_store)
    result = writer.write(
        _base_state(
            anchor_id="anchor-5h",
            final_suggestion="解释售罄原因并推荐备用商品",
            agent_status="final_answer",
        )
    )

    assert result["decision_trace_ids"] == ["decision-1"]
    assert decision_store.records[0].anchor_id == "anchor-5h"
    assert decision_store.records[0].recommendation["final_suggestion"] == "解释售罄原因并推荐备用商品"


def test_pending_blocked_and_max_iterations_are_recorded_in_payload() -> None:
    """pending_human、blocked、max_iterations 都必须进入审计 payload，便于后续复盘。"""
    writer = OnLiveHarnessAuditWriter()

    for status in ("pending_human", "blocked", "max_iterations"):
        result = writer.write(_base_state(agent_status=status, error=f"{status} reason"))
        payload = result["audit_payload"]
        assert payload["result_payload"]["agent_status"] == status
        assert payload["result_payload"]["error"] == f"{status} reason"


def test_audit_payload_redacts_sensitive_values() -> None:
    """审计 payload 不能泄露 key、token、password、.env 或本机私密路径。"""
    writer = OnLiveHarnessAuditWriter()
    result = writer.write(
        _base_state(
            executed_tools=[
                {
                    "tool_name": "generate_on_live_prompt",
                    "status": "success",
                    "api_key": "sk-secret",
                    "token": "token-secret",
                    "path": r"D:\java\agent\.env",
                    "note": r"C:\Users\24KQ\private.txt",
                }
            ],
            final_suggestion="安全摘要",
            agent_status="final_answer",
        )
    )

    dumped = json.dumps(result["audit_payload"], ensure_ascii=False)
    assert "sk-secret" not in dumped
    assert "token-secret" not in dumped
    assert ".env" not in dumped
    assert r"C:\Users" not in dumped
