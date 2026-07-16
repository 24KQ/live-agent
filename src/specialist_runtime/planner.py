"""Phase 13 PlannerAgent 的受限提案、确定性 baseline 与编译证据。"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from enum import StrEnum
import json
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from pydantic import ConfigDict, Field, model_validator

from src.plan_engine.models import (
    CandidatePlanNode,
    CandidatePlanProposal,
    InputBinding,
    InputBindingKind,
    PlanNodeKind,
)
from src.skill_runtime.catalog import get_default_skill_catalog
from src.state.models import RiskLevel
from src.specialist_runtime.models import (
    AgentResult,
    AgentResultStatus,
    AgentTask,
    StrictFrozenModel,
    SpecialistTaskKind,
)
from src.specialist_runtime.profiles import SpecialistProfile


class PlannerCapability(StrEnum):
    """Planner 唯一允许声明的三个播前非高风险能力。"""

    GENERATE_LIVE_PLAN = "generate_live_plan"
    GENERATE_PRODUCT_CARD = "generate_product_card"
    SUGGEST_PRICE_CHANGE = "suggest_price_change"


class PlannerBindingSource(StrEnum):
    """与 Phase 12A 一致的三类静态输入来源。"""

    PLAN_INPUT = "PLAN_INPUT"
    NODE_OUTPUT = "NODE_OUTPUT"
    LITERAL = "LITERAL"


class PlannerNode(StrictFrozenModel):
    """模型可声明的最小节点，不包含任何执行控制字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_key: str = Field(..., min_length=1)
    capability: PlannerCapability


class PlannerDependency(StrictFrozenModel):
    """显式有向依赖边；JSON 字段保持冻结结果 Schema 的 from/to。"""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    from_key: str = Field(..., alias="from", min_length=1)
    to_key: str = Field(..., alias="to", min_length=1)


class PlannerBinding(StrictFrozenModel):
    """目标参数到静态来源的绑定，不接受对象、表达式或执行控制字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str = Field(..., min_length=3)
    source_type: PlannerBindingSource
    source: str | int | float | bool | None


class CandidatePlannerProposal(StrictFrozenModel):
    """Planner 模型的完整受限输出，构造期闭合节点、边、绑定和无环性。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[PlannerNode, ...] = Field(..., min_length=1)
    dependencies: tuple[PlannerDependency, ...] = ()
    bindings: tuple[PlannerBinding, ...] = ()

    @model_validator(mode="after")
    def _validate_graph(self) -> "CandidatePlannerProposal":
        keys = [node.logical_key for node in self.nodes]
        if len(keys) != len(set(keys)):
            raise ValueError("planner node logical_key must be unique")
        known = set(keys)
        graph: dict[str, set[str]] = defaultdict(set)
        edge_set: set[tuple[str, str]] = set()
        for edge in self.dependencies:
            if edge.from_key not in known or edge.to_key not in known:
                raise ValueError("planner dependency references unknown node")
            if edge.from_key == edge.to_key:
                raise ValueError("planner dependency cycle is not allowed")
            identity = (edge.from_key, edge.to_key)
            if identity in edge_set:
                raise ValueError("planner dependency cannot repeat")
            edge_set.add(identity)
            graph[edge.from_key].add(edge.to_key)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("planner dependency cycle is not allowed")
            if key in visited:
                return
            visiting.add(key)
            for child in graph[key]:
                visit(child)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)
        target_set: set[str] = set()
        execution_controls = {
            "approval",
            "authorization",
            "deadline_at",
            "idempotency_key",
            "max_attempt_seconds",
            "resource_key",
            "resource_keys",
            "retry",
            "risk_level",
            "skill_version",
            "version",
        }
        for binding in self.bindings:
            if "." not in binding.target:
                raise ValueError("planner binding target must be node.parameter")
            node_key, parameter = binding.target.rsplit(".", 1)
            if node_key not in known:
                raise ValueError("planner binding targets unknown node")
            if parameter in execution_controls:
                raise ValueError("planner binding cannot set execution control")
            if binding.target in target_set:
                raise ValueError("planner binding target cannot repeat")
            target_set.add(binding.target)
            if binding.source_type is not PlannerBindingSource.LITERAL and not isinstance(
                binding.source, str
            ):
                raise ValueError("planner non-literal binding source must be a path string")
            if binding.source_type is PlannerBindingSource.NODE_OUTPUT:
                upstream = str(binding.source).split(".", 1)[0]
                if (upstream, node_key) not in edge_set:
                    raise ValueError("planner NODE_OUTPUT requires declared dependency")
        return self


class CompiledPlannerCapability(StrictFrozenModel):
    """由可信 Catalog 注入、模型输出中不存在的单节点执行策略证据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_key: str
    skill_id: str
    skill_version: str
    risk_level: RiskLevel
    max_attempt_seconds: int
    resource_keys: tuple[str, ...]
    max_concurrency: int


class CompiledPlannerProposal(StrictFrozenModel):
    """受限 DAG 与可信执行 Profile 的不可变组合，不创建 PlanRun。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: CandidatePlanProposal
    capabilities: tuple[CompiledPlannerCapability, ...]


class RankedProductPlannerPolicy:
    """固定商品顺序的确定性 baseline，刻意不读取失败商品集合。"""

    def propose(self, case_input: dict[str, Any]) -> CandidatePlannerProposal:
        products = case_input["products"]
        max_cards = min(int(case_input["constraints"]["max_cards"]), len(products))
        plan_key = "generate-plan"
        nodes = [PlannerNode(logical_key=plan_key, capability=PlannerCapability.GENERATE_LIVE_PLAN)]
        dependencies: list[PlannerDependency] = []
        bindings = [
            PlannerBinding(target=f"{plan_key}.products", source_type="PLAN_INPUT", source="products"),
            PlannerBinding(target=f"{plan_key}.constraints", source_type="PLAN_INPUT", source="constraints"),
            PlannerBinding(target=f"{plan_key}.memories", source_type="PLAN_INPUT", source="memories"),
        ]
        for index, product in enumerate(products[:max_cards]):
            key = f"card:{product['product_id']}"
            nodes.append(PlannerNode(logical_key=key, capability=PlannerCapability.GENERATE_PRODUCT_CARD))
            dependencies.append(PlannerDependency(from_key=plan_key, to_key=key))
            bindings.append(
                PlannerBinding(
                    target=f"{key}.product",
                    source_type=PlannerBindingSource.PLAN_INPUT,
                    source=f"products.{index}",
                )
            )
        return CandidatePlannerProposal(
            nodes=tuple(nodes), dependencies=tuple(dependencies), bindings=tuple(bindings)
        )


class PlannerProposalCompiler:
    """校验候选并从 Catalog 注入版本、风险、deadline、资源键和并发。"""

    def __init__(self, catalog=None) -> None:
        manifests = tuple(catalog or get_default_skill_catalog())
        self._manifests = {manifest.skill_id: manifest for manifest in manifests}

    def compile(
        self,
        proposal: CandidatePlannerProposal,
        *,
        case_input: dict[str, Any],
    ) -> CompiledPlannerProposal:
        room_id = case_input["room_id"]
        dependencies_by_target: dict[str, list[str]] = defaultdict(list)
        for edge in proposal.dependencies:
            dependencies_by_target[edge.to_key].append(edge.from_key)
        bindings_by_node: dict[str, dict[str, InputBinding]] = defaultdict(dict)
        raw_bindings_by_target = {binding.target: binding for binding in proposal.bindings}
        for binding in proposal.bindings:
            node_key, parameter = binding.target.rsplit(".", 1)
            bindings_by_node[node_key][parameter] = self._compile_binding(binding)
        candidate_nodes = tuple(
            CandidatePlanNode(
                logical_key=node.logical_key,
                node_kind=PlanNodeKind.SKILL,
                skill_id=node.capability.value,
                depends_on=tuple(dependencies_by_target[node.logical_key]),
                input_bindings=bindings_by_node[node.logical_key],
            )
            for node in proposal.nodes
        )
        candidate = CandidatePlanProposal(
            provider_id="planner-agent",
            provider_version="1.0.0",
            nodes=candidate_nodes,
        )
        capabilities = tuple(
            self._compile_capability(
                node=node,
                room_id=room_id,
                case_input=case_input,
                bindings=raw_bindings_by_target,
            )
            for node in proposal.nodes
        )
        return CompiledPlannerProposal(candidate=candidate, capabilities=capabilities)

    @staticmethod
    def _compile_binding(binding: PlannerBinding) -> InputBinding:
        if binding.source_type is PlannerBindingSource.LITERAL:
            return InputBinding(kind=InputBindingKind.LITERAL, literal_value=binding.source)
        source = str(binding.source)
        parts = tuple(int(part) if part.isdigit() else part for part in source.split("."))
        if binding.source_type is PlannerBindingSource.PLAN_INPUT:
            return InputBinding(kind=InputBindingKind.PLAN_INPUT, path=parts)
        return InputBinding(
            kind=InputBindingKind.NODE_OUTPUT,
            upstream_logical_key=str(parts[0]),
            path=parts[1:],
        )

    def _compile_capability(
        self,
        *,
        node: PlannerNode,
        room_id: str,
        case_input: dict[str, Any],
        bindings: dict[str, PlannerBinding],
    ) -> CompiledPlannerCapability:
        manifest = self._manifests.get(node.capability.value)
        if manifest is None or manifest.risk_level is RiskLevel.HIGH:
            raise ValueError("planner capability is unavailable or high risk")
        product_id = "plan"
        product_binding = bindings.get(f"{node.logical_key}.product")
        if product_binding is not None and product_binding.source_type is PlannerBindingSource.PLAN_INPUT:
            parts = str(product_binding.source).split(".")
            if len(parts) != 2 or parts[0] != "products" or not parts[1].isdigit():
                raise ValueError("planner product binding must reference frozen products index")
            product_id = case_input["products"][int(parts[1])]["product_id"]
        encoded_room = quote(room_id, safe="")
        encoded_product = quote(product_id, safe="")
        return CompiledPlannerCapability(
            logical_key=node.logical_key,
            skill_id=manifest.skill_id,
            skill_version=manifest.version,
            risk_level=manifest.risk_level,
            max_attempt_seconds=manifest.max_attempt_seconds,
            resource_keys=(f"room:{encoded_room}:product:{encoded_product}",),
            max_concurrency=4 if node.capability is PlannerCapability.GENERATE_PRODUCT_CARD else 1,
        )


class _SpecialistRunner(Protocol):
    async def run(self, task: AgentTask) -> AgentResult: ...


class PlannerAgentAdapter:
    """把冻结 Planner case 适配为单次共享 Runner 调用，不执行查询或 fallback。"""

    def __init__(self, *, runner: _SpecialistRunner, profile: SpecialistProfile) -> None:
        if profile.task_kind is not SpecialistTaskKind.PLAN_PROPOSAL or profile.max_skill_calls != 0:
            raise ValueError("Planner adapter requires zero-Skill PLAN_PROPOSAL profile")
        self._runner = runner
        self._profile = profile

    def build_task(self, case: dict[str, Any]) -> AgentTask:
        if case.get("candidate") != "planner":
            raise ValueError("Planner adapter requires planner candidate case")
        case_id = case["case_id"]
        case_input = case["input"]
        return AgentTask(
            task_id=f"planner:{case_id}",
            task_kind=SpecialistTaskKind.PLAN_PROPOSAL,
            profile_id=self._profile.profile_id,
            profile_version=self._profile.profile_version,
            room_id=case_input["room_id"],
            trace_id=f"trace-{case_id}",
            objective="Return a bounded candidate DAG from frozen planning facts.",
            input_snapshot=case_input,
            initial_evidence_refs=(),
            evaluation_case_id=case_id,
        )

    async def run_case(self, case: dict[str, Any]) -> AgentResult:
        task = self.build_task(case)
        result = await self._runner.run(task)
        if (
            result.task_id != task.task_id
            or result.profile_id != task.profile_id
            or result.profile_version != task.profile_version
        ):
            raise ValueError("Planner Runner returned mismatched task identity")
        return result

    async def proposal_for_case(self, case: dict[str, Any]) -> CandidatePlannerProposal:
        result = await self.run_case(case)
        if result.status is not AgentResultStatus.SUCCEEDED:
            raise ValueError("Planner Agent did not produce a successful proposal")
        return CandidatePlannerProposal.model_validate(result.output)


def build_planner_profile(evaluation_root: Path) -> SpecialistProfile:
    """从冻结 Task 6 Manifest 构造零 Skill Planner Profile。"""

    root = Path(evaluation_root)
    manifest = json.loads((root / "manifests" / "phase13-v2.json").read_text(encoding="utf-8"))
    facts = manifest["profiles"]["planner"]
    result_schema = json.loads((root / facts["result_schema_path"]).read_text(encoding="utf-8"))
    return SpecialistProfile(
        profile_id=facts["profile_id"],
        profile_version=facts["profile_version"],
        task_kind=SpecialistTaskKind(facts["task_kind"]),
        model_id=facts["model_id"],
        endpoint_host=facts["endpoint_host"],
        temperature=Decimal(facts["temperature"]),
        prompt_text=facts["prompt_text"],
        prompt_hash=facts["prompt_digest"],
        result_schema_hash=facts["result_schema_digest"],
        result_schema=result_schema,
        allowed_skill_ids=tuple(facts["allowed_skill_ids"]),
        skill_versions=facts["skill_versions"],
        max_model_calls=facts["max_model_calls"],
        max_skill_calls=facts["max_skill_calls"],
        max_total_tokens=facts["max_total_tokens"],
        deadline_seconds=facts["deadline_seconds"],
        max_case_cost_cny=Decimal(facts["max_case_cost_cny"]),
    )
