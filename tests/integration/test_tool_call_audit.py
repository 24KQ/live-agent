"""工具调用审计集成测试。

本测试会连接本地 PostgreSQL，并执行 Phase 1 审计表初始化 SQL。
它不写入真实业务表，只验证审计链路可以保存工具调用和状态变更结果。
"""

from decimal import Decimal
from pathlib import Path

import psycopg

from src.audit.tool_call_audit import AuditEvent, ToolCallAuditStore
from src.config.settings import get_settings
from src.core.security_hooks import GateDecision
from src.state.models import ActionType, RiskLevel


def init_audit_table() -> None:
    """执行审计表初始化脚本，保证测试可重复运行。"""

    settings = get_settings()
    sql = Path("docker/init_phase1_audit.sql").read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
        connection.commit()


def test_tool_call_audit_store_writes_event_to_postgres() -> None:
    """审计 Store 应把工具调用事件写入 PostgreSQL，并返回 audit_id。"""

    init_audit_table()
    store = ToolCallAuditStore(get_settings())
    event = AuditEvent(
        trace_id="trace-audit-001",
        room_id="room-001",
        tool_name="set_product_price",
        action_type=ActionType.SET_PRICE,
        risk_level=RiskLevel.HIGH,
        gate_decision=GateDecision.HARD_GATE,
        operator_decision="approved",
        request_payload={"product_id": "p001", "price": "89.90"},
        result_payload={"old_price": str(Decimal("99.00")), "new_price": "89.90"},
    )

    audit_id = store.record_event(event)

    assert audit_id
    saved = store.get_event_by_trace_id("trace-audit-001")
    assert saved is not None
    assert saved["tool_name"] == "set_product_price"
    assert saved["operator_decision"] == "approved"
    assert saved["result_payload"]["new_price"] == "89.90"
