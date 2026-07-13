"""Phase 5A Agent Tool Executor 单元测试。

测试 AgentToolExecutor 的白名单校验、生命周期检查、安全 Hook 和审计集成。
使用 FakePreLiveBusinessService 模拟 PreLiveBusinessFlowService，不依赖 PostgreSQL。
"""

import pytest
from decimal import Decimal

from src.config.tool_registry import get_default_tool_registry
from src.core.agent_decision import AgentToolCall
from src.core.agent_tool_executor import AgentToolExecutor
from src.skill_runtime.routing import RouteConfig, RoutePolicy
from src.skills.product_catalog import CatalogProduct


def _runtime_policy() -> RoutePolicy:
    """需要验证 Runtime 摘要或门禁时，测试显式启用前两批新执行链。"""
    return RoutePolicy(
        batch1=RouteConfig.SKILL_RUNTIME,
        batch2=RouteConfig.SKILL_RUNTIME,
        batch3=RouteConfig.LEGACY,
    )


class FakeService:
    """轻量 PreLiveBusinessFlowService 替代，记录调用并返回固定值。"""

    def __init__(self):
        self.calls = []
        self.products = [
            CatalogProduct(
                product_id="p001", name="测试商品A", category="日用",
                price=Decimal("39.90"), inventory=100,
                conversion_rate=Decimal("0.15"), commission_rate=Decimal("0.05"),
                tags=["引流"], selling_points=["卖点A"],
            ),
        ]

    def query_products(self, room_id, trace_id):
        self.calls.append(("query_products", room_id, trace_id))
        return self.products

    def generate_plan(self, room_id, products, trace_id):
        from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
        self.calls.append(("generate_plan", room_id, trace_id))
        return LivePlanDraft(
            room_id=room_id, trace_id=trace_id,
            items=[LivePlanItem(rank=1, product_id=p.product_id, product_name=p.name, role="引流款", reason="测试") for p in products],
        )

    def generate_cards(self, room_id, plan, products, trace_id):
        from src.skills.product_card_generator import ProductCard
        self.calls.append(("generate_cards", room_id, trace_id))
        return [ProductCard(
            product_id=item.product_id, title=item.product_name + "手卡",
            talking_points=["卖点1", "卖点2"], opening_script="开场话术",
            price_hint="价格提示", risk_tips=[],
        ) for item in plan.items]

    def setup_live_session(self, room_id, plan, trace_id, confirmed_setup):
        from src.core.security_hooks import GateDecision, GateResult
        self.calls.append(("setup_live_session", room_id, trace_id, confirmed_setup))
        if confirmed_setup:
            return GateResult(True, GateDecision.HARD_GATE, False, "已确认"), "audit-setup-001"
        return GateResult(False, GateDecision.HARD_GATE, True, "待确认"), None

    def record_setup_approval_event(self, request, response):
        self.calls.append(("record_setup_approval_event",))
        return "audit-approval-001"


class TestAgentToolExecutor:
    """工具执行器测试。"""

    def test_execute_whitelisted_tool_returns_success(self):
        """白名单内 query_products 应返回 success。"""
        registry = get_default_tool_registry()
        service = FakeService()
        executor = AgentToolExecutor(registry=registry, pre_live_service=service)
        obs = executor.execute(
            tool_name="query_products", arguments={"room_id": "room-001"},
            room_id="room-001", trace_id="trace-001",
        )
        assert obs.status == "success"
        assert obs.tool_name == "query_products"

    def test_execute_unknown_tool_returns_error(self):
        """未注册工具应返回 error。"""
        registry = get_default_tool_registry()
        service = FakeService()
        executor = AgentToolExecutor(registry=registry, pre_live_service=service)
        obs = executor.execute(
            tool_name="nonexistent_tool", arguments={},
            room_id="room-001", trace_id="trace-001",
        )
        assert obs.status == "error"
        assert "not found" in obs.summary.lower() or "未注册" in obs.summary

    def test_execute_wrong_lifecycle_returns_error(self):
        """PRE_LIVE 阶段调用 ON_LIVE 工具应返回 error。"""
        registry = get_default_tool_registry()
        service = FakeService()
        executor = AgentToolExecutor(registry=registry, pre_live_service=service)
        obs = executor.execute(
            tool_name="handle_sold_out_event", arguments={},
            room_id="room-001", trace_id="trace-001",
        )
        assert obs.status == "error"
        assert "lifecycle" in obs.summary.lower() or "not available" in obs.summary.lower()

    def test_generate_plan_returns_runtime_success(self):
        """generate_live_plan 应经统一 Runtime 执行并返回其稳定摘要。"""
        registry = get_default_tool_registry()
        service = FakeService()
        executor = AgentToolExecutor(
            registry=registry,
            pre_live_service=service,
            route_policy=_runtime_policy(),
        )
        obs = executor.execute(
            tool_name="generate_live_plan", arguments={"room_id": "room-001", "products": service.products},
            room_id="room-001", trace_id="trace-001",
        )
        assert obs.status == "success"
        assert obs.summary == "执行成功"


class TestParamValidation:
    """参数 schema 校验测试。"""

    def test_setup_without_approval_returns_pending(self):
        """兼容 setup 即使参数可补全，缺少可信审批也必须保持 pending。"""
        registry = get_default_tool_registry()
        service = FakeService()
        executor = AgentToolExecutor(
            registry=registry,
            pre_live_service=service,
            route_policy=_runtime_policy(),
        )
        obs = executor.execute(
            tool_name="setup_live_session",
            arguments={
                "plan_item_ids": ["p001"],
                "idempotency_key": "idem-agent-tool-001",
            },
            room_id="room-001", trace_id="trace-001",
        )
        assert obs.status == "pending"
        assert "APPROVAL_REQUIRED" in obs.summary

    def test_valid_params_passes_schema_check(self):
        """合法参数应通过校验。"""
        registry = get_default_tool_registry()
        service = FakeService()
        executor = AgentToolExecutor(registry=registry, pre_live_service=service)
        obs = executor.execute(
            tool_name="query_products", arguments={"room_id": "room-001"},
            room_id="room-001", trace_id="trace-001",
        )
        assert obs.status == "success"
