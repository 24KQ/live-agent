"""Phase 12A 真实 PlanStore/Worker 与播前 Graph 的局部路由集成测试。

测试使用内存 PlanStore、真实 SkillExecutor/PlanWorker 和官方 InMemorySaver，不连接
LLM、淘宝 API 或外部数据库。目标是证明 Graph 只在 PlanStore 成功事实提交后保存
最小 checkpoint 引用，并且卡片结果来自统一 Runtime 而非 Legacy 双执行。
"""

from __future__ import annotations

from decimal import Decimal
import importlib
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from src.core.pre_live_graph import (
    build_pre_live_graph,
    create_initial_pre_live_graph_state,
    create_pre_live_graph_config,
)
from src.core.security_hooks import GateDecision, GateResult
from src.plan_engine.models import CardBatchPlanningInput
from src.plan_engine.store import InMemoryPlanStore
from src.plan_engine.worker import PlanWorker, SyncPlanWorkerAdapter
from src.skill_runtime.executor import SkillExecutor
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_card_generator import generate_product_card
from src.skills.product_catalog import CatalogProduct


def _product(product_id: str, rank: int) -> CatalogProduct:
    """构造实际 Runtime Handler 可以直接生成手卡的完整商品。"""
    return CatalogProduct(
        product_id=product_id,
        name=f"集成商品 {rank}",
        category="家居",
        price=Decimal("29.90"),
        inventory=30,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流款"],
        selling_points=["卖点一", "卖点二", "卖点三"],
    )


class _GraphBusinessService:
    """提供查询、排品和建播，Legacy 手卡入口一旦调用就使测试失败。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.products = [_product(f"p00{index}", index) for index in range(1, 5)]

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """返回四个商品，验证 PlanEngine 仍只生成前三张手卡。"""
        self.calls.append("query_products")
        return self.products

    def generate_plan(
        self,
        room_id: str,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> LivePlanDraft:
        """生成与 Graph trace 对齐的固定排品快照。"""
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
                    reason="Phase 12A Graph 路由集成",
                )
                for index, product in enumerate(products, start=1)
            ],
        )

    def generate_cards(self, *_: Any, **__: Any) -> list[Any]:
        """该方法在 PLAN_ENGINE 路由下不可达。"""
        self.calls.append("generate_product_cards")
        raise AssertionError("PLAN_ENGINE 路由不得调用 Legacy generate_cards")

    def setup_live_session(
        self,
        room_id: str,
        plan: LivePlanDraft,
        trace_id: str,
        confirmed_setup: bool,
        **_: Any,
    ) -> tuple[GateResult, str | None]:
        """保持原建播 hard-gate，不给 Task 7 扩大高风险权限。"""
        self.calls.append("setup_live_session")
        return (
            GateResult(False, GateDecision.HARD_GATE, True, "仍需人工确认"),
            None,
        )


class _ProductCardHandler:
    """真实 SkillExecutor 使用的确定性单次 Handler。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        """从 Worker 物化的单商品参数生成完整 ProductCard 快照。"""
        self.calls += 1
        product = CatalogProduct.model_validate(arguments["product"])
        return {"card": generate_product_card(product).model_dump(mode="json")}


def test_real_plan_worker_route_persists_cards_before_checkpoint_reference() -> None:
    """真实 Worker 完成固定 DAG 后，Graph 才能写出三张手卡和最小计划引用。"""
    try:
        routing = importlib.import_module("src.plan_engine.routing")
        service_api = importlib.import_module("src.plan_engine.service")
    except ModuleNotFoundError:
        pytest.fail("尚未实现 Phase 12A Task 7 路由服务", pytrace=False)
    store = InMemoryPlanStore()
    handler = _ProductCardHandler()
    worker = PlanWorker(
        store=store,
        skill_executor=SkillExecutor(
            handlers={"generate_product_card": handler},  # type: ignore[dict-item]
        ),
        worker_id="phase12a-graph-worker",
    )
    plan_service = service_api.DefaultCardBatchPlanService(
        store=store,
        worker=SyncPlanWorkerAdapter(worker),
    )
    business_service = _GraphBusinessService()
    checkpointer = InMemorySaver()
    graph = build_pre_live_graph(
        business_service,
        plan_execution_policy=routing.PlanExecutionPolicy(route="PLAN_ENGINE"),
        card_batch_plan_service=plan_service,
        checkpointer=checkpointer,
    )
    trace_id = "trace-phase12a-plan-route"
    config = create_pre_live_graph_config(trace_id)

    result = graph.invoke(
        create_initial_pre_live_graph_state(
            room_id="room-001",
            trace_id=trace_id,
            confirmed_setup=False,
        ),
        config=config,
    )

    assert handler.calls == 3
    assert "generate_product_cards" not in business_service.calls
    assert [card["product_id"] for card in result["cards_snapshot"]] == [
        "p001",
        "p002",
        "p003",
    ]
    assert store.get_plan_run(result["plan_run_id"]).state == "SUCCEEDED"
    assert result["plan_checkpoint_reference"] == {
        "plan_run_id": result["plan_run_id"],
        "plan_version": 1,
        "control_position": "CARD_BATCH_SUCCEEDED",
    }
    checkpoint_tuple = checkpointer.get_tuple(config)
    assert checkpoint_tuple is not None
    assert (
        checkpoint_tuple.checkpoint["channel_values"]["plan_checkpoint_reference"]
        == result["plan_checkpoint_reference"]
    )

    # 使用 Store 中的冻结输入模拟旧 checkpoint 重放。同一 run_key 必须命中原
    # PlanRun，drive_to_terminal 只读取 COLLECT 结果，不产生第四次 Skill 调用。
    persisted_input = CardBatchPlanningInput.model_validate(
        store.get_plan_run(result["plan_run_id"]).planning_input
    )
    replay_ref = plan_service.create_or_resume(persisted_input)
    replay_result = plan_service.drive_to_terminal(replay_ref.plan_run_id)
    assert replay_ref.plan_run_id == result["plan_run_id"]
    assert [card["product_id"] for card in replay_result.cards_snapshot] == [
        "p001",
        "p002",
        "p003",
    ]
    assert handler.calls == 3
