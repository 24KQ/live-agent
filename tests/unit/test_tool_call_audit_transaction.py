"""工具调用审计事务契约的单元测试。"""

from __future__ import annotations

from typing import Any

import psycopg

from src.audit.tool_call_audit import AuditEvent, ToolCallAuditStore
from src.core.security_hooks import GateDecision
from src.state.models import ActionType, RiskLevel


class _FakeCursor:
    """返回一次成功 INSERT 结果，避免事务契约测试依赖真实 PostgreSQL。"""

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, _sql: str, _parameters: dict[str, Any]) -> None:
        """测试只关心连接隔离级别，不解析 SQL。"""

    def fetchone(self) -> dict[str, str]:
        return {"audit_id": "audit-transaction-001"}


class _FakeConnection:
    """记录 Store 是否在首条 SQL 前显式固定事务隔离级别。"""

    def __init__(self) -> None:
        self.isolation_level: psycopg.IsolationLevel | None = None
        self.isolation_level_when_cursor_opened: psycopg.IsolationLevel | None = None

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        self.isolation_level_when_cursor_opened = self.isolation_level
        return _FakeCursor()

    def commit(self) -> None:
        """模拟成功提交。"""


class _Settings:
    """提供 Store 所需的最小连接参数。"""

    postgres_connection_kwargs: dict[str, Any] = {}


def test_record_event_sets_read_committed_before_opening_cursor(monkeypatch: Any) -> None:
    """幂等冲突后的 SELECT 必须运行在显式 READ COMMITTED 事务中。"""

    connection = _FakeConnection()
    monkeypatch.setattr(psycopg, "connect", lambda **_kwargs: connection)
    store = ToolCallAuditStore(_Settings())  # type: ignore[arg-type]
    event = AuditEvent(
        trace_id="trace-transaction-contract",
        room_id="room-transaction-contract",
        tool_name="query_products",
        action_type=ActionType.QUERY_PRODUCTS,
        risk_level=RiskLevel.LOW,
        gate_decision=GateDecision.AUTO,
        operator_decision=None,
    )

    store.record_event(event)

    assert connection.isolation_level_when_cursor_opened is psycopg.IsolationLevel.READ_COMMITTED
