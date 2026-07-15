"""Phase 12A 无状态 PlanWorker 与同步桥接。

Worker 每次只 claim 一批 READY 节点，最多四个，并发执行后把所有结果先写入
PlanStore 再返回。它不持有第二份计划状态、不在内存中隐藏重试，也不把失败切换到
Legacy；进程崩溃后的恢复完全依赖 Store 中的 NodeRun、输入指纹、lease 与 fencing。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from jsonschema import Draft202012Validator, ValidationError as JsonSchemaError

from src.plan_engine.bindings import (
    InputBindingResolver,
    MaterializedNodeInput,
    VersionedNodeOutput,
)
from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.event_state_machine import EventInboxState
from src.plan_engine.event_store import EventInboxRecord, EventStore
from src.plan_engine.events import _build_event_authorization_context
from src.plan_engine.failure_policy import FailureAction, FailurePolicy
from src.plan_engine.models import (
    CardBatchPlanningInput,
    EmergencySoldOutPlanningInput,
    InputBinding,
    PlanNodeState,
    PlanRunView,
)
from src.plan_engine.store import (
    ClaimedNodeRunView,
    PlanStore,
    PlanStoreInvariantError,
)
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.models import (
    EventAuthorizationContext,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionResult,
    SkillExecutionRoute,
    SkillExecutionStatus,
    SkillManifest,
)
from src.state.models import LifecycleStage


class AsyncSkillExecutor(Protocol):
    """Worker 依赖的最小统一 Runtime 接口，禁止同步或 Legacy fallback。"""

    async def execute(self, call: SkillCall) -> SkillExecutionResult:
        """执行一次精确版本 SkillCall，不在内部实施 PlanEngine 重试。"""


@dataclass(frozen=True)
class WorkerRunResult:
    """一次 run_once 的可观测批次摘要，不替代 Store 权威事实。"""

    claimed: int
    succeeded: int = 0
    retried: int = 0
    waiting_human: int = 0
    waiting_approval: int = 0
    failed: int = 0


@dataclass(frozen=True)
class _NodeOutcome:
    """单节点执行后的内部计数类别。"""

    category: str


class PlanWorker:
    """按 Store claim 执行固定手卡 DAG 的无状态 async Worker。"""

    def __init__(
        self,
        *,
        store: PlanStore,
        event_store: EventStore | None = None,
        skill_executor: AsyncSkillExecutor,
        worker_id: str,
        failure_policy: FailurePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        max_claims: int = 4,
        lease_seconds: int = 60,
        default_node_deadline_seconds: int = 60,
    ) -> None:
        """冻结 Worker 装配参数，运行期间不得动态扩大并发或租约。"""
        if not worker_id:
            raise ValueError("worker_id 不能为空")
        if type(max_claims) is not int or not 1 <= max_claims <= 4:
            raise ValueError("max_claims 必须是 1 到 4 的精确 int")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 600:
            raise ValueError("lease_seconds 必须是 1 到 600 的精确 int")
        if (
            type(default_node_deadline_seconds) is not int
            or default_node_deadline_seconds < 1
        ):
            raise ValueError("default_node_deadline_seconds 必须是正整数")
        self._store = store
        self._event_store = event_store
        self._skill_executor = skill_executor
        self._worker_id = worker_id
        self._failure_policy = failure_policy or FailurePolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_claims = max_claims
        self._lease_seconds = lease_seconds
        self._default_node_deadline_seconds = default_node_deadline_seconds
        self._resolver = InputBindingResolver()
        self._catalog: Mapping[str, SkillManifest] = {
            manifest.skill_id: manifest for manifest in get_default_skill_catalog()
        }

    async def run_once(
        self,
        plan_run_id: str,
        *,
        deadline_at: datetime | None = None,
    ) -> WorkerRunResult:
        """claim 并协作式收敛一个节点批次，成功事实写 Store 后才返回。"""
        started_at = self._aware_utc(self._clock(), "Worker 当前时间")
        resolved_deadline = (
            started_at + timedelta(seconds=self._default_node_deadline_seconds)
            if deadline_at is None
            else self._aware_utc(deadline_at, "节点 deadline")
        )
        claims = self._store.claim_ready_nodes(
            plan_run_id=plan_run_id,
            worker_id=self._worker_id,
            now=started_at,
            lease_seconds=self._lease_seconds,
            limit=self._max_claims,
            deadline_at=resolved_deadline,
        )
        if not claims:
            return WorkerRunResult(claimed=0)

        outcomes = await asyncio.gather(
            *(
                self._execute_claim(
                    claim,
                    deadline_at=resolved_deadline,
                )
                for claim in claims
            )
        )
        counts = {
            "succeeded": 0,
            "retried": 0,
            "waiting_human": 0,
            "waiting_approval": 0,
            "failed": 0,
        }
        for outcome in outcomes:
            counts[outcome.category] += 1
        return WorkerRunResult(claimed=len(claims), **counts)

    async def run_next_once(
        self,
        *,
        deadline_at: datetime | None = None,
    ) -> WorkerRunResult:
        """跨 PlanRun 领取最高优先级节点，同时复用完全相同的执行核心。"""
        started_at = self._aware_utc(self._clock(), "Worker 当前时间")
        resolved_deadline = (
            started_at + timedelta(seconds=self._default_node_deadline_seconds)
            if deadline_at is None
            else self._aware_utc(deadline_at, "节点 deadline")
        )
        claims = self._store.claim_next_ready_nodes(
            worker_id=self._worker_id,
            now=started_at,
            lease_seconds=self._lease_seconds,
            limit=self._max_claims,
            deadline_at=resolved_deadline,
        )
        if not claims:
            return WorkerRunResult(claimed=0)
        outcomes = await asyncio.gather(
            *(
                self._execute_claim(claim, deadline_at=resolved_deadline)
                for claim in claims
            )
        )
        counts = {
            "succeeded": 0,
            "retried": 0,
            "waiting_human": 0,
            "waiting_approval": 0,
            "failed": 0,
        }
        for outcome in outcomes:
            counts[outcome.category] += 1
        return WorkerRunResult(claimed=len(claims), **counts)

    async def _execute_claim(
        self,
        claim: ClaimedNodeRunView,
        *,
        deadline_at: datetime,
    ) -> _NodeOutcome:
        """在单节点边界捕获未预期错误，并尽力持久化内部失败事实。"""
        try:
            return await self._execute_claim_inner(
                claim,
                deadline_at=deadline_at,
            )
        except Exception:
            # 输入损坏、控制节点不变量或依赖查询异常都不能留下无限 RUNNING。
            # 若 fencing/lease 已失效，_record_result 会再次 fail-closed，此时不能
            # 伪造自己仍有写权，异常继续上抛交给调度告警。
            self._record_result(
                claim,
                PlanNodeState.FAILED,
                {"error": "INTERNAL_INVARIANT"},
            )
            return _NodeOutcome("failed")

    async def _execute_claim_inner(
        self,
        claim: ClaimedNodeRunView,
        *,
        deadline_at: datetime,
    ) -> _NodeOutcome:
        """执行一个已持有 fencing 的节点，并将唯一恢复动作写回 Store。"""
        effective_deadline = claim.deadline_at or deadline_at
        materialized, dependency_outputs = self._materialize_claim(claim)
        self._store.record_node_input(
            node_run_id=claim.node_run_id,
            worker_id=claim.worker_id,
            claim_version=claim.claim_version,
            materialized_input=materialized,
            now=self._aware_utc(self._clock(), "输入记录时间"),
        )
        if self._aware_utc(self._clock(), "deadline 检查时间") >= effective_deadline:
            self._record_result(
                claim,
                PlanNodeState.FAILED,
                {"error": "NODE_DEADLINE_EXPIRED"},
            )
            return _NodeOutcome("failed")

        if claim.node_type in {
            "PREPARE_CARD_BATCH",
            "COLLECT_CARD_RESULTS",
            "VALIDATE_SOLD_OUT_EVENT",
            "COLLECT_SOLD_OUT_RESPONSE",
        }:
            output = self._execute_control(
                claim,
                dependency_outputs=dependency_outputs,
            )
            self._record_result(claim, PlanNodeState.SUCCEEDED, output)
            return _NodeOutcome("succeeded")

        manifest = self._manifest_for_claim(claim)
        try:
            Draft202012Validator(manifest.parameter_schema).validate(
                materialized.parameters
            )
        except JsonSchemaError:
            self._record_result(
                claim,
                PlanNodeState.FAILED,
                {"error": "INVALID_ARGUMENTS"},
            )
            return _NodeOutcome("failed")

        plan_run = self._store.get_plan_run(claim.plan_run_id)
        event_authorization = None
        idempotency_key = None
        lifecycle = LifecycleStage.PRE_LIVE
        if manifest.skill_id in {
            "handle_sold_out_event",
            "recommend_backup_product",
            "generate_on_live_prompt",
        }:
            lifecycle = LifecycleStage.ON_LIVE
        if manifest.skill_id == "handle_sold_out_event":
            event_authorization = self._verified_event_authorization(plan_run)
            idempotency_key = (
                f"plan:{claim.plan_run_id}:node:{claim.node_id}:v"
                f"{plan_run.current_version}"
            )
        call = SkillCall(
            skill_id=manifest.skill_id,
            version=manifest.version,
            context=SkillExecutionContext(
                room_id=plan_run.room_id,
                trace_id=plan_run.trace_id,
                lifecycle=lifecycle,
                execution_route=SkillExecutionRoute.SKILL_RUNTIME,
                deadline_at=effective_deadline,
                idempotency_key=idempotency_key,
                event_authorization=event_authorization,
            ),
            arguments=dict(materialized.parameters),
        )
        try:
            result = await self._skill_executor.execute(call)
        except Exception:
            # 统一 Runtime 正常会把 Handler 异常转成 FailureFact，但 Worker 仍必须
            # 防御进程内 Executor/适配器越界抛错。错误文本可能含供应商或参数信息，
            # 因此只保存稳定内部错误码，并把 NodeRun/PlanRun fail-closed。
            self._record_result(
                claim,
                PlanNodeState.FAILED,
                {"error": "INTERNAL_INVARIANT"},
            )
            return _NodeOutcome("failed")
        if result.skill_id != call.skill_id or result.version != call.version:
            self._record_result(
                claim,
                PlanNodeState.FAILED,
                {"error": "RESULT_IDENTITY_MISMATCH"},
            )
            return _NodeOutcome("failed")
        if result.status is SkillExecutionStatus.SUCCESS:
            self._record_result(
                claim,
                PlanNodeState.SUCCEEDED,
                result.output or {},
            )
            return _NodeOutcome("succeeded")
        if result.status is SkillExecutionStatus.PENDING:
            self._record_result(
                claim,
                PlanNodeState.WAITING_APPROVAL,
                {"error_code": None if result.error_code is None else result.error_code.value},
            )
            return _NodeOutcome("waiting_approval")
        if result.failure is None:
            self._record_result(
                claim,
                PlanNodeState.FAILED,
                {"error_code": None if result.error_code is None else result.error_code.value},
            )
            return _NodeOutcome("failed")

        decision = self._failure_policy.decide(
            failure=result.failure,
            capability=self._capability_from_claim(claim, manifest),
            attempt_number=claim.attempt_number,
            deadline_at=effective_deadline,
            now=self._aware_utc(self._clock(), "失败策略时间"),
        )
        if decision.action is FailureAction.RETRY:
            assert decision.retry_at is not None
            self._store.schedule_retry(
                node_run_id=claim.node_run_id,
                worker_id=claim.worker_id,
                claim_version=claim.claim_version,
                now=self._aware_utc(self._clock(), "重试调度时间"),
                retry_at=decision.retry_at,
            )
            return _NodeOutcome("retried")
        if decision.action is FailureAction.WAIT_HUMAN:
            self._record_result(
                claim,
                PlanNodeState.WAITING_RECONCILIATION,
                {"failure": result.failure.model_dump(mode="json")},
            )
            return _NodeOutcome("waiting_human")

        # Phase 12A 没有可以从 RUNNING 合法迁移到 SKIPPED 的恢复语义；集中策略即使
        # 将来新增该判定，也必须先扩展状态决策，当前一律 fail-closed。
        self._record_result(
            claim,
            PlanNodeState.FAILED,
            {"failure": result.failure.model_dump(mode="json")},
        )
        return _NodeOutcome("failed")

    def _materialize_claim(
        self,
        claim: ClaimedNodeRunView,
    ) -> tuple[MaterializedNodeInput, dict[str, VersionedNodeOutput]]:
        """解析受限绑定；reclaim 已有指纹时只复用 Store 的冻结快照。"""
        if claim.input_fingerprint is not None:
            return (
                MaterializedNodeInput(
                    parameters=dict(claim.input_snapshot),
                    input_fingerprint=claim.input_fingerprint,
                ),
                {},
            )

        spec = claim.input_snapshot
        raw_bindings = spec.get("input_bindings", {})
        raw_dependencies = spec.get("depends_on", [])
        if not isinstance(raw_bindings, Mapping) or not isinstance(
            raw_dependencies, list
        ):
            raise PlanStoreInvariantError("NodeRun 缺少受控输入绑定快照")
        bindings = {
            parameter_name: InputBinding.model_validate(binding)
            for parameter_name, binding in raw_bindings.items()
        }
        dependencies = tuple(str(item) for item in raw_dependencies)
        dependency_outputs = self._dependency_outputs(
            claim.plan_run_id,
            dependencies,
        )
        plan_run = self._store.get_plan_run(claim.plan_run_id)
        claim_version = self._claim_plan_version(claim)
        plan_version = self._store.get_plan_version(
            claim.plan_run_id,
            claim_version,
        )
        version_input = plan_version.planning_input or plan_run.planning_input
        if plan_run.plan_kind.value == "EMERGENCY_SOLD_OUT":
            planning_input = EmergencySoldOutPlanningInput.model_validate(
                version_input
            )
        else:
            planning_input = CardBatchPlanningInput.model_validate(
                version_input
            )
        return (
            self._resolver.materialize(
                input_bindings=bindings,
                planning_input=planning_input,
                dependency_outputs=dependency_outputs,
                declared_dependencies=frozenset(dependencies),
                current_plan_version=claim_version,
            ),
            dependency_outputs,
        )

    def _dependency_outputs(
        self,
        plan_run_id: str,
        dependencies: tuple[str, ...],
    ) -> dict[str, VersionedNodeOutput]:
        """只读取显式依赖的最新成功 NodeRun，不从 checkpoint 推断结果。"""
        plan_run = self._store.get_plan_run(plan_run_id)
        nodes_by_key = {
            node.logical_key: node for node in self._store.list_nodes(plan_run_id)
        }
        outputs: dict[str, VersionedNodeOutput] = {}
        for logical_key in dependencies:
            node = nodes_by_key.get(logical_key)
            if node is None:
                raise PlanStoreInvariantError(f"依赖节点不存在: {logical_key}")
            successful_runs = [
                item
                for item in self._store.list_node_runs(plan_run_id, node.node_id)
                if item.state is PlanNodeState.SUCCEEDED
            ]
            source_node_id = node.reused_from_node_id
            visited: set[str] = set()
            while not successful_runs and source_node_id is not None:
                if source_node_id in visited:
                    raise PlanStoreInvariantError("复用节点来源形成循环")
                visited.add(source_node_id)
                successful_runs = [
                    item
                    for item in self._store.list_node_runs(
                        plan_run_id,
                        source_node_id,
                    )
                    if item.state is PlanNodeState.SUCCEEDED and not item.superseded
                ]
                source_node = next(
                    (
                        item
                        for version in range(plan_run.current_version, 0, -1)
                        for item in self._store.list_nodes(plan_run_id, version)
                        if item.node_id == source_node_id
                    ),
                    None,
                )
                source_node_id = (
                    None if source_node is None else source_node.reused_from_node_id
                )
            if not successful_runs:
                raise PlanStoreInvariantError(f"依赖节点尚无成功输出: {logical_key}")
            outputs[logical_key] = VersionedNodeOutput(
                plan_version=node.version_number,
                output=successful_runs[-1].output,
            )
        return outputs

    def _execute_control(
        self,
        claim: ClaimedNodeRunView,
        *,
        dependency_outputs: Mapping[str, VersionedNodeOutput],
    ) -> dict[str, object]:
        """执行两个无 Adapter 的确定性控制节点。"""
        plan_run = self._store.get_plan_run(claim.plan_run_id)
        claim_version = self._claim_plan_version(claim)
        plan_version = self._store.get_plan_version(
            claim.plan_run_id,
            claim_version,
        )
        version_input = plan_version.planning_input or plan_run.planning_input
        if claim.node_type == "PREPARE_CARD_BATCH":
            planning_input = CardBatchPlanningInput.model_validate(
                version_input
            )
            return {
                "prepared": True,
                "product_ids": [
                    item.product_id for item in planning_input.live_plan.items[:3]
                ],
            }
        if claim.node_type == "COLLECT_CARD_RESULTS":
            return {
                "cards": [
                    dependency_outputs[key].output for key in dependency_outputs
                ]
            }
        if claim.node_type == "VALIDATE_SOLD_OUT_EVENT":
            planning_input = EmergencySoldOutPlanningInput.model_validate(
                version_input
            )
            record = self._verified_event_record(planning_input)
            return {
                "validated": True,
                "event_id": record.event.event_id,
                "provenance_id": record.provenance.provenance_id,
                "payload_digest": record.event.payload_digest,
            }
        if claim.node_type == "COLLECT_SOLD_OUT_RESPONSE":
            return {
                "responses": [
                    dependency_outputs[key].output for key in dependency_outputs
                ]
            }
        raise PlanStoreInvariantError(f"未知控制节点类型: {claim.node_type}")

    def _claim_plan_version(self, claim: ClaimedNodeRunView) -> int:
        """通过权威 node_id 定位 NodeRun 所属不可变 PlanVersion。"""
        plan_run = self._store.get_plan_run(claim.plan_run_id)
        matches = tuple(
            node.version_number
            for version in range(plan_run.current_version, 0, -1)
            for node in self._store.list_nodes(claim.plan_run_id, version)
            if node.node_id == claim.node_id
        )
        if len(matches) != 1:
            raise PlanStoreInvariantError("NodeRun 无法定位唯一 PlanVersion")
        return matches[0]

    def _verified_event_record(
        self,
        planning_input: EmergencySoldOutPlanningInput,
    ) -> EventInboxRecord:
        """从只读 EventStore 复核状态、事件与 provenance 的完整冻结事实。"""
        if self._event_store is None:
            raise PlanStoreInvariantError("紧急计划 Worker 缺少只读 EventStore")
        record = self._event_store.get_inbox(planning_input.trigger_event_id)
        if record.state not in {
            EventInboxState.VERIFIED,
            EventInboxState.PROCESSING,
        }:
            raise PlanStoreInvariantError("售罄事件已冲突、阻断或闭合")
        if (
            record.event.model_dump(mode="json")
            != planning_input.event.model_dump(mode="json")
            or record.provenance.model_dump(mode="json")
            != planning_input.provenance.model_dump(mode="json")
        ):
            raise PlanStoreInvariantError("紧急计划输入与 EventStore 权威事实不一致")
        return record

    def _verified_event_authorization(
        self,
        plan_run: PlanRunView,
    ) -> EventAuthorizationContext:
        """在售罄写派发前再次读取权威事件并重建不可伪造的授权上下文。"""
        planning_input = EmergencySoldOutPlanningInput.model_validate(
            plan_run.planning_input
        )
        record = self._verified_event_record(planning_input)
        return _build_event_authorization_context(record.event, record.provenance)

    def _manifest_for_claim(self, claim: ClaimedNodeRunView) -> SkillManifest:
        """从启动冻结 Catalog 复核 claim 的 Skill ID 与精确版本。"""
        if claim.skill_id is None or claim.skill_version is None:
            raise PlanStoreInvariantError("Skill NodeRun 缺少精确能力身份")
        manifest = self._catalog.get(claim.skill_id)
        if manifest is None or manifest.version != claim.skill_version:
            raise PlanStoreInvariantError("NodeRun Skill 版本与 Catalog 不一致")
        return manifest

    @staticmethod
    def _capability_from_claim(
        claim: ClaimedNodeRunView,
        manifest: SkillManifest,
    ) -> ResolvedPlanCapability:
        """重建 FailurePolicy 所需的可信能力事实，不接受 Executor 输出覆盖。"""
        return ResolvedPlanCapability(
            node_type=claim.node_type,
            skill_id=manifest.skill_id,
            skill_version=manifest.version,
            lifecycle=manifest.lifecycle,
            risk_level=manifest.risk_level,
            max_attempt_seconds=manifest.max_attempt_seconds,
            resource_keys=claim.resource_keys,
            max_concurrency=4,
        )

    def _record_result(
        self,
        claim: ClaimedNodeRunView,
        state: PlanNodeState,
        output: dict[str, object],
    ) -> None:
        """使用 claim 三元组提交终态，禁止迟到 Worker 绕过 fencing。"""
        self._store.record_node_result(
            node_run_id=claim.node_run_id,
            worker_id=claim.worker_id,
            claim_version=claim.claim_version,
            state=state,
            output=output,
            now=self._aware_utc(self._clock(), "结果提交时间"),
        )

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        """Worker 的所有时钟事实统一为 UTC aware datetime。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise PlanStoreInvariantError(f"{field_name}必须包含时区")
        return value.astimezone(timezone.utc)


class SyncPlanWorkerAdapter:
    """同步 Graph 的受限桥接器，只复用 PlanWorker 的 async 核心。"""

    def __init__(self, worker: PlanWorker) -> None:
        self._worker = worker

    def run_once(
        self,
        plan_run_id: str,
        *,
        deadline_at: datetime | None = None,
    ) -> WorkerRunResult:
        """在无运行中事件循环时执行；async 调用方必须直接 await。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._worker.run_once(plan_run_id, deadline_at=deadline_at)
            )
        raise RuntimeError("运行中的事件循环内必须直接 await PlanWorker.run_once")
