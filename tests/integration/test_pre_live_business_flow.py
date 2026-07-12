"""Phase 2A 播前业务闭环集成测试。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data
from src.skills.product_catalog import ProductCatalogRepository


def test_pre_live_business_flow_generates_plan_cards_setup_and_audit() -> None:
    """完整播前流程应查询货盘、生成排品和手卡、确认建播并写入审计。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    audit_store = ToolCallAuditStore(settings)
    service = PreLiveBusinessFlowService(
        catalog_repository=ProductCatalogRepository(settings),
        audit_store=audit_store,
    )

    result = service.prepare_room(
        room_id="room-demo-001",
        trace_id="trace-phase2-flow",
        confirmed_setup=True,
    )

    assert len(result.products) == 10
    assert len(result.plan.items) >= 3
    assert len(result.cards) == 3
    assert result.setup_gate.allowed is True
    assert result.setup_audit_id is not None

    events = audit_store.list_events_by_trace_id("trace-phase2-flow")
    assert {event["tool_name"] for event in events} >= {
        "query_products",
        "generate_live_plan",
        "generate_product_card",
        "setup_live_session",
    }


def test_concurrent_setup_reuses_one_audit_for_same_idempotency_key() -> None:
    """并发相同幂等键必须由数据库唯一约束收敛为一次建播审计。"""
    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    audit_store = ToolCallAuditStore(settings)
    service = PreLiveBusinessFlowService(
        catalog_repository=ProductCatalogRepository(settings),
        audit_store=audit_store,
    )
    trace_id = f"trace-phase11a-concurrent-{uuid4()}"
    products = service.query_products("room-demo-001", trace_id)
    plan = service.generate_plan("room-demo-001", products, trace_id)
    idempotency_key = f"{trace_id}:concurrent-setup"
    barrier = Barrier(2)

    def setup_once() -> str | None:
        barrier.wait()
        return service.setup_live_session(
            room_id="room-demo-001",
            plan=plan,
            trace_id=trace_id,
            confirmed_setup=True,
            idempotency_key=idempotency_key,
        )[1]

    with ThreadPoolExecutor(max_workers=2) as executor:
        audit_ids = list(executor.map(lambda _: setup_once(), range(2)))

    setup_events = [
        event
        for event in audit_store.list_events_by_trace_id(trace_id)
        if event["tool_name"] == "setup_live_session"
    ]
    assert audit_ids[0] == audit_ids[1]
    assert len(setup_events) == 1
