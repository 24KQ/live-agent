"""Phase 2F 播前 Graph interrupt 人审单元测试。

本文件使用 InMemorySaver 验证 LangGraph 原生 interrupt/resume 语义，
不访问真实 PostgreSQL；真实持久化恢复放在集成测试中覆盖。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from src.core.human_approval import HumanApprovalRequest, HumanApprovalResponse
from src.core.pre_live_graph import (
    build_pre_live_graph,
    create_initial_pre_live_graph_state,
    create_pre_live_graph_config,
)
from src.core.security_hooks import GateDecision, GateResult
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_card_generator import ProductCard
from src.skills.product_catalog import CatalogProduct


class FakeInterruptPreLiveService:
    """用于 Graph interrupt 测试的播前服务替身。

    替身记录业务节点调用和审批审计结果。审批记录方法模拟生产服务的幂等行为：
    同一个 trace 下相同审批状态只保存一次，从而覆盖 LangGraph resume 会重跑节点开头的特点。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.approval_events: list[dict[str, Any]] = []
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
        """模拟查询播前货盘。"""

        self.calls.append("query_products")
        return self.products

    def generate_plan(self, room_id: str, products: list[CatalogProduct], trace_id: str) -> LivePlanDraft:
        """模拟生成排品草案。"""

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
                    reason="Phase 2F 单元测试固定排品理由",
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
        """模拟生成商品手卡。"""

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
        """模拟建播 hard-gate，只有恢复批准后才允许执行。"""

        self.calls.append("setup_live_session")
        if confirmed_setup:
            return GateResult(True, GateDecision.HARD_GATE, False, "人工已批准，允许建播"), "audit-setup-001"
        return GateResult(False, GateDecision.HARD_GATE, True, "等待人工审批"), None

    def record_setup_approval_event(
        self,
        request: HumanApprovalRequest,
        response: HumanApprovalResponse | None,
    ) -> str:
        """模拟审批审计写入，并按 trace_id/status 幂等去重。"""

        status = "pending" if response is None else response.decision.value
        idempotency_key = f"{request.trace_id}:{request.tool_name}:approval:{status}"
        for event in self.approval_events:
            if event["idempotency_key"] == idempotency_key:
                return event["audit_id"]

        audit_id = f"audit-approval-{status}"
        self.approval_events.append(
            {
                "audit_id": audit_id,
                "idempotency_key": idempotency_key,
                "status": status,
                "operator_id": None if response is None else response.operator_id,
                "reason": None if response is None else response.reason,
            }
        )
        return audit_id


def _run_until_setup_interrupt(trace_id: str, service: FakeInterruptPreLiveService):
    """运行 Graph 到建播人审 interrupt，并返回 graph/config/首次结果。"""

    graph = build_pre_live_graph(service, checkpointer=InMemorySaver())
    config = create_pre_live_graph_config(trace_id)
    first_result = graph.invoke(
        create_initial_pre_live_graph_state(
            room_id="room-demo-001",
            trace_id=trace_id,
            confirmed_setup=False,
            enable_human_approval=True,
        ),
        config=config,
    )
    return graph, config, first_result


def test_pre_live_graph_interrupts_before_setup_live_session_when_human_approval_enabled() -> None:
    """启用人审时，建播节点应触发 LangGraph interrupt，而不是直接调用建播成功逻辑。"""

    service = FakeInterruptPreLiveService()
    graph, config, first_result = _run_until_setup_interrupt("trace-phase2f-interrupt-unit", service)

    interrupt_payload = first_result["__interrupt__"][0].value

    assert interrupt_payload["trace_id"] == "trace-phase2f-interrupt-unit"
    assert interrupt_payload["room_id"] == "room-demo-001"
    assert interrupt_payload["tool_name"] == "setup_live_session"
    assert interrupt_payload["risk_level"] == "HIGH"
    assert interrupt_payload["action"] == "confirm_setup_live_session"
    assert interrupt_payload["plan_item_ids"] == ["p001"]
    assert graph.get_state(config).interrupts[0].value == interrupt_payload
    assert "setup_live_session" not in service.calls
    assert [event["status"] for event in service.approval_events] == ["pending"]


def test_pre_live_graph_resumes_with_approved_decision_and_executes_setup() -> None:
    """人工批准后，Graph 应从 checkpoint 恢复并执行建播服务。"""

    service = FakeInterruptPreLiveService()
    graph, config, _ = _run_until_setup_interrupt("trace-phase2f-approved-unit", service)

    resumed = graph.invoke(
        Command(
            resume={
                "trace_id": "trace-phase2f-approved-unit",
                "room_id": "room-demo-001",
                "tool_name": "setup_live_session",
                "decision": "approved",
                "operator_id": "operator-demo",
                "reason": "确认建播配置无误。",
            }
        ),
        config=config,
    )

    assert resumed["setup_status"] == "prepared"
    assert resumed["setup_audit_id"] == "audit-setup-001"
    assert resumed["approval_decision"] == "approved"
    assert resumed["approval_resume_audit_id"] == "audit-approval-approved"
    assert service.calls == [
        "query_products",
        "generate_live_plan",
        "generate_product_cards",
        "setup_live_session",
    ]
    assert [event["status"] for event in service.approval_events] == ["pending", "approved"]


def test_pre_live_graph_resumes_with_rejected_decision_without_setup_success() -> None:
    """人工拒绝后，Graph 应结束为 rejected，且不得调用建播成功逻辑。"""

    service = FakeInterruptPreLiveService()
    graph, config, _ = _run_until_setup_interrupt("trace-phase2f-rejected-unit", service)

    resumed = graph.invoke(
        Command(
            resume={
                "trace_id": "trace-phase2f-rejected-unit",
                "room_id": "room-demo-001",
                "tool_name": "setup_live_session",
                "decision": "rejected",
                "operator_id": "operator-demo",
                "reason": "需要先调整排品节奏。",
            }
        ),
        config=config,
    )

    assert resumed["setup_status"] == "rejected"
    assert resumed["setup_gate_allowed"] is False
    assert resumed["setup_requires_confirmation"] is False
    assert resumed["setup_audit_id"] is None
    assert resumed["approval_decision"] == "rejected"
    assert resumed["approval_resume_audit_id"] == "audit-approval-rejected"
    assert "setup_live_session" not in service.calls
    assert [event["status"] for event in service.approval_events] == ["pending", "rejected"]
