"""Phase 12B root 级不可变增量 Replan 协调器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from src.plan_engine.bindings import InputBindingResolver
from src.plan_engine.capabilities import PlanCapabilityProfile, ResolvedPlanCapability
from src.plan_engine.event_state_machine import EventApplicationState
from src.plan_engine.event_store import EventStore
from src.plan_engine.models import CardBatchPlanningInput, PlanNodeState
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.store import MaterializedPlan, PlanStore, PlanStoreInvariantError
from src.skill_runtime.catalog import get_default_skill_catalog


class ReplanStatus(StrEnum):
    """一次协调结果是新建版本还是幂等复用已提交版本。"""

    CREATED = "CREATED"
    REPLAYED = "REPLAYED"


@dataclass(frozen=True)
class ReplanResult:
    """不泄漏 Store 内部记录的最小 Replan 结果。"""

    status: ReplanStatus
    plan_run_id: str
    plan_version: int
    source_event_ids: tuple[str, ...]


class ReplanCoordinator:
    """合并 REPLAN_READY Application，并委托 PlanStore 原子创建新版本。"""

    def __init__(self, *, plan_store: PlanStore, event_store: EventStore) -> None:
        self._plan_store = plan_store
        self._event_store = event_store
        self._provider = CanonicalCardBatchProposalProvider()
        self._profile = PlanCapabilityProfile.default(
            catalog=get_default_skill_catalog()
        )
        self._resolver = InputBindingResolver()

    def replan(
        self,
        *,
        root_plan_run_id: str,
        planning_input: CardBatchPlanningInput,
        failure_signature: str,
        now: datetime,
    ) -> ReplanResult:
        """创建下一不可变版本；PlanStore 提交后才把来源 Application 标为 APPLIED。"""
        if not root_plan_run_id:
            raise ValueError("root_plan_run_id 不能为空")
        if len(failure_signature) != 64 or any(
            character not in "0123456789abcdef" for character in failure_signature
        ):
            raise ValueError("failure_signature 必须是小写 SHA-256")
        frozen_input = CardBatchPlanningInput.model_validate(
            planning_input.model_dump(mode="json")
        )
        plan_run = self._plan_store.get_plan_run(root_plan_run_id)
        applications = tuple(
            application
            for application in self._event_store.list_applications(
                root_plan_run_id=root_plan_run_id
            )
            if application.state is EventApplicationState.REPLAN_READY
        )
        latest = self._plan_store.get_plan_version(
            root_plan_run_id,
            plan_run.current_version,
        )
        latest_source_events = set(latest.source_event_ids)
        applications = tuple(
            application
            for application in applications
            if application.source_plan_version == plan_run.current_version
            or application.event_id in latest_source_events
        )
        input_fingerprint = frozen_input.run_key
        if not applications and not (
            latest.failure_signature == failure_signature
            and latest.input_fingerprint == input_fingerprint
        ):
            raise PlanStoreInvariantError("没有可合并的 REPLAN_READY 事件")

        source_event_ids = tuple(application.event_id for application in applications)
        affected = {
            str(logical_key)
            for application in applications
            for logical_key in (application.impact_analysis or {}).get(
                "affected_logical_keys",
                (),
            )
        }
        proposal = self._provider.propose_sync(frozen_input)
        capabilities = self._resolve_capabilities(frozen_input, proposal.nodes)
        materialized = MaterializedPlan(
            planning_input=frozen_input,
            proposal=proposal,
            capabilities_by_logical_key=capabilities,
        )
        reused = self._select_reusable_nodes(
            root_plan_run_id=root_plan_run_id,
            current_version=plan_run.current_version,
            planning_input=frozen_input,
            proposal_nodes=proposal.nodes,
            affected_logical_keys=frozenset(affected),
        )
        version, created = self._plan_store.create_replan_version(
            plan_run_id=root_plan_run_id,
            expected_plan_version=plan_run.current_version,
            plan=materialized,
            source_event_ids=source_event_ids,
            failure_signature=failure_signature,
            input_fingerprint=input_fingerprint,
            reused_from_by_logical_key=reused,
            now=now,
        )
        for application in applications:
            current = self._event_store.get_application(
                application.event_id,
                root_plan_run_id,
            )
            if current.state is EventApplicationState.APPLIED:
                if current.applied_plan_version != version.version_number:
                    raise PlanStoreInvariantError("Application 已关联其他 PlanVersion")
                continue
            if current.state is not EventApplicationState.REPLAN_READY:
                raise PlanStoreInvariantError("Application 不再允许 Replan 补偿")
            self._event_store.transition_application(
                application.event_id,
                root_plan_run_id,
                expected_state=EventApplicationState.REPLAN_READY,
                target_state=EventApplicationState.APPLIED,
                now=now,
                applied_plan_version=version.version_number,
            )
        return ReplanResult(
            status=ReplanStatus.CREATED if created else ReplanStatus.REPLAYED,
            plan_run_id=root_plan_run_id,
            plan_version=version.version_number,
            source_event_ids=version.source_event_ids,
        )

    def _select_reusable_nodes(
        self,
        *,
        root_plan_run_id: str,
        current_version: int,
        planning_input: CardBatchPlanningInput,
        proposal_nodes: tuple[object, ...],
        affected_logical_keys: frozenset[str],
    ) -> dict[str, str]:
        """只选择指纹相同、成功、未 superseded 且不在影响闭包内的 Skill。"""
        old_nodes = {
            node.logical_key: node
            for node in self._plan_store.list_nodes(
                root_plan_run_id,
                current_version,
            )
        }
        reused: dict[str, str] = {}
        for candidate in proposal_nodes:
            if (
                candidate.skill_id != "generate_product_card"
                or candidate.logical_key in affected_logical_keys
            ):
                continue
            old_node = old_nodes.get(candidate.logical_key)
            if old_node is None or old_node.state is not PlanNodeState.SUCCEEDED:
                continue
            successful_runs = self._successful_source_runs(
                root_plan_run_id,
                old_node,
                current_version,
            )
            if not successful_runs or successful_runs[-1].input_fingerprint is None:
                continue
            materialized = self._resolver.materialize(
                input_bindings=candidate.input_bindings,
                planning_input=planning_input,
                dependency_outputs={},
                declared_dependencies=frozenset(candidate.depends_on),
                current_plan_version=current_version + 1,
            )
            if materialized.input_fingerprint == successful_runs[-1].input_fingerprint:
                reused[candidate.logical_key] = old_node.node_id
        return reused

    def _successful_source_runs(
        self,
        root_plan_run_id: str,
        node: object,
        current_version: int,
    ) -> tuple[object, ...]:
        """沿复用链寻找最终成功 NodeRun，并拒绝身份循环。"""
        current = node
        visited: set[str] = set()
        while current is not None:
            if current.node_id in visited:
                raise PlanStoreInvariantError("Replan 复用来源形成循环")
            visited.add(current.node_id)
            runs = tuple(
                run
                for run in self._plan_store.list_node_runs(
                    root_plan_run_id,
                    current.node_id,
                )
                if run.state is PlanNodeState.SUCCEEDED and not run.superseded
            )
            if runs:
                return runs
            source_id = current.reused_from_node_id
            if source_id is None:
                return ()
            current = next(
                (
                    candidate
                    for version in range(current_version, 0, -1)
                    for candidate in self._plan_store.list_nodes(
                        root_plan_run_id,
                        version,
                    )
                    if candidate.node_id == source_id
                ),
                None,
            )
        return ()

    def _resolve_capabilities(
        self,
        planning_input: CardBatchPlanningInput,
        nodes: tuple[object, ...],
    ) -> dict[str, ResolvedPlanCapability]:
        """复用固定 Capability Profile，禁止 Replan 自行声明版本或资源键。"""
        capabilities: dict[str, ResolvedPlanCapability] = {}
        for node in nodes:
            if node.logical_key == "prepare-card-batch":
                capability = self._profile.resolve_control_node(
                    control_type=self._profile.PREPARE_CARD_BATCH
                )
            elif node.logical_key == "collect-card-results":
                capability = self._profile.resolve_control_node(
                    control_type=self._profile.COLLECT_CARD_RESULTS
                )
            else:
                capability = self._profile.resolve_skill_node(
                    skill_id=node.skill_id,
                    product_id=node.logical_key.removeprefix("card:"),
                    room_id=planning_input.room_id,
                )
            capabilities[node.logical_key] = capability
        return capabilities
