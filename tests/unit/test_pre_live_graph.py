"""Phase 2D LangGraph 播前骨架单元测试。

这些测试先定义 LangGraph 编排层应该暴露的最小行为：初始化 state、按固定
节点顺序运行、复用现有播前服务、并且不绕过 hard-gate。
"""

from decimal import Decimal

from src.core.pre_live_graph import build_pre_live_graph, create_initial_pre_live_graph_state
from src.core.security_hooks import GateDecision, GateResult
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_card_generator import ProductCard
from src.skills.product_catalog import CatalogProduct


class FakePreLiveBusinessService:
    """不访问数据库的轻量服务替身。

    单元测试关注 LangGraph 编排层本身，不需要连接 PostgreSQL。替身实现与
    `PreLiveBusinessFlowService` 对齐的公开方法，并记录调用顺序。
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
            ),
            CatalogProduct(
                product_id="p002",
                name="桌面理线器",
                category="收纳",
                price=Decimal("29.90"),
                inventory=50,
                conversion_rate=Decimal("0.18"),
                commission_rate=Decimal("0.08"),
                tags=["氛围"],
                selling_points=["桌面整洁", "安装简单", "适合办公室"],
            ),
        ]

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """模拟查询货盘节点。"""

        self.calls.append("query_products")
        assert room_id == "room-demo-001"
        assert trace_id.startswith("trace-phase2d")
        return self.products

    def generate_plan(self, room_id: str, products: list[CatalogProduct], trace_id: str) -> LivePlanDraft:
        """模拟排品节点。"""

        self.calls.append("generate_live_plan")
        return LivePlanDraft(
            room_id=room_id,
            trace_id=trace_id,
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product.product_id,
                    product_name=product.name,
                    role="引流款" if index == 1 else "氛围款",
                    reason="单元测试固定排品理由",
                )
                for index, product in enumerate(products, start=1)
            ],
        )

    def generate_cards(
        self,
        room_id: str,
        plan: LivePlanDraft,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> list[ProductCard]:
        """模拟手卡节点。"""

        self.calls.append("generate_product_cards")
        return [
            ProductCard(
                product_id=item.product_id,
                title=f"{item.product_name} 手卡",
                talking_points=["卖点一", "卖点二", "卖点三"],
                opening_script="这里是单元测试固定开场话术。",
                price_hint="价格以直播间当前展示为准。",
                risk_tips=["避免绝对化承诺。"],
            )
            for item in plan.items
        ]

    def setup_live_session(
        self,
        room_id: str,
        plan: LivePlanDraft,
        trace_id: str,
        confirmed_setup: bool,
    ) -> tuple[GateResult, str | None]:
        """模拟建播 hard-gate 节点。"""

        self.calls.append("setup_live_session")
        if confirmed_setup:
            return (
                GateResult(True, GateDecision.HARD_GATE, False, "主播已确认，允许执行高风险工具"),
                "audit-setup-001",
            )
        return (
            GateResult(False, GateDecision.HARD_GATE, True, "高风险工具需要主播确认"),
            None,
        )


def test_langgraph_dependency_can_be_imported() -> None:
    """Phase 2D 必须真正引入 LangGraph，而不是只保留文档口径。"""

    from langgraph.graph import StateGraph

    assert StateGraph is not None


def test_create_initial_pre_live_graph_state_sets_required_fields() -> None:
    """初始化 state 应包含 graph 运行所需的最小输入和节点历史。"""

    state = create_initial_pre_live_graph_state(
        room_id="room-demo-001",
        trace_id="trace-phase2d-unit",
        confirmed_setup=False,
    )

    assert state["room_id"] == "room-demo-001"
    assert state["trace_id"] == "trace-phase2d-unit"
    assert state["confirmed_setup"] is False
    assert state["completed_nodes"] == []
    assert state["error"] is None


def test_pre_live_graph_runs_nodes_and_keeps_setup_pending_without_confirmation() -> None:
    """未确认建播时 graph 应走完节点，但不得伪装建播成功。"""

    service = FakePreLiveBusinessService()
    graph = build_pre_live_graph(service)

    result = graph.invoke(
        create_initial_pre_live_graph_state(
            room_id="room-demo-001",
            trace_id="trace-phase2d-unit-pending",
            confirmed_setup=False,
        )
    )

    assert service.calls == [
        "query_products",
        "generate_live_plan",
        "generate_product_cards",
        "setup_live_session",
    ]
    assert result["completed_nodes"] == [
        "query_products",
        "generate_live_plan",
        "generate_product_cards",
        "compliance_check",
        "setup_live_session",
    ]
    assert result["product_count"] == 2
    assert result["plan_item_count"] == 2
    assert result["card_count"] == 2
    assert result["setup_gate_decision"] == "hard-gate"
    assert result["setup_gate_allowed"] is False
    assert result["setup_requires_confirmation"] is True
    assert result["setup_status"] == "pending_confirmation"
    assert result["setup_audit_id"] is None


def test_pre_live_graph_returns_setup_audit_after_confirmation() -> None:
    """确认建播后 graph 应返回 hard-gate 通过结果和建播审计 ID。"""

    service = FakePreLiveBusinessService()
    graph = build_pre_live_graph(service)

    result = graph.invoke(
        create_initial_pre_live_graph_state(
            room_id="room-demo-001",
            trace_id="trace-phase2d-unit-approved",
            confirmed_setup=True,
        )
    )

    assert result["setup_gate_allowed"] is True
    assert result["setup_requires_confirmation"] is False
    assert result["setup_status"] == "prepared"
    assert result["setup_audit_id"] == "audit-setup-001"
    assert "hard-gate" in result["compliance_summary"]
