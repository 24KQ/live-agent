"""Phase 12A 播前手卡 PlanEngine 路由的 TDD 契约测试。

本文件只验证装配期路由和 Graph 局部切换：默认继续走 Legacy；显式打开
PLAN_ENGINE 后只有手卡节点切换，任何 PlanEngine 异常都必须原样失败，不能在同次
Graph 调用中回退 Legacy。真实 Worker 与 checkpoint 的组合证据放在集成测试中。
"""

from __future__ import annotations

from decimal import Decimal
import importlib
from typing import Any

import pytest
from pydantic import ValidationError

from src.config.settings import Settings
from src.core.pre_live_graph import (
    build_pre_live_graph,
    create_initial_pre_live_graph_state,
)
from src.core.security_hooks import GateDecision, GateResult
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_card_generator import ProductCard
from src.skills.product_catalog import CatalogProduct


def _routing_api() -> Any:
    """延迟导入待实现模块，让 RED 表现为明确断言而不是收集阶段错误。"""
    try:
        return importlib.import_module("src.plan_engine.routing")
    except ModuleNotFoundError:
        pytest.fail("尚未实现 src.plan_engine.routing", pytrace=False)


def _service_api() -> Any:
    """延迟读取服务类型，保持第一个红灯直接指向缺失的 Task 7 接口。"""
    return importlib.import_module("src.plan_engine.service")


def _product(product_id: str, rank: int) -> CatalogProduct:
    """构造 Graph 快照和计划输入使用的完整确定性商品。"""
    return CatalogProduct(
        product_id=product_id,
        name=f"商品 {rank}",
        category="家居",
        price=Decimal("19.90"),
        inventory=20,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流款"],
        selling_points=["可审计卖点"],
    )


def _card(product_id: str) -> ProductCard:
    """构造可由现有 Graph 快照函数恢复的完整手卡。"""
    return ProductCard(
        product_id=product_id,
        title=f"{product_id} 手卡",
        talking_points=["卖点一", "卖点二", "卖点三"],
        opening_script="确定性开场话术。",
        price_hint="价格以直播间当前展示为准。",
        risk_tips=["避免绝对化承诺。"],
    )


class _LegacyService:
    """记录 Graph 业务调用，证明 Task 7 只替换手卡生成节点。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.products = [_product(f"p00{index}", index) for index in range(1, 4)]

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """返回固定货盘并记录查询调用。"""
        self.calls.append("query_products")
        return self.products

    def generate_plan(
        self,
        room_id: str,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> LivePlanDraft:
        """返回覆盖全部商品的固定排品。"""
        self.calls.append("generate_live_plan")
        return LivePlanDraft(
            room_id=room_id,
            trace_id=trace_id,
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product.product_id,
                    product_name=product.name,
                    role="引流款",
                    reason="Task 7 路由测试",
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
        """Legacy 手卡路径；PLAN_ENGINE 模式下该方法不得被调用。"""
        self.calls.append("generate_product_cards")
        return [_card(item.product_id) for item in plan.items[:3]]

    def setup_live_session(
        self,
        room_id: str,
        plan: LivePlanDraft,
        trace_id: str,
        confirmed_setup: bool,
        **_: Any,
    ) -> tuple[GateResult, str | None]:
        """保留既有建播门禁行为，证明路由没有接管该节点。"""
        self.calls.append("setup_live_session")
        return (
            GateResult(
                allowed=confirmed_setup,
                decision=GateDecision.HARD_GATE,
                requires_confirmation=not confirmed_setup,
                reason="Task 7 固定门禁",
            ),
            "audit-setup" if confirmed_setup else None,
        )


class _CardBatchPlanService:
    """返回预设 PlanRun 结果的轻量替身，并保存 Graph 传入的冻结输入。"""

    def __init__(self, *, fail: bool = False, terminal_status: str = "SUCCEEDED") -> None:
        self.fail = fail
        self.terminal_status = terminal_status
        self.requests: list[Any] = []
        self.driven_plan_run_ids: list[str] = []

    def create_or_resume(self, request: Any) -> Any:
        """记录请求并返回固定计划引用。"""
        self.requests.append(request)
        return _service_api().CardBatchPlanRef(
            plan_run_id="plan-run-001",
            plan_version=1,
        )

    def drive_to_terminal(self, plan_run_id: str) -> Any:
        """成功时返回三张手卡；故障模式用于验证禁止 Legacy fallback。"""
        self.driven_plan_run_ids.append(plan_run_id)
        if self.fail:
            raise RuntimeError("plan engine unavailable")
        cards_snapshot = (
            tuple(
                _card(f"p00{index}").model_dump(mode="json")
                for index in range(1, 4)
            )
            if self.terminal_status == "SUCCEEDED"
            else ()
        )
        return _service_api().CardBatchExecutionResult(
            plan_run_id=plan_run_id,
            plan_version=1,
            status=self.terminal_status,
            cards_snapshot=cards_snapshot,
        )


def _initial_state(trace_id: str) -> dict[str, Any]:
    """创建禁用人审且不批准建播的最小 Graph 输入。"""
    return create_initial_pre_live_graph_state(
        room_id="room-001",
        trace_id=trace_id,
        confirmed_setup=False,
    )


def test_default_route_is_legacy_and_rejects_unknown_value() -> None:
    """新配置必须 fail-safe 默认为 Legacy，并在加载期拒绝未知路由。"""
    routing = _routing_api()
    settings = Settings(_env_file=None)

    assert settings.plan_engine_card_execution_route == "LEGACY"
    assert routing.PlanExecutionPolicy.from_settings(settings).route == "LEGACY"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PLAN_ENGINE_CARD_EXECUTION_ROUTE="SHADOW_COMPARE")


def test_policy_copies_settings_value_at_assembly_time() -> None:
    """修改 Settings 实例不得改变已经装配的 PlanExecutionPolicy。"""
    routing = _routing_api()
    settings = Settings(_env_file=None)
    policy = routing.PlanExecutionPolicy.from_settings(settings)

    settings.plan_engine_card_execution_route = "PLAN_ENGINE"

    assert policy.route == routing.PlanExecutionRoute.LEGACY


def test_default_graph_route_keeps_existing_generate_cards_call() -> None:
    """未显式注入 PlanEngine 路由时，现有 Graph 行为和状态必须保持兼容。"""
    legacy = _LegacyService()
    graph = build_pre_live_graph(legacy)

    result = graph.invoke(_initial_state("trace-route-legacy"))

    assert legacy.calls.count("generate_product_cards") == 1
    assert result["plan_run_id"] is None
    assert result["plan_version"] is None
    assert result["plan_execution_status"] is None


def test_plan_engine_route_uses_frozen_snapshots_and_skips_legacy_cards() -> None:
    """PlanEngine 只消费既有排品/商品快照，并把最小引用写回 Graph state。"""
    routing = _routing_api()
    legacy = _LegacyService()
    plan_service = _CardBatchPlanService()
    graph = build_pre_live_graph(
        legacy,
        plan_execution_policy=routing.PlanExecutionPolicy(route="PLAN_ENGINE"),
        card_batch_plan_service=plan_service,
    )

    result = graph.invoke(_initial_state("trace-route-plan-engine"))

    assert "generate_product_cards" not in legacy.calls
    assert plan_service.driven_plan_run_ids == ["plan-run-001"]
    request = plan_service.requests[0]
    assert request.room_id == "room-001"
    assert request.trace_id == "trace-route-plan-engine"
    assert list(request.products_by_id) == ["p001", "p002", "p003"]
    assert result["plan_run_id"] == "plan-run-001"
    assert result["plan_version"] == 1
    assert result["plan_execution_status"] == "SUCCEEDED"
    assert result["card_count"] == 3
    assert result["plan_checkpoint_reference"] == {
        "plan_run_id": "plan-run-001",
        "plan_version": 1,
        "control_position": "CARD_BATCH_SUCCEEDED",
    }


def test_plan_engine_exception_never_falls_back_to_legacy_cards() -> None:
    """PlanEngine 注入失败必须终止本次 Graph，不得偷偷调用 Legacy 再伪装成功。"""
    routing = _routing_api()
    legacy = _LegacyService()
    graph = build_pre_live_graph(
        legacy,
        plan_execution_policy=routing.PlanExecutionPolicy(route="PLAN_ENGINE"),
        card_batch_plan_service=_CardBatchPlanService(fail=True),
    )

    with pytest.raises(RuntimeError, match="plan engine unavailable"):
        graph.invoke(_initial_state("trace-route-no-fallback"))

    assert "generate_product_cards" not in legacy.calls


def test_failed_plan_writes_failed_reference_and_stops_before_setup() -> None:
    """终态失败必须可 checkpoint 对账，并在建播前停止后续业务节点。"""
    routing = _routing_api()
    legacy = _LegacyService()
    graph = build_pre_live_graph(
        legacy,
        plan_execution_policy=routing.PlanExecutionPolicy(route="PLAN_ENGINE"),
        card_batch_plan_service=_CardBatchPlanService(terminal_status="FAILED"),
    )

    result = graph.invoke(_initial_state("trace-route-terminal-failure"))

    assert result["plan_execution_status"] == "FAILED"
    assert result["error"] == "CARD_BATCH_PLAN_FAILED"
    assert result["card_count"] == 0
    assert result["plan_checkpoint_reference"]["control_position"] == "CARD_BATCH_FAILED"
    assert "generate_product_cards" not in legacy.calls
    assert "setup_live_session" not in legacy.calls


def test_plan_engine_route_requires_explicit_service_at_graph_assembly() -> None:
    """显式打开新路由却未注入服务时必须启动失败，不能运行期回退 Legacy。"""
    routing = _routing_api()

    with pytest.raises(ValueError, match="CardBatchPlanService"):
        build_pre_live_graph(
            _LegacyService(),
            plan_execution_policy=routing.PlanExecutionPolicy(route="PLAN_ENGINE"),
        )


def test_card_batch_service_rejects_candidate_bound_to_unknown_product() -> None:
    """候选引用冻结输入外商品时必须在创建 PlanRun 前失败，不能延迟到 Worker。"""
    from src.plan_engine.models import (
        CandidatePlanNode,
        CandidatePlanProposal,
        CardBatchPlanningInput,
        InputBinding,
        PlanNodeKind,
    )
    from src.plan_engine.store import InMemoryPlanStore, PlanStoreInvariantError

    class _UnknownProductProvider:
        """生成拓扑合法但业务输入引用越界的受控候选。"""

        def propose_sync(self, request: CardBatchPlanningInput) -> CandidatePlanProposal:
            """把唯一手卡节点绑定到冻结货盘中不存在的商品。"""
            return CandidatePlanProposal(
                provider_id="unknown-product-fixture",
                provider_version="1.0.0",
                nodes=(
                    CandidatePlanNode(
                        logical_key="prepare-card-batch",
                        node_kind=PlanNodeKind.CONTROL,
                    ),
                    CandidatePlanNode(
                        logical_key="card:missing",
                        node_kind=PlanNodeKind.SKILL,
                        skill_id="generate_product_card",
                        depends_on=("prepare-card-batch",),
                        input_bindings={
                            "product": InputBinding(
                                kind="PLAN_INPUT",
                                path=("products_by_id", "missing"),
                            )
                        },
                    ),
                    CandidatePlanNode(
                        logical_key="collect-card-results",
                        node_kind=PlanNodeKind.CONTROL,
                        depends_on=("card:missing",),
                    ),
                ),
            )

    class _UnusedWorker:
        """该场景必须在 Worker 前失败，任何调用都表示校验过晚。"""

        def run_once(self, plan_run_id: str, **_: Any) -> Any:
            raise AssertionError("非法候选不得进入 Worker")

    legacy = _LegacyService()
    plan = legacy.generate_plan("room-001", legacy.products, "trace-invalid-candidate")
    request = CardBatchPlanningInput(
        room_id="room-001",
        trace_id="trace-invalid-candidate",
        live_plan=plan,
        products_by_id={item.product_id: item for item in legacy.products},
    )
    store = InMemoryPlanStore()
    service = _service_api().DefaultCardBatchPlanService(
        store=store,
        worker=_UnusedWorker(),
        proposal_provider=_UnknownProductProvider(),
    )

    with pytest.raises(PlanStoreInvariantError, match="冻结输入"):
        service.create_or_resume(request)

    assert store.list_plan_runs(include_terminal=True) == ()
