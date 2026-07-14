"""Phase 12A PlanEngine 的冻结领域模型。

本模块只表达可持久化、可审计的计划事实。候选节点无法在此层声明 Catalog 版本、
风险或资源锁，后续 Capability Profile 会从可信配置补全这些字段，防止候选来源
越权影响执行边界。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.skills.live_plan_generator import LivePlanDraft
from src.skills.product_catalog import CatalogProduct


JsonScalar: TypeAlias = str | int | float | bool | None
JsonSafeValue: TypeAlias = JsonScalar | list["JsonSafeValue"] | dict[str, "JsonSafeValue"]


class FrozenDict(dict):
    """保持 JSON object 语义的只读映射。

    Pydantic 的 ``frozen=True`` 只禁止字段重新赋值，不能禁止普通 dict/list 的原地
    修改。本类保留 ``dict`` 身份以兼容 JSON 序列化，同时关闭所有写入入口，使计划
    快照在创建后不能被 Graph、Provider 或调用方悄悄篡改。
    """

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        """统一拒绝映射原地写入，返回明确的不可变错误。"""
        raise TypeError("冻结映射不允许修改")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class FrozenList(list):
    """保持 JSON array 语义的只读序列，防止嵌套快照被原地修改。"""

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        """统一拒绝序列原地写入，避免审计快照和运行输入产生分叉。"""
        raise TypeError("冻结序列不允许修改")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _freeze_json(value: Any) -> JsonSafeValue:
    """递归验证并冻结 JSON 值。

    PlanStore 的 JSONB、输入指纹和跨进程 Worker 都依赖严格 JSON 事实。因此这里
    拒绝 bytes、Decimal、Pydantic 模型、NaN/Infinity 和非字符串 object key，而不
    尝试隐式转换可能改变业务含义的值。
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("JSON-safe 浮点数必须是有限值")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON 对象 key 必须是字符串")
        return FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return FrozenList(_freeze_json(item) for item in value)
    raise ValueError(f"值不是 JSON-safe 类型: {type(value).__name__}")


def _frozen_empty_json_object() -> JsonSafeValue:
    """返回经 JSON 递归冻结的空对象，供所有可选快照字段作为安全默认值。

    Pydantic 默认不会执行默认值的字段校验；因此不能直接使用 ``dict`` 作为
    default_factory，必须在工厂本身返回 FrozenDict，避免省略字段的调用方获得可写容器。
    """
    return _freeze_json({})


def _freeze_live_plan(value: LivePlanDraft) -> LivePlanDraft:
    """复制并冻结既有 LivePlanDraft 的可变 items 容器。

    排品模型由既有模块所有，PlanEngine 不修改其定义；这里在可信输入边界复制后再
    冻结，确保外部仍持有原始草案时也无法影响当前 PlanRun 的快照。
    """
    frozen = LivePlanDraft.model_validate(value.model_dump(mode="json"))
    object.__setattr__(frozen, "items", FrozenList(frozen.items))
    return frozen


def _freeze_product(value: CatalogProduct) -> CatalogProduct:
    """复制并冻结既有商品模型内部的 tags/selling_points 容器。"""
    frozen = CatalogProduct.model_validate(value.model_dump(mode="json"))
    object.__setattr__(frozen, "tags", FrozenList(frozen.tags))
    object.__setattr__(frozen, "selling_points", FrozenList(frozen.selling_points))
    return frozen


class PlanNodeKind(StrEnum):
    """候选节点种类：CONTROL 只做引擎内编排，SKILL 才可派发能力调用。"""

    CONTROL = "CONTROL"
    SKILL = "SKILL"


class InputBindingKind(StrEnum):
    """受限输入来源，禁止表达式、环境变量或隐式跨版本读取。"""

    PLAN_INPUT = "PLAN_INPUT"
    NODE_OUTPUT = "NODE_OUTPUT"
    LITERAL = "LITERAL"


class PlanRunState(StrEnum):
    """PlanRun 的聚合状态，首期不允许部分成功终态。"""

    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PlanRunKind(StrEnum):
    """Phase 12B 引入的计划用途，用于区分普通批次与售罄紧急 child。"""

    CARD_BATCH = "CARD_BATCH"
    EMERGENCY_SOLD_OUT = "EMERGENCY_SOLD_OUT"


class PlanNodeState(StrEnum):
    """D-015 规定的节点状态集合，具体转换由后续状态机集中约束。"""

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_RECONCILIATION = "WAITING_RECONCILIATION"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    FROZEN = "FROZEN"
    INVALIDATED = "INVALIDATED"
    SKIPPED = "SKIPPED"


class PlanCommandType(StrEnum):
    """命令账本允许的人工控制意图，未知命令不得进入状态机。"""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    RECONCILE = "RECONCILE"
    RESUME = "RESUME"


class InputBinding(BaseModel):
    """候选节点参数到受控数据源的静态绑定。

    ``NODE_OUTPUT`` 必须显式写出上游 logical key，随后由候选 DAG 校验器确认该
    key 位于当前节点已声明的依赖中。该模型不解析路径，解析发生在 Worker 派发前，
    以保持候选阶段与运行阶段的责任分离。
    """

    model_config = ConfigDict(frozen=True)

    kind: InputBindingKind
    path: tuple[str | int, ...] = Field(default_factory=tuple)
    upstream_logical_key: str | None = Field(default=None, min_length=1)
    literal_value: Any = Field(default=None)

    @field_validator("path")
    @classmethod
    def _freeze_path(cls, value: tuple[str | int, ...]) -> tuple[str | int, ...]:
        """路径只允许稳定的对象键和数组下标，拒绝 JSONPath 等动态表达式。"""
        if any(isinstance(part, bool) or not isinstance(part, (str, int)) for part in value):
            raise ValueError("绑定路径只能包含字符串键或整数下标")
        if any(isinstance(part, str) and not part for part in value):
            raise ValueError("绑定路径不能包含空字符串")
        if any(isinstance(part, int) and part < 0 for part in value):
            raise ValueError("绑定路径下标不能为负数")
        return tuple(value)

    @field_validator("literal_value", mode="after")
    @classmethod
    def _freeze_literal(cls, value: Any) -> JsonSafeValue:
        """Literal 只能携带 JSON-safe 常量，避免候选捎带不可持久化对象。"""
        return _freeze_json(value)

    @model_validator(mode="after")
    def _validate_source_shape(self) -> "InputBinding":
        """让每种绑定类型只携带其语义允许的来源字段。"""
        if self.kind == InputBindingKind.NODE_OUTPUT and not self.upstream_logical_key:
            raise ValueError("NODE_OUTPUT 必须提供 upstream_logical_key")
        if self.kind != InputBindingKind.NODE_OUTPUT and self.upstream_logical_key is not None:
            raise ValueError("只有 NODE_OUTPUT 可以提供 upstream_logical_key")
        if self.kind == InputBindingKind.LITERAL and self.path:
            raise ValueError("LITERAL 不能提供 path")
        if self.kind != InputBindingKind.LITERAL and self.literal_value is not None:
            raise ValueError("只有 LITERAL 可以提供 literal_value")
        return self


class CardBatchPlanningInput(BaseModel):
    """创建手卡批次 PlanRun 所需的完整冻结规划输入。

    ``run_key`` 覆盖房间、追踪、完整排品和全部商品快照，而非只覆盖前三项。这样
    后续审计可精确说明候选为何从某一完整输入产生，也避免排品尾部变化误复用旧计划。
    """

    model_config = ConfigDict(frozen=True)

    room_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    live_plan: LivePlanDraft
    products_by_id: dict[str, CatalogProduct]
    run_key: str = Field(default="", min_length=64, max_length=64)

    @model_validator(mode="after")
    def _freeze_and_validate_snapshot(self) -> "CardBatchPlanningInput":
        """验证输入闭包并一次性建立稳定、不可变的 SHA-256 身份。"""
        if not self.live_plan.items:
            raise ValueError("排品不能为空")
        product_ids = [item.product_id for item in self.live_plan.items]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("排品中存在重复 product_id")
        missing = [product_id for product_id in product_ids if product_id not in self.products_by_id]
        if missing:
            raise ValueError(f"排品缺少商品快照: {', '.join(missing)}")
        mismatched_product_ids = [
            f"{product_key} -> {product.product_id}"
            for product_key, product in self.products_by_id.items()
            if product_key != product.product_id
        ]
        if mismatched_product_ids:
            raise ValueError(
                "商品快照键与 product_id 不一致: "
                f"{', '.join(mismatched_product_ids)}"
            )

        frozen_plan = _freeze_live_plan(self.live_plan)
        frozen_products = FrozenDict(
            {
                product_id: _freeze_product(product)
                for product_id, product in self.products_by_id.items()
            }
        )
        object.__setattr__(self, "live_plan", frozen_plan)
        object.__setattr__(self, "products_by_id", frozen_products)

        canonical_snapshot = {
            "room_id": self.room_id,
            "trace_id": self.trace_id,
            "live_plan": frozen_plan.model_dump(mode="json"),
            "products_by_id": {
                product_id: product.model_dump(mode="json")
                for product_id, product in frozen_products.items()
            },
        }
        calculated_run_key = _canonical_sha256(canonical_snapshot)
        if self.run_key and self.run_key != calculated_run_key:
            raise ValueError("run_key 必须与冻结规划输入一致")
        object.__setattr__(self, "run_key", calculated_run_key)
        return self


class CandidatePlanNode(BaseModel):
    """尚未物化到 Store 的受限 DAG 节点声明。

    节点不得声明版本、风险、重试或资源键，这些执行事实仅能由可信 Capability
    Profile 导出。这里仅允许表达节点类型、合法 Skill 标识、依赖和参数绑定。
    """

    model_config = ConfigDict(frozen=True)

    logical_key: str = Field(..., min_length=1)
    node_kind: PlanNodeKind
    skill_id: str | None = Field(default=None, min_length=1)
    depends_on: tuple[str, ...] = Field(default_factory=tuple)
    input_bindings: dict[str, InputBinding] = Field(default_factory=_frozen_empty_json_object)

    @field_validator("depends_on")
    @classmethod
    def _freeze_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """依赖键必须是非空的稳定标识，重复依赖没有额外语义而直接拒绝。"""
        if any(not logical_key for logical_key in value):
            raise ValueError("依赖 logical_key 不能为空")
        if len(value) != len(set(value)):
            raise ValueError("节点依赖不能重复")
        return tuple(value)

    @field_validator("input_bindings", mode="after")
    @classmethod
    def _freeze_bindings(cls, value: dict[str, InputBinding]) -> dict[str, InputBinding]:
        """参数名映射也是候选证据的一部分，创建后不可被调用方改写。"""
        if any(not parameter_name for parameter_name in value):
            raise ValueError("输入参数名不能为空")
        return FrozenDict(value)

    @model_validator(mode="after")
    def _validate_node_kind(self) -> "CandidatePlanNode":
        """控制节点与 Skill 节点的能力边界必须在节点构造期闭合。"""
        if self.node_kind == PlanNodeKind.CONTROL and self.skill_id is not None:
            raise ValueError("CONTROL 节点不能携带 skill_id")
        if self.node_kind == PlanNodeKind.SKILL and not self.skill_id:
            raise ValueError("SKILL 节点必须提供 skill_id")
        return self


class CandidatePlanProposal(BaseModel):
    """Provider 产生的完整候选 DAG。

    构造时验证拓扑和输入闭包。任何错误都直接向上抛出；本层刻意不触发 Legacy 或
    其他 Provider fallback，确保非法候选不能创建 PlanRun。
    """

    model_config = ConfigDict(frozen=True)

    provider_id: str = Field(..., min_length=1)
    provider_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    nodes: tuple[CandidatePlanNode, ...]

    @model_validator(mode="after")
    def _validate_dag(self) -> "CandidatePlanProposal":
        """一次性校验节点唯一性、依赖存在性、无环性和 NODE_OUTPUT 闭包。"""
        if not self.nodes:
            raise ValueError("候选 DAG 不能为空")
        # Pydantic 接收既有 BaseModel 实例时不会递归重放其全部字段验证，因而
        # model_construct 产生的节点可能携带可变容器或原始 binding dict。这里先将每个
        # 节点导出为普通数据并完整重建，复用 CandidatePlanNode/InputBinding 的标准
        # 验证、冻结和 JSON-safe 规则；之后以规范化节点替换 Proposal 的审计事实。
        normalized_nodes = tuple(
            CandidatePlanNode.model_validate(node.model_dump(warnings=False))
            for node in self.nodes
        )
        object.__setattr__(self, "nodes", normalized_nodes)
        logical_keys = [node.logical_key for node in self.nodes]
        if len(logical_keys) != len(set(logical_keys)):
            raise ValueError("候选 DAG 存在重复 logical_key")
        known_keys = set(logical_keys)
        for node in self.nodes:
            unknown_dependencies = set(node.depends_on) - known_keys
            if unknown_dependencies:
                raise ValueError(f"节点 {node.logical_key} 存在未知依赖")
            for binding in node.input_bindings.values():
                if (
                    binding.kind == InputBindingKind.NODE_OUTPUT
                    and binding.upstream_logical_key not in node.depends_on
                ):
                    raise ValueError(
                        f"节点 {node.logical_key} 的 NODE_OUTPUT 引用未声明上游依赖"
                    )
        _raise_if_cycle(self.nodes)
        return self


def _raise_if_cycle(nodes: tuple[CandidatePlanNode, ...]) -> None:
    """以深度优先遍历检查依赖图中的回边，拒绝无法调度的候选。"""
    dependencies = {node.logical_key: node.depends_on for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(logical_key: str) -> None:
        if logical_key in visiting:
            raise ValueError("候选 DAG 存在环")
        if logical_key in visited:
            return
        visiting.add(logical_key)
        for dependency in dependencies[logical_key]:
            visit(dependency)
        visiting.remove(logical_key)
        visited.add(logical_key)

    for node in nodes:
        visit(node.logical_key)


def _canonical_sha256(value: JsonSafeValue | dict[str, Any]) -> str:
    """以规范 JSON 编码生成跨进程稳定的 SHA-256 摘要。"""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(serialized.encode("utf-8")).hexdigest()


class _JsonSafeView(BaseModel):
    """Store 查询视图的公共冻结基类，统一冻结所有 JSONB 投影字段。"""

    model_config = ConfigDict(frozen=True)


class PlanRunView(_JsonSafeView):
    """PlanRun 的只读 JSON-safe 视图，不暴露 Store 内部连接或锁状态。"""

    plan_run_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    run_key: str = Field(..., min_length=1)
    current_version: int = Field(..., ge=1)
    state: PlanRunState
    planning_input: Any = Field(default_factory=_frozen_empty_json_object)
    plan_kind: PlanRunKind = PlanRunKind.CARD_BATCH
    priority: int = Field(default=0, ge=0, strict=True)
    root_plan_run_id: str | None = Field(default=None, min_length=1)
    parent_plan_run_id: str | None = Field(default=None, min_length=1)
    trigger_event_id: str | None = Field(default=None, min_length=1)
    reconciliation_required: bool = False
    reconciliation_failure: Any | None = None
    reconciliation_signature: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    reconciliation_attempt_count: int = Field(default=0, ge=0, strict=True)
    last_reconciled_at: datetime | None = None

    @field_validator("planning_input", mode="after")
    @classmethod
    def _freeze_planning_input(cls, value: Any) -> JsonSafeValue:
        """视图返回的冻结输入必须仍是严格 JSON，供 API/Graph 安全重放。"""
        return _freeze_json(value)

    @field_validator("reconciliation_failure", mode="after")
    @classmethod
    def _freeze_reconciliation_failure(cls, value: Any | None) -> JsonSafeValue:
        """事故事实必须是不可变严格 JSON，防止查询方修改权威失败证据。"""
        return None if value is None else _freeze_json(value)

    @model_validator(mode="after")
    def _lineage_matches_plan_kind(self) -> "PlanRunView":
        """普通计划不得伪装 child，紧急计划必须闭合 root/parent/event 事实。"""
        lineage = (
            self.root_plan_run_id,
            self.parent_plan_run_id,
            self.trigger_event_id,
        )
        if self.plan_kind is PlanRunKind.CARD_BATCH:
            if self.priority != 0 or any(value is not None for value in lineage):
                raise ValueError("CARD_BATCH 必须使用 priority 0 且不携带 child lineage")
        elif self.priority != 100 or any(value is None for value in lineage):
            raise ValueError(
                "EMERGENCY_SOLD_OUT 必须使用 priority 100 并闭合 root/parent/event"
            )
        return self

    @field_validator("last_reconciled_at")
    @classmethod
    def _last_reconciled_at_must_be_aware(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        """最近对账时间统一规范为 UTC，避免跨 Worker 时区造成扫描顺序漂移。"""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_reconciled_at 必须包含时区")
        return value.astimezone(timezone.utc)


class PlanVersionView(_JsonSafeView):
    """不可变 PlanVersion 的查询投影，保留 Provider 版本作为审计证据。"""

    plan_run_id: str = Field(..., min_length=1)
    version_number: int = Field(..., ge=1)
    provider_id: str = Field(..., min_length=1)
    provider_version: str = Field(..., min_length=1)
    proposal: Any = Field(default_factory=_frozen_empty_json_object)
    change_reason: str = Field(default="INITIAL", min_length=1)
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("proposal", mode="after")
    @classmethod
    def _freeze_proposal(cls, value: Any) -> JsonSafeValue:
        """Provider 候选投影必须是 JSON-safe，避免查询层泄漏运行对象。"""
        return _freeze_json(value)

    @field_validator("source_event_ids")
    @classmethod
    def _source_events_are_unique(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        """来源事件保持有序不可变，拒绝空 ID 和重复 lineage。"""
        normalized = tuple(value)
        if any(not event_id for event_id in normalized):
            raise ValueError("source_event_ids 不能包含空 ID")
        if len(set(normalized)) != len(normalized):
            raise ValueError("source_event_ids 不能重复")
        return normalized


class PlanNodeView(_JsonSafeView):
    """一个物化节点的只读投影，依赖和绑定仍保持可审计的 JSON 事实。"""

    node_id: str = Field(..., min_length=1)
    plan_run_id: str = Field(..., min_length=1)
    version_number: int = Field(..., ge=1)
    logical_key: str = Field(..., min_length=1)
    node_kind: PlanNodeKind
    state: PlanNodeState
    skill_id: str | None = None
    input_bindings: Any = Field(default_factory=_frozen_empty_json_object)

    @field_validator("input_bindings", mode="after")
    @classmethod
    def _freeze_input_bindings(cls, value: Any) -> JsonSafeValue:
        """防止读取节点视图的调用方原地篡改后续审计显示。"""
        return _freeze_json(value)


class NodeRunView(_JsonSafeView):
    """一次 Worker claim 的只读执行事实，不覆盖历史 attempt。"""

    node_run_id: str = Field(..., min_length=1)
    plan_run_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    attempt_number: int = Field(..., ge=1)
    state: PlanNodeState
    input_snapshot: Any = Field(default_factory=_frozen_empty_json_object)
    output: Any | None = None

    @field_validator("input_snapshot", "output", mode="after")
    @classmethod
    def _freeze_node_run_json(cls, value: Any) -> JsonSafeValue:
        """输入与输出都必须是冻结 JSON，确保 NodeRun 可跨进程复盘。"""
        return _freeze_json(value)
