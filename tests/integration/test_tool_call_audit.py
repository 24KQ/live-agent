"""工具调用审计集成测试。

本测试会连接本地 PostgreSQL，并执行 Phase 1 审计表初始化 SQL。
它不写入真实业务表，只验证审计链路可以保存工具调用和状态变更结果。
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from src.audit import tool_call_audit
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


def _idempotent_event(*, trace_id: str, room_id: str, idempotency_key: str) -> AuditEvent:
    """构造带显式幂等键的审计事件，所有唯一值均由测试传入以隔离历史数据。"""

    return AuditEvent(
        trace_id=trace_id,
        room_id=room_id,
        tool_name="setup_live_session",
        action_type=ActionType.SETUP_LIVE_SESSION,
        risk_level=RiskLevel.HIGH,
        gate_decision=GateDecision.HARD_GATE,
        operator_decision="approved",
        idempotency_key=idempotency_key,
        request_payload={"room_id": room_id, "plan_item_ids": ["p001"]},
        result_payload={"status": "prepared", "plan_item_ids": ["p001"]},
    )


def test_idempotent_replay_returns_original_id_only_for_semantically_equal_event() -> None:
    """同工具同键且全部审计事实一致时，应复用原 ID 且数据库中仅保留一行。"""

    init_audit_table()
    store = ToolCallAuditStore(get_settings())
    unique = str(uuid4())
    event = _idempotent_event(
        trace_id=f"trace-audit-replay-{unique}",
        room_id=f"room-audit-replay-{unique}",
        idempotency_key=f"idem-audit-replay-{unique}",
    )

    first_audit_id = store.record_event(event)
    second_audit_id = store.record_event(event)

    assert second_audit_id == first_audit_id
    assert len(store.list_events_by_trace_id(event.trace_id)) == 1


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("room_id", "room-conflicting-scope"),
        ("trace_id", "trace-conflicting-scope"),
    ],
)
def test_idempotent_replay_rejects_cross_scope_event(
    changed_field: str,
    changed_value: str,
) -> None:
    """幂等键是工具级全局作用域，跨直播间或链路重用必须 fail-closed。"""

    init_audit_table()
    store = ToolCallAuditStore(get_settings())
    unique = str(uuid4())
    original = _idempotent_event(
        trace_id=f"trace-audit-scope-{unique}",
        room_id=f"room-audit-scope-{unique}",
        idempotency_key=f"idem-audit-scope-{unique}",
    )
    original_audit_id = store.record_event(original)

    with pytest.raises(tool_call_audit.IdempotencyConflictError):
        store.record_event(replace(original, **{changed_field: f"{changed_value}-{unique}"}))

    # 冲突尝试不得覆盖先写入者，原始 room、trace 和 ID 都必须保持不变。
    saved = store.get_event_by_trace_id(original.trace_id)
    assert saved is not None
    assert saved["audit_id"] == original_audit_id
    assert saved["room_id"] == original.room_id
    assert saved["trace_id"] == original.trace_id


def test_idempotent_replay_rejects_different_payload_without_leaking_sensitive_values() -> None:
    """同 trace 的异计划载荷必须冲突，异常摘要不得回显幂等键或载荷内容。"""

    init_audit_table()
    store = ToolCallAuditStore(get_settings())
    unique = str(uuid4())
    secret_key = f"idem-secret-{unique}"
    secret_payload = f"payload-secret-{unique}"
    original = _idempotent_event(
        trace_id=f"trace-audit-payload-{unique}",
        room_id=f"room-audit-payload-{unique}",
        idempotency_key=secret_key,
    )
    original_audit_id = store.record_event(original)
    conflicting = replace(
        original,
        request_payload={"room_id": original.room_id, "plan_item_ids": [secret_payload]},
    )

    with pytest.raises(tool_call_audit.IdempotencyConflictError) as exc_info:
        store.record_event(conflicting)

    summary = str(exc_info.value)
    assert secret_key not in summary
    assert secret_payload not in summary
    saved = store.get_event_by_trace_id(original.trace_id)
    assert saved is not None
    assert saved["audit_id"] == original_audit_id
    assert saved["request_payload"] == original.request_payload


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("action_type", ActionType.SET_PRICE),
        ("risk_level", RiskLevel.CRITICAL),
        ("gate_decision", GateDecision.SOFT_GATE),
        ("operator_decision", "rejected"),
        ("result_payload", {"status": "prepared", "plan_item_ids": ["p002"]}),
    ],
)
def test_idempotent_replay_rejects_each_changed_audit_fact(
    changed_field: str,
    changed_value: object,
) -> None:
    """除作用域和请求外，其余任一审计事实变化也必须触发受控冲突。"""

    init_audit_table()
    store = ToolCallAuditStore(get_settings())
    unique = str(uuid4())
    original = _idempotent_event(
        trace_id=f"trace-audit-fact-{unique}",
        room_id=f"room-audit-fact-{unique}",
        idempotency_key=f"idem-audit-fact-{unique}",
    )
    store.record_event(original)

    with pytest.raises(tool_call_audit.IdempotencyConflictError):
        store.record_event(replace(original, **{changed_field: changed_value}))


def test_idempotent_replay_distinguishes_json_boolean_from_integer() -> None:
    """JSON 布尔值和整数必须按类型区分，不能沿用 Python 的 True == 1 规则。"""

    init_audit_table()
    store = ToolCallAuditStore(get_settings())
    unique = str(uuid4())
    original = replace(
        _idempotent_event(
            trace_id=f"trace-audit-json-type-{unique}",
            room_id=f"room-audit-json-type-{unique}",
            idempotency_key=f"idem-audit-json-type-{unique}",
        ),
        request_payload={"requires_confirmation": True},
    )
    store.record_event(original)

    with pytest.raises(tool_call_audit.IdempotencyConflictError):
        store.record_event(replace(original, request_payload={"requires_confirmation": 1}))


def test_concurrent_equal_idempotent_events_create_only_one_row() -> None:
    """两个并发事务写入完全相同事件时，应等待胜者提交并共同返回同一 ID。"""

    init_audit_table()
    store = ToolCallAuditStore(get_settings())
    unique = str(uuid4())
    event = _idempotent_event(
        trace_id=f"trace-audit-concurrent-{unique}",
        room_id=f"room-audit-concurrent-{unique}",
        idempotency_key=f"idem-audit-concurrent-{unique}",
    )
    barrier = Barrier(2)

    def record_once() -> str:
        """让两个线程尽量同时进入 INSERT，以验证数据库冲突路径。"""

        barrier.wait()
        return store.record_event(event)

    with ThreadPoolExecutor(max_workers=2) as executor:
        audit_ids = list(executor.map(lambda _: record_once(), range(2)))

    assert audit_ids[0] == audit_ids[1]
    assert len(store.list_events_by_trace_id(event.trace_id)) == 1
