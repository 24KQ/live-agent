"""Phase 12A 输入绑定和能力配置的契约测试。

测试通过真实的冻结规划输入和 Catalog 验证：候选 DAG 只能读取已声明的
JSON 事实，且执行能力的版本、风险与资源锁只能由可信 Catalog 补全。
"""

from decimal import Decimal
from hashlib import sha256
import json

import pytest

from src.plan_engine.bindings import (
    InputBindingResolver,
    MaterializedNodeInput,
    PlanValidationError,
    VersionedNodeOutput,
)
from src.plan_engine.capabilities import PlanCapabilityError, PlanCapabilityProfile
from src.plan_engine.models import CardBatchPlanningInput, InputBinding
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


def _planning_input() -> CardBatchPlanningInput:
    """构造最小而完整的规划快照，避免测试依赖外部货盘或运行时状态。"""
    product = CatalogProduct(
        product_id="p001",
        name="测试商品",
        category="家居",
        price=Decimal("19.90"),
        inventory=20,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流款"],
        selling_points=["可审计卖点"],
    )
    return CardBatchPlanningInput(
        room_id="room-1",
        trace_id="trace-1",
        live_plan=LivePlanDraft(
            room_id="room-1",
            trace_id="trace-1",
            items=[
                LivePlanItem(
                    rank=1,
                    product_id="p001",
                    product_name="测试商品",
                    role="引流款",
                    reason="测试用排品理由",
                )
            ],
        ),
        products_by_id={"p001": product},
    )


def test_binding_resolver_only_reads_declared_sources() -> None:
    """PLAN_INPUT 可读，NODE_OUTPUT 必须先出现在当前节点的显式依赖中。"""
    resolver = InputBindingResolver()
    value = resolver.resolve(
        InputBinding(kind="PLAN_INPUT", path=("products_by_id", "p001")),
        planning_input=_planning_input(),
        dependency_outputs={},
        declared_dependencies=frozenset(),
    )
    assert value["product_id"] == "p001"

    # 即便调用方恰好传入了该输出，未在 DAG 边上声明仍不可读取，防止隐式数据依赖。
    with pytest.raises(PlanValidationError, match="未声明依赖"):
        resolver.resolve(
            InputBinding(
                kind="NODE_OUTPUT",
                upstream_logical_key="prepare-card-batch",
                path=("products",),
            ),
            planning_input=_planning_input(),
            dependency_outputs={"prepare-card-batch": {"products": []}},
            declared_dependencies=frozenset(),
        )


def test_node_output_rejects_plain_mapping_without_version_fact() -> None:
    """普通映射即使满足依赖闭包，也不能冒充具有明确版本归属的节点输出。"""
    binding = InputBinding(
        kind="NODE_OUTPUT",
        upstream_logical_key="prepare-card-batch",
        path=("products",),
    )

    # 同时提供依赖声明、实际输出和当前版本，确保拒绝原因只可能是上游值本身
    # 没有 VersionedNodeOutput 所承载的 PlanVersion 事实。
    with pytest.raises(PlanValidationError, match="缺少版本事实"):
        InputBindingResolver().resolve(
            binding,
            planning_input=_planning_input(),
            dependency_outputs={"prepare-card-batch": {"products": ["p001"]}},
            declared_dependencies=frozenset({"prepare-card-batch"}),
            current_plan_version=12,
        )


def test_node_output_rejects_missing_current_plan_version() -> None:
    """上游输出已有版本事实时，调用方仍必须声明本次读取所属的当前版本。"""
    binding = InputBinding(
        kind="NODE_OUTPUT",
        upstream_logical_key="prepare-card-batch",
        path=("products",),
    )

    with pytest.raises(PlanValidationError, match="必须提供当前计划版本"):
        InputBindingResolver().resolve(
            binding,
            planning_input=_planning_input(),
            dependency_outputs={
                "prepare-card-batch": VersionedNodeOutput(
                    plan_version=12,
                    output={"products": ["p001"]},
                )
            },
            declared_dependencies=frozenset({"prepare-card-batch"}),
        )


def test_node_output_rejects_mismatched_plan_version() -> None:
    """当前计划版本与输出归属不一致时必须拒绝，禁止读取旧 DAG 的节点结果。"""
    binding = InputBinding(
        kind="NODE_OUTPUT",
        upstream_logical_key="prepare-card-batch",
        path=("products",),
    )

    with pytest.raises(PlanValidationError, match="禁止跨版本读取"):
        InputBindingResolver().resolve(
            binding,
            planning_input=_planning_input(),
            dependency_outputs={
                "prepare-card-batch": VersionedNodeOutput(
                    plan_version=11,
                    output={"products": ["p001"]},
                )
            },
            declared_dependencies=frozenset({"prepare-card-batch"}),
            current_plan_version=12,
        )


@pytest.mark.parametrize(
    "invalid_plan_version",
    [True, 1.0, 0, -1],
    ids=["bool", "float", "zero", "negative"],
)
def test_node_output_rejects_invalid_output_plan_version(
    invalid_plan_version: object,
) -> None:
    """上游版本必须是大于等于 1 的精确 int，不能借 Python 数值相等规则通过。"""
    binding = InputBinding(
        kind="NODE_OUTPUT",
        upstream_logical_key="prepare-card-batch",
        path=("products",),
    )

    with pytest.raises(PlanValidationError, match="上游输出计划版本"):
        InputBindingResolver().resolve(
            binding,
            planning_input=_planning_input(),
            dependency_outputs={
                "prepare-card-batch": VersionedNodeOutput(
                    plan_version=invalid_plan_version,
                    output={"products": ["p001"]},
                )
            },
            declared_dependencies=frozenset({"prepare-card-batch"}),
            current_plan_version=1,
        )


@pytest.mark.parametrize(
    "invalid_current_plan_version",
    [True, 1.0, 0, -1],
    ids=["bool", "float", "zero", "negative"],
)
def test_node_output_rejects_invalid_current_plan_version(
    invalid_current_plan_version: object,
) -> None:
    """当前版本同样必须是正整数，非法值不能先参与跨版本一致性比较。"""
    binding = InputBinding(
        kind="NODE_OUTPUT",
        upstream_logical_key="prepare-card-batch",
        path=("products",),
    )

    with pytest.raises(PlanValidationError, match="当前计划版本"):
        InputBindingResolver().resolve(
            binding,
            planning_input=_planning_input(),
            dependency_outputs={
                "prepare-card-batch": VersionedNodeOutput(
                    plan_version=1,
                    output={"products": ["p001"]},
                )
            },
            declared_dependencies=frozenset({"prepare-card-batch"}),
            current_plan_version=invalid_current_plan_version,
        )


def test_node_output_reads_matching_plan_version() -> None:
    """仅当输出归属与当前 PlanVersion 精确一致时，才沿静态路径返回 JSON 值。"""
    binding = InputBinding(
        kind="NODE_OUTPUT",
        upstream_logical_key="prepare-card-batch",
        path=("products",),
    )

    value = InputBindingResolver().resolve(
        binding,
        planning_input=_planning_input(),
        dependency_outputs={
            "prepare-card-batch": VersionedNodeOutput(
                plan_version=12,
                output={"products": ["p001"]},
            )
        },
        declared_dependencies=frozenset({"prepare-card-batch"}),
        current_plan_version=12,
    )

    assert value == ["p001"]


def test_materialized_node_input_direct_construction_freezes_canonical_snapshot() -> None:
    """直接构造值对象时也必须复制并递归冻结参数，不能依赖 resolver 调用路径。"""
    source_parameters = {"nested": []}
    expected_fingerprint = sha256(
        json.dumps(
            source_parameters,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    materialized = MaterializedNodeInput(
        parameters=source_parameters,
        input_fingerprint=expected_fingerprint,
    )

    # 顶层 object 与嵌套 array 必须分别关闭原地写入入口，证明 frozen dataclass
    # 不只是冻结字段引用，而是让指纹对应的整棵 JSON 参数树成为不可变审计快照。
    with pytest.raises(TypeError):
        materialized.parameters["extra"] = True
    with pytest.raises(TypeError):
        materialized.parameters["nested"].append("tampered")

    # 构造方仍持有的原始容器也不能反向改写值对象，避免外部别名使参数与指纹脱节。
    source_parameters["nested"].append("source-mutated")
    assert materialized.parameters == {"nested": []}


def test_materialized_node_input_direct_construction_rejects_fingerprint_mismatch() -> None:
    """调用方提供的指纹若不匹配规范 JSON，值对象必须在构造期 fail-closed。"""
    with pytest.raises(PlanValidationError, match="指纹"):
        MaterializedNodeInput(
            parameters={"nested": []},
            input_fingerprint="0" * 64,
        )


@pytest.mark.parametrize(
    "parameters",
    [
        {"items": ("p001", "p002")},
        {"payload": b"not-json"},
        {"number": float("nan")},
        {1: "非字符串键"},
    ],
    ids=["tuple", "bytes", "nan", "non-string-key"],
)
def test_materialized_node_input_direct_construction_rejects_non_strict_json(
    parameters: dict[object, object],
) -> None:
    """直接构造边界同样拒绝隐式转换或无法稳定持久化的非严格 JSON 参数。"""
    # 正常 InputBinding 会把 Sequence 规范化成 FrozenList；这里的 tuple 代表
    # model_construct 等非类型化入口留下的原始值，运行时值对象必须继续拒绝它。
    with pytest.raises(PlanValidationError, match="物化参数不是普通 JSON"):
        MaterializedNodeInput(
            parameters=parameters,  # type: ignore[arg-type]
            input_fingerprint="not-used-for-invalid-parameters",
        )


def test_binding_materialization_has_canonical_input_fingerprint() -> None:
    """物化参数必须是普通 JSON，并用键序无关的规范编码计算稳定输入指纹。"""
    resolver = InputBindingResolver()
    first = resolver.materialize(
        {
            "product": InputBinding(kind="PLAN_INPUT", path=("products_by_id", "p001")),
            "locale": InputBinding(kind="LITERAL", literal_value="zh-CN"),
        },
        planning_input=_planning_input(),
        dependency_outputs={},
        declared_dependencies=frozenset(),
    )
    second = resolver.materialize(
        {
            "locale": InputBinding(kind="LITERAL", literal_value="zh-CN"),
            "product": InputBinding(kind="PLAN_INPUT", path=("products_by_id", "p001")),
        },
        planning_input=_planning_input(),
        dependency_outputs={},
        declared_dependencies=frozenset(),
    )

    assert isinstance(first.parameters, dict)
    assert first.parameters["product"]["product_id"] == "p001"
    assert first.input_fingerprint == second.input_fingerprint
    assert len(first.input_fingerprint) == 64
    # 此处独立重建规格给出的编码，而非只验证两次实现调用相等，防止实现同时偏离。
    expected_fingerprint = sha256(
        json.dumps(
            first.parameters,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert first.input_fingerprint == expected_fingerprint


def test_binding_materialization_returns_deeply_immutable_parameters() -> None:
    """物化参数保持 JSON 容器语义，但任何层级都不能在指纹生成后被原地改写。"""
    materialized = InputBindingResolver().materialize(
        {
            "product": InputBinding(kind="PLAN_INPUT", path=("products_by_id", "p001")),
            "locale": InputBinding(kind="LITERAL", literal_value="zh-CN"),
        },
        planning_input=_planning_input(),
        dependency_outputs={},
        declared_dependencies=frozenset(),
    )
    original_fingerprint = materialized.input_fingerprint
    original_json = json.dumps(materialized.parameters, sort_keys=True)

    # frozen dataclass 只能阻止 parameters 字段被整体替换；以下断言覆盖快照内部所有
    # JSON 容器边界，确保调用方不能让参数内容与已经持久化的输入指纹发生脱节。
    with pytest.raises(TypeError):
        materialized.parameters["locale"] = "en-US"
    with pytest.raises(TypeError):
        materialized.parameters["product"]["name"] = "被篡改商品"
    with pytest.raises(TypeError):
        materialized.parameters["product"]["tags"].append("被篡改标签")

    assert isinstance(materialized.parameters, dict)
    assert isinstance(materialized.parameters["product"], dict)
    assert isinstance(materialized.parameters["product"]["tags"], list)
    assert json.dumps(materialized.parameters, sort_keys=True) == original_json
    assert materialized.input_fingerprint == original_fingerprint


@pytest.mark.parametrize(
    "literal_value",
    [
        {1: "整数键", "1": "字符串键"},
        {"nested": [{1: "整数键", "1": "字符串键"}]},
    ],
    ids=["top-level", "nested"],
)
def test_binding_resolver_rejects_non_string_json_object_keys(
    literal_value: object,
) -> None:
    """任何层级的 JSON object key 都必须是字符串，禁止编码后合并为同一键。"""
    unsafe_binding = InputBinding.model_construct(
        kind="LITERAL",
        path=(),
        upstream_logical_key=None,
        literal_value=literal_value,
    )

    # 正常 Pydantic 构造会先拒绝这些值；运行时仍需防御 model_construct 或其他
    # 非类型化调用方，尤其不能让 1 与 "1" 经 json.dumps 后静默变成重复键。
    with pytest.raises(PlanValidationError, match="LITERAL 值不是普通 JSON"):
        InputBindingResolver().resolve(
            unsafe_binding,
            planning_input=_planning_input(),
            dependency_outputs={},
            declared_dependencies=frozenset(),
        )


@pytest.mark.parametrize(
    "literal_value",
    [
        {"items": ("p001", "p002")},
        {"payload": b"not-json"},
        {"number": float("nan")},
        {"number": float("inf")},
        {"number": float("-inf")},
    ],
    ids=["tuple", "bytes", "nan", "positive-infinity", "negative-infinity"],
)
def test_binding_resolver_rejects_values_outside_strict_json_domain(
    literal_value: object,
) -> None:
    """JSON 边界拒绝隐式容器转换、二进制值和所有非有限浮点数。"""
    unsafe_binding = InputBinding.model_construct(
        kind="LITERAL",
        path=(),
        upstream_logical_key=None,
        literal_value=literal_value,
    )

    with pytest.raises(PlanValidationError, match="LITERAL 值不是普通 JSON"):
        InputBindingResolver().resolve(
            unsafe_binding,
            planning_input=_planning_input(),
            dependency_outputs={},
            declared_dependencies=frozenset(),
        )


def test_binding_resolver_rejects_non_tuple_path_even_when_model_validation_is_bypassed() -> None:
    """运行期仍须防御 model_construct，路径契约不能只依赖候选构造时的校验。"""
    unsafe_binding = InputBinding.model_construct(
        kind="PLAN_INPUT",
        path=["products_by_id", "p001"],
        upstream_logical_key=None,
        literal_value=None,
    )

    with pytest.raises(PlanValidationError, match="tuple"):
        InputBindingResolver().resolve(
            unsafe_binding,
            planning_input=_planning_input(),
            dependency_outputs={},
            declared_dependencies=frozenset(),
        )


def test_card_capability_derives_resource_key_and_catalog_version() -> None:
    """手卡节点的所有执行事实必须来自 Catalog，而非候选 DAG 的可控字段。"""
    resolved = PlanCapabilityProfile.default(
        catalog=get_default_skill_catalog()
    ).resolve_skill_node(
        skill_id="generate_product_card",
        product_id="p001",
        room_id="room-1",
    )

    assert resolved.skill_version == "1.0.0"
    assert resolved.resource_keys == ("room:room-1:product:p001",)
    assert resolved.max_concurrency == 4


@pytest.mark.parametrize(
    (
        "first_room_id",
        "first_product_id",
        "second_room_id",
        "second_product_id",
    ),
    [
        ("a:product:b", "c", "a", "b:product:c"),
        ("a:b", "c", "a%3Ab", "c"),
    ],
    ids=["delimiter-injection", "percent-encoding-alias"],
)
def test_card_resource_key_encodes_dynamic_segments_without_collisions(
    first_room_id: str,
    first_product_id: str,
    second_room_id: str,
    second_product_id: str,
) -> None:
    """不可信动态段即使包含分隔符或转义前缀，也必须生成不同资源键。"""
    profile = PlanCapabilityProfile.default(catalog=get_default_skill_catalog())

    first = profile.resolve_skill_node(
        skill_id="generate_product_card",
        room_id=first_room_id,
        product_id=first_product_id,
    )
    second = profile.resolve_skill_node(
        skill_id="generate_product_card",
        room_id=second_room_id,
        product_id=second_product_id,
    )

    # 第一组证明冒号不能改变静态段边界；第二组证明百分号也必须先被编码，
    # 否则原始 "%3A" 会与冒号的 percent-encoding 结果重新形成别名。
    assert first.resource_keys != second.resource_keys


def test_card_capability_derives_all_execution_facts_from_manifest() -> None:
    """Catalog Manifest 是版本、生命周期、风险和单次超时的唯一可信来源。"""
    catalog = get_default_skill_catalog()
    manifest = next(item for item in catalog if item.skill_id == "generate_product_card")
    resolved = PlanCapabilityProfile.default(catalog=catalog).resolve_skill_node(
        skill_id="generate_product_card",
        product_id="p001",
        room_id="room-1",
    )

    assert resolved.skill_version == manifest.version
    assert resolved.lifecycle == manifest.lifecycle
    assert resolved.risk_level == manifest.risk_level
    assert resolved.max_attempt_seconds == manifest.max_attempt_seconds


@pytest.mark.parametrize(
    "control_type",
    [
        PlanCapabilityProfile.PREPARE_CARD_BATCH,
        PlanCapabilityProfile.COLLECT_CARD_RESULTS,
    ],
)
def test_capability_profile_limits_control_nodes_to_internal_lock_free_nodes(
    control_type: str,
) -> None:
    """两个受控编排节点都没有外部资源锁，且白名单外节点必须被拒绝。"""
    profile = PlanCapabilityProfile.default(catalog=get_default_skill_catalog())
    resolved = profile.resolve_control_node(control_type=control_type)

    assert resolved.node_type == control_type
    assert resolved.resource_keys == ()
    assert resolved.skill_id is None

    with pytest.raises(PlanCapabilityError, match="不允许控制节点"):
        profile.resolve_control_node(control_type="UNTRUSTED_CONTROL")
