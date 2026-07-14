"""Phase 12A PlanEngine 领域模型的契约测试。

这些测试只校验计划创建前的冻结输入和只读视图，确保后续 Store、Worker 或
Graph 无法通过可变对象或不完整快照改变已经审计的计划身份。
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.plan_engine.models import (
    CandidatePlanNode,
    CardBatchPlanningInput,
    NodeRunView,
    PlanCommandType,
    PlanNodeKind,
    PlanNodeState,
    PlanNodeView,
    PlanRunState,
    PlanRunView,
    PlanVersionView,
)
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


def _product(product_id: str) -> CatalogProduct:
    """构造冻结商品快照，保持测试只关注 PlanEngine 输入约束。"""
    return CatalogProduct(
        product_id=product_id,
        name=f"商品 {product_id}",
        category="家居",
        price=Decimal("19.90"),
        inventory=20,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流款"],
        selling_points=["可审计卖点"],
    )


def _planning_input(product_ids: tuple[str, ...] = ("p001", "p002", "p003")) -> CardBatchPlanningInput:
    """构造完整排品与商品快照，使各测试可局部替换目标约束。"""
    items = [
        LivePlanItem(
            rank=index,
            product_id=product_id,
            product_name=f"商品 {product_id}",
            role="引流款",
            reason="测试排品理由",
        )
        for index, product_id in enumerate(product_ids, start=1)
    ]
    return CardBatchPlanningInput(
        room_id="room-001",
        trace_id="trace-001",
        live_plan=LivePlanDraft(room_id="room-001", trace_id="trace-001", items=items),
        products_by_id={product_id: _product(product_id) for product_id in product_ids},
    )


def test_planning_input_uses_canonical_snapshot_for_stable_run_key() -> None:
    """等价冻结输入必须得到相同 run_key，改变任一业务快照则必须改变身份。"""
    first = _planning_input()
    second = _planning_input()
    changed = CardBatchPlanningInput(
        room_id="room-001",
        trace_id="trace-001",
        live_plan=first.live_plan,
        products_by_id={**first.products_by_id, "p001": _product("p001").model_copy(update={"inventory": 21})},
    )

    assert first.run_key == second.run_key
    assert len(first.run_key) == 64
    assert first.run_key != changed.run_key


def test_live_plan_draft_rejects_empty_items() -> None:
    """空排品必须由 LivePlanDraft 自身的最小条目边界立即拒绝。"""
    # 空列表会在 LivePlanDraft 的 items 字段校验期失败，尚未构造 CardBatchPlanningInput。
    # 断言结构化错误位置和类型，避免依赖 Pydantic 随版本变化的英文错误文案。
    with pytest.raises(ValidationError) as captured_error:
        LivePlanDraft(
            room_id="room-001",
            trace_id="trace-001",
            items=[],
        )
    assert captured_error.value.errors()[0]["loc"] == ("items",)
    assert captured_error.value.errors()[0]["type"] == "too_short"


def test_planning_input_rejects_duplicate_or_missing_plan_products() -> None:
    """重复商品位和缺失商品快照必须由 CardBatchPlanningInput 闭合拒绝。"""
    # 这两类用例保留至少一个合法排品，确保断言命中的是规划输入的跨对象闭合规则。
    with pytest.raises(ValidationError, match="重复"):
        _planning_input(("p001", "p001"))
    with pytest.raises(ValidationError, match="缺少商品快照"):
        CardBatchPlanningInput(
            room_id="room-001",
            trace_id="trace-001",
            live_plan=_planning_input().live_plan,
            products_by_id={"p001": _product("p001")},
        )


@pytest.mark.parametrize(
    "default_container_factory",
    [
        lambda: CandidatePlanNode(
            logical_key="card:p001",
            node_kind=PlanNodeKind.SKILL,
            skill_id="generate_product_card",
        ).input_bindings,
        lambda: PlanRunView(
            plan_run_id="run-001",
            room_id="room-001",
            trace_id="trace-001",
            run_key="run-key-001",
            current_version=1,
            state=PlanRunState.ACTIVE,
        ).planning_input,
        lambda: PlanVersionView(
            plan_run_id="run-001",
            version_number=1,
            provider_id="fixture",
            provider_version="1.0.0",
        ).proposal,
        lambda: PlanNodeView(
            node_id="node-001",
            plan_run_id="run-001",
            version_number=1,
            logical_key="card:p001",
            node_kind=PlanNodeKind.SKILL,
            state=PlanNodeState.PENDING,
        ).input_bindings,
        lambda: NodeRunView(
            node_run_id="node-run-001",
            plan_run_id="run-001",
            node_id="node-001",
            attempt_number=1,
            state=PlanNodeState.PENDING,
        ).input_snapshot,
    ],
    ids=(
        "candidate-node-bindings",
        "plan-run-input",
        "plan-version-proposal",
        "plan-node-bindings",
        "node-run-input",
    ),
)
def test_default_json_containers_are_immutable(default_container_factory) -> None:
    """所有默认 JSON 容器也必须冻结，不能因未传入字段而泄漏可变 dict。"""
    # 每个工厂只省略一个 JSON 字段，精确覆盖 Pydantic 不执行默认值字段校验的路径。
    # 统一写入探针必须被 FrozenDict 拒绝，确保默认容器与显式传入的快照同样不可变。
    default_container = default_container_factory()
    with pytest.raises(TypeError):
        default_container["unexpected"] = True


def test_planning_input_rejects_product_snapshot_key_product_id_mismatch() -> None:
    """商品快照外层键必须与 CatalogProduct.product_id 一致，避免错误绑定。"""
    # 排品仍引用 p001，且外层快照键也存在 p001；仅将其内部商品标识替换为 p999，
    # 从而证明校验的是键和值的一致性，而非仅验证排品键能在字典中找到。
    request = _planning_input()
    with pytest.raises(ValidationError, match="product_id 不一致"):
        CardBatchPlanningInput(
            room_id="room-001",
            trace_id="trace-001",
            live_plan=request.live_plan,
            products_by_id={
                "p001": _product("p999"),
                "p002": request.products_by_id["p002"],
                "p003": request.products_by_id["p003"],
            },
        )


def test_planning_input_and_json_safe_views_are_immutable() -> None:
    """计划输入和查询视图均为冻结 JSON 安全事实，调用方不能原地改写。"""
    request = _planning_input()
    view = PlanRunView(
        plan_run_id="run-001",
        room_id="room-001",
        trace_id="trace-001",
        run_key=request.run_key,
        current_version=1,
        state=PlanRunState.ACTIVE,
        planning_input=request.model_dump(mode="json"),
    )
    node_run = NodeRunView(
        node_run_id="node-run-001",
        plan_run_id="run-001",
        node_id="node-001",
        attempt_number=1,
        state=PlanNodeState.RUNNING,
        input_snapshot={"product": {"product_id": "p001"}},
    )

    with pytest.raises(ValidationError):
        view.state = PlanRunState.SUCCEEDED
    with pytest.raises(TypeError):
        view.planning_input["room_id"] = "room-other"
    with pytest.raises(TypeError):
        node_run.input_snapshot["product"]["product_id"] = "p002"
    assert PlanCommandType.APPROVE.value == "APPROVE"
