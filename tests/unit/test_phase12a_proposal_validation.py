"""Phase 12A 固定候选 DAG 与 fail-closed 校验的测试。"""

from decimal import Decimal
import warnings

import pytest
from pydantic import ValidationError

from src.plan_engine.models import (
    CandidatePlanNode,
    CandidatePlanProposal,
    InputBinding,
    InputBindingKind,
    PlanNodeKind,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


def _planning_input():
    """构造四项排品，验证规范 Provider 只选择排品顺序中的前三项。"""
    from src.plan_engine.models import CardBatchPlanningInput

    product_ids = ("p001", "p002", "p003", "p004")
    return CardBatchPlanningInput(
        room_id="room-001",
        trace_id="trace-001",
        live_plan=LivePlanDraft(
            room_id="room-001",
            trace_id="trace-001",
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product_id,
                    product_name=f"商品 {product_id}",
                    role="引流款",
                    reason="测试排品理由",
                )
                for index, product_id in enumerate(product_ids, start=1)
            ],
        ),
        products_by_id={
            product_id: CatalogProduct(
                product_id=product_id,
                name=f"商品 {product_id}",
                category="家居",
                price=Decimal("19.90"),
                inventory=20,
                conversion_rate=Decimal("0.20"),
                commission_rate=Decimal("0.10"),
            )
            for product_id in product_ids
        },
    )


def test_canonical_provider_materializes_prepare_cards_and_collect() -> None:
    """规范 Provider 必须生成固定控制节点和前三个单商品手卡节点。"""
    provider = CanonicalCardBatchProposalProvider()
    proposal = provider.propose_sync(_planning_input())

    assert provider.provider_id == "canonical-card-batch"
    assert provider.provider_version == "1.0.0"
    assert [node.logical_key for node in proposal.nodes] == [
        "prepare-card-batch",
        "card:p001",
        "card:p002",
        "card:p003",
        "collect-card-results",
    ]
    assert proposal.nodes[0].node_kind is PlanNodeKind.CONTROL
    assert proposal.nodes[0].skill_id is None
    assert proposal.nodes[1].skill_id == "generate_product_card"
    assert proposal.nodes[1].input_bindings["product"] == InputBinding(
        kind=InputBindingKind.PLAN_INPUT,
        path=("products_by_id", "p001"),
    )
    assert proposal.nodes[-1].depends_on == ("card:p001", "card:p002", "card:p003")


@pytest.mark.parametrize(
    ("node_kind", "skill_id", "message"),
    [
        (
            PlanNodeKind.CONTROL,
            "generate_product_card",
            "CONTROL 节点不能携带 skill_id",
        ),
        (PlanNodeKind.SKILL, None, "SKILL 节点必须提供 skill_id"),
    ],
)
def test_candidate_plan_node_rejects_invalid_node_kind_combinations(
    node_kind: PlanNodeKind,
    skill_id: str | None,
    message: str,
) -> None:
    """节点级非法组合必须在节点构造时拒绝，避免错误延迟到候选图校验阶段。"""
    # 参数化数据只保存原始枚举和值，非法节点必须在测试函数内构造并由 raises 捕获，
    # 防止 pytest 导入模块时提前触发 Pydantic 的严格节点级校验而导致测试收集失败。
    with pytest.raises(ValidationError, match=message):
        CandidatePlanNode(
            logical_key="card:p001",
            node_kind=node_kind,
            skill_id=skill_id,
        )


@pytest.mark.parametrize(
    ("node_specs", "message"),
    [
        ((), "候选 DAG 不能为空"),
        (
            (
                {
                    "logical_key": "card:p001",
                    "node_kind": PlanNodeKind.SKILL,
                    "skill_id": "generate_product_card",
                },
                {
                    "logical_key": "card:p001",
                    "node_kind": PlanNodeKind.SKILL,
                    "skill_id": "generate_product_card",
                },
            ),
            "重复 logical_key",
        ),
        (
            (
                {
                    "logical_key": "card:p001",
                    "node_kind": PlanNodeKind.SKILL,
                    "skill_id": "generate_product_card",
                    "depends_on": ("card:p002",),
                },
                {
                    "logical_key": "card:p002",
                    "node_kind": PlanNodeKind.SKILL,
                    "skill_id": "generate_product_card",
                    "depends_on": ("card:p001",),
                },
            ),
            "存在环",
        ),
        (
            (
                {
                    "logical_key": "card:p001",
                    "node_kind": PlanNodeKind.SKILL,
                    "skill_id": "generate_product_card",
                    "depends_on": ("unknown",),
                },
            ),
            "未知依赖",
        ),
        (
            (
                {
                    "logical_key": "card:p001",
                    "node_kind": PlanNodeKind.SKILL,
                    "skill_id": "generate_product_card",
                    "input_bindings": {
                        "product": InputBinding(
                            kind=InputBindingKind.NODE_OUTPUT,
                            upstream_logical_key="prepare-card-batch",
                            path=("product",),
                        )
                    }
                },
            ),
            "未声明上游依赖",
        ),
    ],
)
def test_candidate_proposal_rejects_invalid_dag_shapes(node_specs, message: str) -> None:
    """非法候选必须在模型校验期拒绝，绝不能由 Provider 或其他路径降级掩盖。"""
    # 图级测试只关心 Proposal 对节点关系的验证；model_construct 在此显式绕过节点
    # 构造期校验，使节点规格在执行测试时才交给 Proposal 的嵌套模型与 DAG 校验处理。
    nodes = tuple(CandidatePlanNode.model_construct(**node_spec) for node_spec in node_specs)
    with pytest.raises(ValidationError, match=message):
        CandidatePlanProposal(
            provider_id="fixture",
            provider_version="1.0.0",
            nodes=nodes,
        )


def test_candidate_proposal_rejects_unknown_binding_kind() -> None:
    """绑定来源是受控枚举，任意未声明字符串必须 fail-closed。"""
    with pytest.raises(ValidationError, match="InputBindingKind|kind"):
        InputBinding.model_validate({"kind": "ENVIRONMENT", "path": ["secret"]})


def test_candidate_proposal_rejects_constructed_unknown_binding_kind() -> None:
    """即使嵌套绑定绕过 Pydantic 构造，Proposal 也必须拒绝未知来源。"""
    # 先使用 model_construct 模拟不受信任调用方绕过 InputBinding 的字段枚举校验，
    # 再将其嵌入同样显式构造的节点，确保断言命中 Proposal 的 fail-closed 图级复核。
    bypassed_binding = InputBinding.model_construct(
        kind="ENVIRONMENT",
        path=("secret",),
    )
    bypassed_node = CandidatePlanNode.model_construct(
        logical_key="card:p001",
        node_kind=PlanNodeKind.SKILL,
        skill_id="generate_product_card",
        input_bindings={"secret": bypassed_binding},
    )
    # Proposal 复用 InputBinding 的标准枚举错误，断言字段语义而不绑定特定英文措辞。
    with pytest.raises(ValidationError, match="PLAN_INPUT|NODE_OUTPUT|LITERAL|kind"):
        CandidatePlanProposal(
            provider_id="fixture",
            provider_version="1.0.0",
            nodes=(bypassed_node,),
        )


@pytest.mark.parametrize(
    ("binding_kwargs", "message"),
    [
        (
            {
                "kind": InputBindingKind.LITERAL,
                "path": ("forbidden-path",),
                "literal_value": None,
            },
            "LITERAL 不能提供 path",
        ),
        (
            {
                "kind": InputBindingKind.LITERAL,
                "path": (),
                "literal_value": object(),
            },
            "不是 JSON-safe 类型",
        ),
    ],
    ids=("literal-path", "literal-json-safe"),
)
def test_candidate_proposal_revalidates_constructed_binding_shape(
    binding_kwargs: dict[str, object],
    message: str,
) -> None:
    """Proposal 必须重新验证绕过构造的绑定完整形状和 JSON-safe 常量。"""
    # 仅 binding 与其所属节点使用 model_construct 绕过局部验证；Proposal 仍正常构造，
    # 以证明图级信任边界会重新走 InputBinding 的标准 Pydantic 验证而非延迟到序列化。
    bypassed_binding = InputBinding.model_construct(**binding_kwargs)
    bypassed_node = CandidatePlanNode.model_construct(
        logical_key="card:p001",
        node_kind=PlanNodeKind.SKILL,
        skill_id="generate_product_card",
        input_bindings={"literal": bypassed_binding},
    )
    with pytest.raises(ValidationError, match=message):
        CandidatePlanProposal(
            provider_id="fixture",
            provider_version="1.0.0",
            nodes=(bypassed_node,),
        )


def test_candidate_proposal_normalizes_constructed_literal_binding() -> None:
    """Proposal 必须将绕过构造的合法 Literal 重新物化为冻结节点。"""
    # binding 与节点都通过 model_construct 保留普通 dict/list；Proposal 正常构造后，
    # 其 nodes 必须替换为完整验证后的不可变对象，且 JSON 导出不能产生序列化警告。
    bypassed_binding = InputBinding.model_construct(
        kind=InputBindingKind.LITERAL,
        path=(),
        literal_value={"nested": []},
    )
    bypassed_node = CandidatePlanNode.model_construct(
        logical_key="card:p001",
        node_kind=PlanNodeKind.SKILL,
        skill_id="generate_product_card",
        input_bindings={"literal": bypassed_binding},
    )
    proposal = CandidatePlanProposal(
        provider_id="fixture",
        provider_version="1.0.0",
        nodes=(bypassed_node,),
    )

    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always")
        assert proposal.model_dump_json()
    assert not captured_warnings
    with pytest.raises(TypeError):
        proposal.nodes[0].input_bindings["literal"].literal_value["nested"].append("changed")


def test_candidate_proposal_rejects_constructed_node_with_raw_invalid_binding_dict() -> None:
    """原始 binding dict 也必须经节点重建返回 ValidationError，不能泄漏 AttributeError。"""
    # 此字典绕过了 InputBinding 构造且违反 LITERAL 不得携带 path 的既有规则，
    # 用于证明 Proposal 首先执行 CandidatePlanNode 的标准验证，而非直接访问 dict.model_dump。
    bypassed_node = CandidatePlanNode.model_construct(
        logical_key="card:p001",
        node_kind=PlanNodeKind.SKILL,
        skill_id="generate_product_card",
        input_bindings={
            "literal": {
                "kind": InputBindingKind.LITERAL,
                "path": ("forbidden-path",),
                "literal_value": None,
            }
        },
    )
    with pytest.raises(ValidationError, match="LITERAL 不能提供 path"):
        CandidatePlanProposal(
            provider_id="fixture",
            provider_version="1.0.0",
            nodes=(bypassed_node,),
        )
