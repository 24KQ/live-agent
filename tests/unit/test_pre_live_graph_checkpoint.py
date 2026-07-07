"""Phase 2E 播前 Graph checkpoint 单元测试。

本文件使用 LangGraph 内存 checkpointer 验证恢复语义，不依赖真实 PostgreSQL。
真正的 PostgresSaver 在集成测试里覆盖。
"""

from __future__ import annotations

from decimal import Decimal

from langgraph.checkpoint.memory import InMemorySaver

from src.core.pre_live_graph import (
    build_pre_live_graph,
    create_initial_pre_live_graph_state,
    create_pre_live_graph_config,
)
from src.core.security_hooks import GateDecision, GateResult
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_card_generator import ProductCard
from src.skills.product_catalog import CatalogProduct


class FakeCheckpointPreLiveService:
    """记录调用次数的服务替身。

    checkpoint 恢复时，如果已完成节点被重复执行，调用次数会立刻暴露问题。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.products = [
            CatalogProduct(
                product_id="p001",
                name="轻盈保温杯",
                category="日用品",
                price=Decimal("89.90"),
                inventory=20,
                conversion_rate=Decimal("0.20"),
                commission_rate=Decimal("0.10"),
                tags=["引流"],
                selling_points=["保温稳定", "杯身轻巧", "适合通勤"],
            )
        ]

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """模拟货盘查询。"""

        self.calls.append("query_products")
        return self.products

    def generate_plan(self, room_id: str, products: list[CatalogProduct], trace_id: str) -> LivePlanDraft:
        """模拟排品生成。"""

        self.calls.append("generate_live_plan")
        return LivePlanDraft(
            room_id=room_id,
            trace_id=trace_id,
            items=[
                LivePlanItem(
                    rank=1,
                    product_id=products[0].product_id,
                    product_name=products[0].name,
                    role="引流款",
                    reason="checkpoint 单元测试固定理由",
                )
            ],
        )

    def generate_cards(
        self,
        room_id: str,
        plan: LivePlanDraft,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> list[ProductCard]:
        """模拟商品手卡生成。"""

        self.calls.append("generate_product_cards")
        return [
            ProductCard(
                product_id=plan.items[0].product_id,
                title="轻盈保温杯｜日用品手卡",
                talking_points=["保温稳定", "杯身轻巧", "适合通勤"],
                opening_script="接下来介绍轻盈保温杯。",
                price_hint="以直播间当前展示为准。",
                risk_tips=["避免绝对化承诺。"],
            )
        ]

    def setup_live_session(
        self,
        room_id: str,
        plan: LivePlanDraft,
        trace_id: str,
        confirmed_setup: bool,
    ) -> tuple[GateResult, str | None]:
        """模拟建播 hard-gate。"""

        self.calls.append("setup_live_session")
        if confirmed_setup:
            return GateResult(True, GateDecision.HARD_GATE, False, "允许建播"), "audit-setup-001"
        return GateResult(False, GateDecision.HARD_GATE, True, "等待人工确认"), None


def test_create_pre_live_graph_config_uses_trace_id_as_thread_id() -> None:
    """trace_id 应直接作为 thread_id，便于 checkpoint 与审计链路对齐。"""

    config = create_pre_live_graph_config(" trace-phase2e-thread ")

    assert config == {"configurable": {"thread_id": "trace-phase2e-thread"}}


def test_pre_live_graph_interrupts_and_resumes_without_replaying_completed_nodes() -> None:
    """Graph 中断后恢复时，应从下一节点继续执行，而不是重放前半段审计节点。"""

    service = FakeCheckpointPreLiveService()
    checkpointer = InMemorySaver()
    graph = build_pre_live_graph(
        service,
        checkpointer=checkpointer,
        interrupt_after=["generate_product_cards"],
    )
    config = create_pre_live_graph_config("trace-phase2e-checkpoint-unit")

    first_result = graph.invoke(
        create_initial_pre_live_graph_state(
            room_id="room-demo-001",
            trace_id="trace-phase2e-checkpoint-unit",
            confirmed_setup=True,
        ),
        config=config,
    )

    assert first_result["completed_nodes"] == [
        "query_products",
        "generate_live_plan",
        "generate_product_cards",
    ]
    assert graph.get_state(config).next == ("compliance_check",)
    assert service.calls == ["query_products", "generate_live_plan", "generate_product_cards"]

    resumed_result = graph.invoke(None, config=config)

    assert resumed_result["setup_status"] == "prepared"
    assert resumed_result["setup_audit_id"] == "audit-setup-001"
    assert service.calls == [
        "query_products",
        "generate_live_plan",
        "generate_product_cards",
        "setup_live_session",
    ]


def test_pre_live_graph_resume_keeps_hard_gate_pending_without_confirmation() -> None:
    """恢复后的建播节点仍必须尊重 hard-gate，未确认不得返回成功状态。"""

    service = FakeCheckpointPreLiveService()
    checkpointer = InMemorySaver()
    graph = build_pre_live_graph(
        service,
        checkpointer=checkpointer,
        interrupt_after=["generate_product_cards"],
    )
    config = create_pre_live_graph_config("trace-phase2e-pending-unit")

    graph.invoke(
        create_initial_pre_live_graph_state(
            room_id="room-demo-001",
            trace_id="trace-phase2e-pending-unit",
            confirmed_setup=False,
        ),
        config=config,
    )
    resumed_result = graph.invoke(None, config=config)

    assert resumed_result["setup_status"] == "pending_confirmation"
    assert resumed_result["setup_gate_allowed"] is False
    assert resumed_result["setup_requires_confirmation"] is True
    assert resumed_result["setup_audit_id"] is None

