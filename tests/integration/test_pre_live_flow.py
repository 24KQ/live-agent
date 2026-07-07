"""播前最小闭环集成测试。

该测试串联工具注册表、安全 Hook、Reducer 和 PostgreSQL 审计，验证
Phase 1 的核心目标：改价必须 hard-gate，确认后才更新状态并写入审计。
"""

from decimal import Decimal
from pathlib import Path

import psycopg

from src.audit.tool_call_audit import ToolCallAuditStore
from src.config.settings import get_settings
from src.core.pre_live_flow import PreLiveFlowService
from src.state.models import LifecycleStage, LiveRoomState, Product


def init_audit_table() -> None:
    """执行审计表初始化脚本。"""

    settings = get_settings()
    sql = Path("docker/init_phase1_audit.sql").read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
        connection.commit()


def make_state() -> LiveRoomState:
    """构造播前模拟货盘。"""

    return LiveRoomState(
        room_id="room-001",
        products=[
            Product(product_id="p001", name="轻盈保温杯", price=Decimal("99.00"), inventory=20),
        ],
    )


def test_pre_live_price_change_requires_confirmation_before_reducer_runs() -> None:
    """未确认 hard-gate 时，不允许 Reducer 更新价格。"""

    init_audit_table()
    service = PreLiveFlowService(ToolCallAuditStore(get_settings()))

    result = service.request_price_change(
        state=make_state(),
        product_id="p001",
        new_price=Decimal("89.90"),
        confirmed=False,
        trace_id="trace-flow-pending",
    )

    assert result.gate_result.requires_confirmation is True
    assert result.updated_state.get_product("p001").price == Decimal("99.00")
    assert result.audit_id is not None


def test_pre_live_price_change_updates_state_and_audit_after_confirmation() -> None:
    """确认 hard-gate 后，应更新商品价格并写入审计。"""

    init_audit_table()
    audit_store = ToolCallAuditStore(get_settings())
    service = PreLiveFlowService(audit_store)

    result = service.request_price_change(
        state=make_state(),
        product_id="p001",
        new_price=Decimal("89.90"),
        confirmed=True,
        trace_id="trace-flow-approved",
    )

    assert result.gate_result.allowed is True
    assert result.updated_state.get_product("p001").price == Decimal("89.90")
    assert result.audit_id is not None

    saved = audit_store.get_event_by_trace_id("trace-flow-approved")
    assert saved is not None
    assert saved["gate_decision"] == "hard-gate"
    assert saved["operator_decision"] == "approved"
    assert saved["result_payload"]["new_price"] == "89.90"


def test_query_products_rejects_non_pre_live_state() -> None:
    """应用服务层也必须拒绝非播前阶段查询播前货盘工具。"""

    init_audit_table()
    service = PreLiveFlowService(ToolCallAuditStore(get_settings()))
    on_live_state = make_state().model_copy(update={"lifecycle": LifecycleStage.ON_LIVE})

    try:
        service.query_products(on_live_state)
    except ValueError as exc:
        assert "PRE_LIVE" in str(exc)
    else:
        raise AssertionError("query_products should reject non PRE_LIVE state")
