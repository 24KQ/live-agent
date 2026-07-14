"""Phase 12A PlanEngine 的权威计划事实存储边界。

本模块先提供线程安全的内存实现，用于单进程测试和后续服务装配。PlanRun、版本、
节点、依赖和每次 claim 都只在 Store 锁内创建或替换；对外始终返回冻结视图，避免
调用方绕过状态机或通过可变 JSON 引用改写已经持久化的审计事实。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from pydantic import ConfigDict, Field, field_validator

from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.models import (
    CandidatePlanProposal,
    CardBatchPlanningInput,
    FrozenDict,
    NodeRunView,
    PlanNodeKind,
    PlanNodeState,
    PlanNodeView,
    PlanRunState,
    PlanRunView,
    PlanVersionView,
)
from src.plan_engine.state_machine import PlanStateMachine, validate_plan_run_state

if TYPE_CHECKING:
    from src.plan_engine.commands import (
        PlanCommand,
        PlanCommandLedgerView,
        PlanCommandResult,
    )


class PlanStoreInvariantError(RuntimeError):
    """表示写入请求与 Store 已保存的权威计划事实发生冲突。"""


@dataclass(frozen=True)
class MaterializedPlan:
    """创建 PlanRun 前已经闭合的不可变计划值对象。

    ``planning_input`` 提供由 ``run_key`` 标识的冻结业务输入，``proposal`` 保存
    Provider 的候选证据，能力映射则保存可信 Capability Profile 补全的版本、风险、
    超时、并发和资源键。Store 只接受三者完整闭合的对象，不再自行推断能力事实。
    """

    planning_input: CardBatchPlanningInput
    proposal: CandidatePlanProposal
    capabilities_by_logical_key: dict[str, ResolvedPlanCapability]

    def __post_init__(self) -> None:
        """深复制领域模型并冻结能力映射，隔离构造方仍持有的容器引用。"""
        planning_input = CardBatchPlanningInput.model_validate(
            self.planning_input.model_dump(mode="json")
        )
        proposal = CandidatePlanProposal.model_validate(
            self.proposal.model_dump(mode="json")
        )
        expected_keys = {node.logical_key for node in proposal.nodes}
        supplied_keys = set(self.capabilities_by_logical_key)
        if supplied_keys != expected_keys:
            raise PlanStoreInvariantError("物化计划的节点与能力事实未完整闭合")
        capabilities: dict[str, ResolvedPlanCapability] = {}
        expected_control_types = {
            "prepare-card-batch": "PREPARE_CARD_BATCH",
            "collect-card-results": "COLLECT_CARD_RESULTS",
        }
        for logical_key, capability in self.capabilities_by_logical_key.items():
            if not isinstance(capability, ResolvedPlanCapability):
                raise PlanStoreInvariantError("物化计划只能保存可信 Capability 事实")
            candidate = next(
                node for node in proposal.nodes if node.logical_key == logical_key
            )
            if candidate.node_kind is PlanNodeKind.CONTROL:
                expected_node_type = expected_control_types.get(logical_key)
                if (
                    expected_node_type is None
                    or capability.node_type != expected_node_type
                    or capability.skill_id is not None
                    or capability.skill_version is not None
                    or capability.resource_keys
                ):
                    raise PlanStoreInvariantError(
                        f"控制节点 {logical_key} 的能力事实与候选不一致"
                    )
            elif (
                capability.node_type != "SKILL"
                or capability.skill_id != candidate.skill_id
                or not capability.skill_version
            ):
                raise PlanStoreInvariantError(
                    f"Skill 节点 {logical_key} 的能力事实与候选不一致"
                )
            # ResolvedPlanCapability 自身是 frozen dataclass；这里重建容器层并规范 tuple/
            # frozenset，确保摘要与后续 claim 不依赖调用方提供的具体集合实现。
            capabilities[logical_key] = ResolvedPlanCapability(
                node_type=capability.node_type,
                skill_id=capability.skill_id,
                skill_version=capability.skill_version,
                lifecycle=frozenset(capability.lifecycle),
                risk_level=capability.risk_level,
                max_attempt_seconds=capability.max_attempt_seconds,
                resource_keys=tuple(capability.resource_keys),
                max_concurrency=capability.max_concurrency,
            )
        object.__setattr__(self, "planning_input", planning_input)
        object.__setattr__(self, "proposal", proposal)
        object.__setattr__(self, "capabilities_by_logical_key", FrozenDict(capabilities))

    @property
    def digest(self) -> str:
        """生成覆盖输入、候选和可信能力事实的稳定 SHA-256 摘要。

        ``run_key`` 只标识冻结规划输入；同一输入若被不同 Provider 版本或不同能力
        快照物化，Store 必须借此摘要识别冲突并 fail-closed，不能覆盖首次计划。
        """
        capability_snapshot = {
            logical_key: {
                "node_type": capability.node_type,
                "skill_id": capability.skill_id,
                "skill_version": capability.skill_version,
                "lifecycle": sorted(stage.value for stage in capability.lifecycle),
                "risk_level": (
                    capability.risk_level.value
                    if capability.risk_level is not None
                    else None
                ),
                "max_attempt_seconds": capability.max_attempt_seconds,
                "resource_keys": list(capability.resource_keys),
                "max_concurrency": capability.max_concurrency,
            }
            for logical_key, capability in self.capabilities_by_logical_key.items()
        }
        snapshot = {
            "planning_input": self.planning_input.model_dump(mode="json"),
            "proposal": self.proposal.model_dump(mode="json"),
            "capabilities": capability_snapshot,
        }
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


class ClaimedNodeRunView(NodeRunView):
    """一次 claim 的完整冻结视图，额外公开租约与 fencing 权威事实。"""

    model_config = ConfigDict(frozen=True)

    claim_version: int = Field(..., ge=1)
    worker_id: str = Field(..., min_length=1)
    lease_until: datetime
    resource_keys: tuple[str, ...] = Field(default_factory=tuple)
    node_type: str = Field(..., min_length=1)
    skill_id: str | None = None
    skill_version: str | None = None

    @field_validator("lease_until")
    @classmethod
    def _lease_must_include_timezone(cls, value: datetime) -> datetime:
        """租约统一保存为 UTC，禁止本地时间导致错误回收仍有效的 claim。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease_until 必须包含时区")
        return value.astimezone(timezone.utc)


class PlanStore(Protocol):
    """PlanEngine 服务依赖的完整 Store 协议，不暴露实现锁或数据库细节。"""

    def create_or_resume(self, plan: MaterializedPlan) -> PlanRunView:
        """按冻结输入身份原子创建 PlanRun，或安全重放首次创建结果。"""

    def get_plan_run(self, plan_run_id: str) -> PlanRunView:
        """读取一个 PlanRun 的 JSON-safe 冻结视图。"""

    def get_plan_version(self, plan_run_id: str, version_number: int) -> PlanVersionView:
        """读取指定不可变 PlanVersion。"""

    def list_nodes(self, plan_run_id: str, version_number: int | None = None) -> tuple[PlanNodeView, ...]:
        """列出指定版本节点，不返回内部可变记录。"""

    def claim_ready_nodes(
        self,
        *,
        plan_run_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        limit: int = 1,
    ) -> tuple[ClaimedNodeRunView, ...]:
        """为 READY 节点创建独立 NodeRun 和 fencing token。"""

    def heartbeat_node_run(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedNodeRunView:
        """仅在 claim 三元组匹配时延长有效租约。"""

    def reclaim_expired_node(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedNodeRunView:
        """仅回收已过期 claim，并创建更高 fencing token。"""

    def record_node_result(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        state: PlanNodeState,
        output: Any | None,
        now: datetime,
    ) -> ClaimedNodeRunView:
        """在 fencing 匹配时记录 NodeRun 终态并推进节点状态。"""

    def schedule_retry(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        now: datetime,
        retry_at: datetime,
    ) -> PlanNodeView:
        """按 D-015 状态机保存受约束的重试调度事实。"""

    def freeze_plan(self, *, plan_run_id: str) -> PlanRunView:
        """按 D-015 约束冻结 PlanRun。"""

    def list_node_runs(self, plan_run_id: str, node_id: str | None = None) -> tuple[ClaimedNodeRunView, ...]:
        """列出独立保留的历史 NodeRun 视图。"""

    def submit_command(
        self,
        *,
        command: "PlanCommand",
        now: datetime,
    ) -> "PlanCommandResult":
        """原子保存人工命令账本，并返回首次执行结果。"""

    def get_command(self, command_id: str) -> "PlanCommandLedgerView":
        """读取命令首次请求与首次结果的 JSON-safe 冻结视图。"""

    def reconcile_plan_reference(
        self,
        *,
        plan_run_id: str,
        node_id: str,
        outcome: PlanNodeState | str,
        reference: Any,
    ) -> PlanNodeView:
        """在显式对账命令内闭合外部引用事实。"""


class PlanQueryService:
    """PlanEngine 的只读查询门面，只委托 PlanStore 返回冻结领域视图。

    服务不接收 checkpoint 连接、表名或 Graph 状态，因此从类型边界上就无法把
    checkpoint 当作计划事实源。每次调用都重新向 Store 取视图，调用方对
    ``model_dump`` 导出副本的修改不会回流到 Store 内部记录。
    """

    def __init__(self, store: PlanStore) -> None:
        """注入唯一权威 PlanStore；查询层不持有第二份计划缓存。"""
        self._store = store

    def get_plan_run(self, plan_run_id: str) -> PlanRunView:
        """返回指定 PlanRun 的 JSON-safe 冻结视图。"""
        return self._store.get_plan_run(plan_run_id)

    def get_plan_version(
        self,
        plan_run_id: str,
        version_number: int,
    ) -> PlanVersionView:
        """返回指定 PlanVersion，不从 Graph checkpoint 推断版本。"""
        return self._store.get_plan_version(plan_run_id, version_number)

    def list_nodes(
        self,
        plan_run_id: str,
        version_number: int | None = None,
    ) -> tuple[PlanNodeView, ...]:
        """列出 Store 已物化节点的冻结视图。"""
        return self._store.list_nodes(plan_run_id, version_number)

    def list_node_runs(
        self,
        plan_run_id: str,
        node_id: str | None = None,
    ) -> tuple[ClaimedNodeRunView, ...]:
        """列出每次独立 claim 的历史视图，不合并或覆盖 attempt。"""
        return self._store.list_node_runs(plan_run_id, node_id)

    def get_command(self, command_id: str) -> "PlanCommandLedgerView":
        """返回 Store 权威命令账本视图，不从 checkpoint 或服务缓存重建结果。"""
        return self._store.get_command(command_id)


@dataclass(frozen=True)
class _PlanRunRecord:
    """锁内保存的 PlanRun 权威记录；任何状态变化都以 ``replace`` 产生新值。"""

    plan_run_id: str
    room_id: str
    trace_id: str
    run_key: str
    current_version: int
    state: PlanRunState
    planning_input: dict[str, Any]


@dataclass(frozen=True)
class _PlanVersionRecord:
    """首次物化的不可变 PlanVersion 与 Provider 审计快照。"""

    plan_run_id: str
    version_number: int
    provider_id: str
    provider_version: str
    proposal: dict[str, Any]


@dataclass(frozen=True)
class _PlanNodeRecord:
    """锁内节点记录，同时保留依赖和可信能力供调度使用。"""

    node_id: str
    plan_run_id: str
    version_number: int
    logical_key: str
    node_kind: Any
    state: PlanNodeState
    skill_id: str | None
    input_bindings: dict[str, Any]
    depends_on: tuple[str, ...]
    capability: ResolvedPlanCapability
    retry_at: datetime | None


@dataclass(frozen=True)
class _NodeRunRecord:
    """一次 claim 的永久历史事实；后续回收创建新记录而不覆盖本记录身份。"""

    node_run_id: str
    plan_run_id: str
    node_id: str
    attempt_number: int
    claim_version: int
    state: PlanNodeState
    worker_id: str
    lease_until: datetime
    input_snapshot: dict[str, Any]
    output: Any | None
    resource_keys: tuple[str, ...]
    node_type: str
    skill_id: str | None
    skill_version: str | None


@dataclass(frozen=True)
class _CommandLedgerRecord:
    """命令账本的首次请求与首次结果，重复 command_id 永不覆盖。"""

    command: "PlanCommand"
    result: "PlanCommandResult"


class InMemoryPlanStore:
    """使用进程内锁实现原子幂等、claim 和不可变历史的测试 Store。

    此锁只保证一个 Python 进程内的复合检查与写入不可交错，绝不代表生产分布式
    锁语义。未来 PostgreSQL 实现必须用事务、条件更新和唯一约束表达同一不变量。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._plan_run_id_by_run_key: dict[str, str] = {}
        self._digest_by_run_key: dict[str, str] = {}
        self._plan_runs: dict[str, _PlanRunRecord] = {}
        self._versions: dict[tuple[str, int], _PlanVersionRecord] = {}
        self._nodes: dict[str, _PlanNodeRecord] = {}
        self._node_ids_by_plan_version: dict[tuple[str, int], tuple[str, ...]] = {}
        self._node_run_ids_by_node: dict[str, tuple[str, ...]] = {}
        self._node_runs: dict[str, _NodeRunRecord] = {}
        self._commands: dict[str, _CommandLedgerRecord] = {}

    def create_or_resume(self, plan: MaterializedPlan) -> PlanRunView:
        """以 ``run_key`` 为幂等身份，并用完整物化摘要拒绝冲突重放。"""
        if not isinstance(plan, MaterializedPlan):
            raise PlanStoreInvariantError("create_or_resume 必须接收 MaterializedPlan")
        run_key = plan.planning_input.run_key
        digest = plan.digest
        with self._lock:
            existing_id = self._plan_run_id_by_run_key.get(run_key)
            if existing_id is not None:
                if self._digest_by_run_key[run_key] != digest:
                    raise PlanStoreInvariantError("同一 run_key 的物化计划摘要冲突")
                return self._plan_run_view(self._plan_runs[existing_id])

            plan_run_id = str(uuid4())
            version_number = 1
            plan_run = _PlanRunRecord(
                plan_run_id=plan_run_id,
                room_id=plan.planning_input.room_id,
                trace_id=plan.planning_input.trace_id,
                run_key=run_key,
                current_version=version_number,
                state=PlanRunState.ACTIVE,
                planning_input=plan.planning_input.model_dump(mode="json"),
            )
            version = _PlanVersionRecord(
                plan_run_id=plan_run_id,
                version_number=version_number,
                provider_id=plan.proposal.provider_id,
                provider_version=plan.proposal.provider_version,
                proposal=plan.proposal.model_dump(mode="json"),
            )
            node_ids: list[str] = []
            for candidate in plan.proposal.nodes:
                capability = plan.capabilities_by_logical_key[candidate.logical_key]
                initial_state = (
                    PlanNodeState.READY
                    if capability.node_type == "PREPARE_CARD_BATCH"
                    else PlanNodeState.PENDING
                )
                node = _PlanNodeRecord(
                    node_id=str(uuid4()),
                    plan_run_id=plan_run_id,
                    version_number=version_number,
                    logical_key=candidate.logical_key,
                    node_kind=candidate.node_kind,
                    state=initial_state,
                    skill_id=candidate.skill_id,
                    input_bindings={
                        key: binding.model_dump(mode="json")
                        for key, binding in candidate.input_bindings.items()
                    },
                    depends_on=tuple(candidate.depends_on),
                    capability=capability,
                    retry_at=None,
                )
                self._nodes[node.node_id] = node
                self._node_run_ids_by_node[node.node_id] = ()
                node_ids.append(node.node_id)

            # 所有索引都在同一锁区间最后发布；任何并发读取只会看到完整计划，不能
            # 观察到仅有 PlanRun 而缺少 Version/Node 的半持久化状态。
            self._plan_runs[plan_run_id] = plan_run
            self._versions[(plan_run_id, version_number)] = version
            self._node_ids_by_plan_version[(plan_run_id, version_number)] = tuple(node_ids)
            self._plan_run_id_by_run_key[run_key] = plan_run_id
            self._digest_by_run_key[run_key] = digest
            return self._plan_run_view(plan_run)

    def get_plan_run(self, plan_run_id: str) -> PlanRunView:
        """在锁内定位记录，再投影为与内部 JSON 断开引用的冻结视图。"""
        with self._lock:
            record = self._plan_runs.get(plan_run_id)
            if record is None:
                raise PlanStoreInvariantError("PlanRun 不存在")
            return self._plan_run_view(record)

    def get_plan_version(self, plan_run_id: str, version_number: int) -> PlanVersionView:
        """返回指定版本的审计快照，不回退到当前版本或其他 PlanRun。"""
        selected_version = self._validate_version_number(version_number)
        with self._lock:
            record = self._versions.get((plan_run_id, selected_version))
            if record is None:
                raise PlanStoreInvariantError("PlanVersion 不存在")
            return PlanVersionView(
                plan_run_id=record.plan_run_id,
                version_number=record.version_number,
                provider_id=record.provider_id,
                provider_version=record.provider_version,
                proposal=record.proposal,
            )

    def list_nodes(
        self,
        plan_run_id: str,
        version_number: int | None = None,
    ) -> tuple[PlanNodeView, ...]:
        """按物化顺序返回节点；省略版本时只读取 PlanRun 的权威当前版本。"""
        requested_version = (
            None
            if version_number is None
            else self._validate_version_number(version_number)
        )
        with self._lock:
            plan_run = self._plan_runs.get(plan_run_id)
            if plan_run is None:
                raise PlanStoreInvariantError("PlanRun 不存在")
            selected_version = (
                plan_run.current_version
                if requested_version is None
                else requested_version
            )
            node_ids = self._node_ids_by_plan_version.get((plan_run_id, selected_version))
            if node_ids is None:
                raise PlanStoreInvariantError("PlanVersion 不存在")
            return tuple(self._node_view(self._nodes[node_id]) for node_id in node_ids)

    def claim_ready_nodes(
        self,
        *,
        plan_run_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        limit: int = 1,
    ) -> tuple[ClaimedNodeRunView, ...]:
        """为 READY 节点原子创建 NodeRun，并把节点迁移到 RUNNING。

        attempt_number 表示该节点的历史执行次数，claim_version 是单调递增的 fencing
        token。二者都由 Store 锁内历史推导，Worker 无权自行选择或复用。
        """
        normalized_now = self._aware_utc(now, "claim 时间")
        if not worker_id:
            raise PlanStoreInvariantError("worker_id 不能为空")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise PlanStoreInvariantError("lease_seconds 必须是正整数")
        if type(limit) is not int or limit <= 0:
            raise PlanStoreInvariantError("limit 必须是正整数")

        with self._lock:
            plan_run = self._plan_runs.get(plan_run_id)
            if plan_run is None:
                raise PlanStoreInvariantError("PlanRun 不存在")
            if plan_run.state is not PlanRunState.ACTIVE:
                return ()
            node_ids = self._node_ids_by_plan_version[
                (plan_run_id, plan_run.current_version)
            ]
            # RETRY_WAIT 只有在权威 retry_at 到期后才能经 D-015 回到 READY。这个
            # 晋级与随后的 claim 位于同一锁区间，不会暴露可被另一个 worker 抢先
            # 观察、再重复创建 NodeRun 的中间状态。
            for node_id in node_ids:
                retry_node = self._nodes[node_id]
                if (
                    retry_node.state is PlanNodeState.RETRY_WAIT
                    and retry_node.retry_at is not None
                    and normalized_now >= retry_node.retry_at
                ):
                    self._nodes[node_id] = replace(
                        retry_node,
                        state=PlanStateMachine.transition_node(
                            retry_node.state,
                            PlanNodeState.READY,
                        ),
                        retry_at=None,
                    )
            # 资源锁是跨 PlanRun 的执行约束，不能只在当前 DAG 内过滤。只有仍是各
            # 节点最新 claim、状态为 RUNNING 且 lease 尚未到期的 NodeRun 持有锁；
            # 过期 claim 已失去提交权，因此不会永久阻塞其他计划。选中本批节点后也
            # 立即把资源加入集合，避免一次批量 claim 内部产生冲突。
            locked_resource_keys = self._locked_resource_keys(normalized_now)
            ready_ids: list[str] = []
            for node_id in node_ids:
                node = self._nodes[node_id]
                if node.state is not PlanNodeState.READY:
                    continue
                node_resource_keys = set(node.capability.resource_keys)
                if node_resource_keys & locked_resource_keys:
                    continue
                ready_ids.append(node_id)
                locked_resource_keys.update(node_resource_keys)
                if len(ready_ids) == limit:
                    break
            claimed: list[ClaimedNodeRunView] = []
            for node_id in ready_ids:
                node = self._nodes[node_id]
                historical_ids = self._node_run_ids_by_node[node_id]
                next_attempt = len(historical_ids) + 1
                next_claim_version = (
                    max(
                        (self._node_runs[item].claim_version for item in historical_ids),
                        default=0,
                    )
                    + 1
                )
                running_node = replace(
                    node,
                    state=PlanStateMachine.transition_node(
                        node.state,
                        PlanNodeState.RUNNING,
                    ),
                )
                node_run = _NodeRunRecord(
                    node_run_id=str(uuid4()),
                    plan_run_id=plan_run_id,
                    node_id=node_id,
                    attempt_number=next_attempt,
                    claim_version=next_claim_version,
                    state=PlanNodeState.RUNNING,
                    worker_id=worker_id,
                    lease_until=normalized_now + timedelta(seconds=lease_seconds),
                    input_snapshot={"input_bindings": node.input_bindings},
                    output=None,
                    resource_keys=node.capability.resource_keys,
                    node_type=node.capability.node_type,
                    skill_id=node.capability.skill_id,
                    skill_version=node.capability.skill_version,
                )
                self._nodes[node_id] = running_node
                self._node_runs[node_run.node_run_id] = node_run
                self._node_run_ids_by_node[node_id] = (*historical_ids, node_run.node_run_id)
                claimed.append(self._node_run_view(node_run))
            return tuple(claimed)

    def schedule_retry(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        now: datetime,
        retry_at: datetime,
    ) -> PlanNodeView:
        """由当前有效 claim 把 RUNNING 节点调度到 RETRY_WAIT。

        方法同时闭合本次 NodeRun 与节点状态，但不预先创建下一 attempt；只有未来
        ``claim_ready_nodes`` 观察到权威 retry_at 到期，才会签发新的 fencing token。
        """
        normalized_now = self._aware_utc(now, "重试调度时间")
        normalized_retry_at = self._aware_utc(retry_at, "retry_at")
        if normalized_retry_at < normalized_now:
            raise PlanStoreInvariantError("retry_at 不能早于重试调度时间")
        with self._lock:
            node_run = self._node_runs.get(node_run_id)
            if node_run is None:
                raise PlanStoreInvariantError("NodeRun 不存在")
            if (
                node_run.worker_id != worker_id
                or node_run.claim_version != claim_version
            ):
                raise PlanStoreInvariantError("重试调度的 worker 或 fencing token 不匹配")
            if self._current_node_run_id(node_run.node_id) != node_run_id:
                raise PlanStoreInvariantError("旧 fencing token 不能调度重试")
            if node_run.state is not PlanNodeState.RUNNING:
                raise PlanStoreInvariantError("只有 RUNNING NodeRun 可以调度重试")
            if normalized_now >= node_run.lease_until:
                raise PlanStoreInvariantError("租约已过期，禁止迟到重试调度")

            node = self._nodes[node_run.node_id]
            retry_state = PlanStateMachine.transition_node(
                node.state,
                PlanNodeState.RETRY_WAIT,
            )
            self._node_runs[node_run_id] = replace(node_run, state=retry_state)
            waiting_node = replace(
                node,
                state=retry_state,
                retry_at=normalized_retry_at,
            )
            self._nodes[node.node_id] = waiting_node
            return self._node_view(waiting_node)

    def heartbeat_node_run(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedNodeRunView:
        """仅由当前 claim 持有者续租，错误或过期 fencing 事实一律拒绝。

        校验全部完成后才替换记录，因此错误 token、错误 worker、过期租约和非 RUNNING
        状态都不会产生部分写入。历史 NodeRun 的身份及 attempt_number 始终不变。
        """
        normalized_now = self._aware_utc(now, "heartbeat 时间")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise PlanStoreInvariantError("lease_seconds 必须是正整数")
        with self._lock:
            record = self._node_runs.get(node_run_id)
            if record is None:
                raise PlanStoreInvariantError("NodeRun 不存在")
            if record.worker_id != worker_id or record.claim_version != claim_version:
                raise PlanStoreInvariantError("heartbeat 的 worker 或 fencing token 不匹配")
            if self._current_node_run_id(record.node_id) != node_run_id:
                raise PlanStoreInvariantError("旧 fencing token 永久不能 heartbeat")
            if record.state is not PlanNodeState.RUNNING:
                raise PlanStoreInvariantError("只有 RUNNING NodeRun 可以 heartbeat")
            if normalized_now >= record.lease_until:
                raise PlanStoreInvariantError("已过期租约不能通过 heartbeat 复活")

            requested_lease = normalized_now + timedelta(seconds=lease_seconds)
            renewed = replace(
                record,
                # heartbeat 是续租而非重设；即使调用时间非常靠前，也不允许缩短原租约。
                lease_until=max(record.lease_until, requested_lease),
            )
            self._node_runs[node_run_id] = renewed
            return self._node_run_view(renewed)

    def reclaim_expired_node(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedNodeRunView:
        """仅在当前租约过期后追加新 NodeRun，并签发更高 fencing token。

        旧 NodeRun 不删除也不覆盖：其过期 lease 与旧 token 继续作为审计事实存在；
        ``_current_node_run_id`` 切换到新记录后，旧持有者永久失去 heartbeat 和终态写权。
        """
        normalized_now = self._aware_utc(now, "reclaim 时间")
        if not worker_id:
            raise PlanStoreInvariantError("worker_id 不能为空")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise PlanStoreInvariantError("lease_seconds 必须是正整数")
        with self._lock:
            record = self._node_runs.get(node_run_id)
            if record is None:
                raise PlanStoreInvariantError("NodeRun 不存在")
            if self._current_node_run_id(record.node_id) != node_run_id:
                raise PlanStoreInvariantError("只能回收节点的当前 NodeRun")
            if record.state is not PlanNodeState.RUNNING:
                raise PlanStoreInvariantError("只有 RUNNING NodeRun 可以回收")
            if normalized_now < record.lease_until:
                raise PlanStoreInvariantError("NodeRun 租约尚未过期，禁止 reclaim")
            locked_resource_keys = self._locked_resource_keys(normalized_now)
            if set(record.resource_keys) & locked_resource_keys:
                raise PlanStoreInvariantError("节点资源已被其他有效 claim 持有，禁止 reclaim")

            historical_ids = self._node_run_ids_by_node[record.node_id]
            reclaimed = _NodeRunRecord(
                node_run_id=str(uuid4()),
                plan_run_id=record.plan_run_id,
                node_id=record.node_id,
                attempt_number=len(historical_ids) + 1,
                claim_version=max(
                    self._node_runs[item].claim_version for item in historical_ids
                )
                + 1,
                state=PlanNodeState.RUNNING,
                worker_id=worker_id,
                lease_until=normalized_now + timedelta(seconds=lease_seconds),
                input_snapshot=record.input_snapshot,
                output=None,
                resource_keys=record.resource_keys,
                node_type=record.node_type,
                skill_id=record.skill_id,
                skill_version=record.skill_version,
            )
            self._node_runs[reclaimed.node_run_id] = reclaimed
            self._node_run_ids_by_node[record.node_id] = (
                *historical_ids,
                reclaimed.node_run_id,
            )
            return self._node_run_view(reclaimed)

    def record_node_result(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        state: PlanNodeState,
        output: Any | None,
        now: datetime,
    ) -> ClaimedNodeRunView:
        """在有效当前 fencing 下记录结果，并按 D-015 推进节点及依赖。

        结果 JSON 在进入锁前先严格验证和复制；锁内再核对 NodeRun/worker/token、
        当前 claim 身份与租约。只有全部事实一致才同时替换 NodeRun、节点和聚合状态，
        因而迟到写、重复写或被回收的旧 worker 都无法产生部分更新。
        """
        normalized_now = self._aware_utc(now, "结果提交时间")
        try:
            # 复用既有 NodeRunView 的严格 JSON 校验与递归冻结，再导出普通 JSON 副本
            # 供 Store 内部持有，避免调用方提交后修改原始 output 容器。
            validated_output = NodeRunView(
                node_run_id="validation-only",
                plan_run_id="validation-only",
                node_id="validation-only",
                attempt_number=1,
                state=PlanNodeState.RUNNING,
                output=output,
            ).output
            output_snapshot = json.loads(
                json.dumps(validated_output, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise PlanStoreInvariantError("节点输出必须是 JSON-safe 值") from exc
        with self._lock:
            record = self._node_runs.get(node_run_id)
            if record is None:
                raise PlanStoreInvariantError("NodeRun 不存在")
            if record.worker_id != worker_id or record.claim_version != claim_version:
                raise PlanStoreInvariantError("结果提交的 worker 或 fencing token 不匹配")
            if self._current_node_run_id(record.node_id) != node_run_id:
                raise PlanStoreInvariantError("旧 fencing token 永久不能提交节点终态")
            if record.state is not PlanNodeState.RUNNING:
                raise PlanStoreInvariantError("NodeRun 已经闭合，不能重复提交终态")
            if normalized_now >= record.lease_until:
                raise PlanStoreInvariantError("租约已过期，禁止迟到结果写入")

            node = self._nodes[record.node_id]
            target_state = PlanStateMachine.transition_node(node.state, state)
            completed_run = replace(
                record,
                state=target_state,
                output=output_snapshot,
            )
            completed_node = replace(node, state=target_state)
            self._node_runs[node_run_id] = completed_run
            self._nodes[node.node_id] = completed_node

            # 仅 SUCCEEDED 能满足依赖。每个 PENDING 节点都按其显式 logical_key 依赖
            # 检查，不根据物化顺序猜测拓扑，也不会因一个失败节点误开放后继。
            if target_state is PlanNodeState.SUCCEEDED:
                plan_run = self._plan_runs[record.plan_run_id]
                plan_node_ids = self._node_ids_by_plan_version[
                    (record.plan_run_id, plan_run.current_version)
                ]
                states_by_logical_key = {
                    self._nodes[item].logical_key: self._nodes[item].state
                    for item in plan_node_ids
                }
                for candidate_id in plan_node_ids:
                    candidate = self._nodes[candidate_id]
                    if candidate.state is not PlanNodeState.PENDING:
                        continue
                    if all(
                        states_by_logical_key[dependency]
                        is PlanNodeState.SUCCEEDED
                        for dependency in candidate.depends_on
                    ):
                        ready = replace(
                            candidate,
                            state=PlanStateMachine.transition_node(
                                candidate.state,
                                PlanNodeState.READY,
                            ),
                        )
                        self._nodes[candidate_id] = ready
                        states_by_logical_key[ready.logical_key] = ready.state

                if all(
                    self._nodes[item].state is PlanNodeState.SUCCEEDED
                    for item in plan_node_ids
                ):
                    self._plan_runs[record.plan_run_id] = replace(
                        plan_run,
                        state=PlanRunState.SUCCEEDED,
                    )
            elif target_state is PlanNodeState.FAILED:
                plan_run = self._plan_runs[record.plan_run_id]
                self._plan_runs[record.plan_run_id] = replace(
                    plan_run,
                    state=PlanRunState.FAILED,
                )
            elif target_state is PlanNodeState.FROZEN:
                plan_run = self._plan_runs[record.plan_run_id]
                self._plan_runs[record.plan_run_id] = replace(
                    plan_run,
                    state=PlanRunState.FROZEN,
                )
            return self._node_run_view(completed_run)

    def freeze_plan(self, *, plan_run_id: str) -> PlanRunView:
        """把 ACTIVE PlanRun 原子冻结，同时保留所有节点的既有 D-015 状态。

        这是可恢复的聚合级人工暂停，不等同于把某个 RUNNING 节点迁移到不可恢复的
        ``PlanNodeState.FROZEN``。因此 RESUME 可以只恢复 PlanRun 为 ACTIVE，既不伪造
        NodeRun，也不绕过节点状态机。终态计划禁止重新冻结。
        """
        with self._lock:
            plan_run = self._plan_runs.get(plan_run_id)
            if plan_run is None:
                raise PlanStoreInvariantError("PlanRun 不存在")
            if plan_run.state is PlanRunState.FROZEN:
                return self._plan_run_view(plan_run)
            if plan_run.state is not PlanRunState.ACTIVE:
                raise PlanStoreInvariantError("只有 ACTIVE PlanRun 可以冻结")
            frozen = replace(
                plan_run,
                state=validate_plan_run_state(PlanRunState.FROZEN),
            )
            self._plan_runs[plan_run_id] = frozen
            return self._plan_run_view(frozen)

    def reconcile_plan_reference(
        self,
        *,
        plan_run_id: str,
        node_id: str,
        outcome: PlanNodeState | str,
        reference: Any,
    ) -> PlanNodeView:
        """闭合 WAITING_RECONCILIATION 节点并保存防御复制的外部引用。

        这是 ``CommandService`` 在完成账本、TTL 和乐观并发校验后使用的 Store 原语；
        方法自身仍重验 PlanRun/节点归属、D-015 状态与 outcome，避免错误调用绕过
        状态机。生产 API 不应直接向操作者暴露此低层端口。
        """
        try:
            target_state = PlanNodeState(outcome)
        except (TypeError, ValueError) as exc:
            raise PlanStoreInvariantError("对账 outcome 非法") from exc
        if target_state not in {PlanNodeState.SUCCEEDED, PlanNodeState.FAILED}:
            raise PlanStoreInvariantError("对账 outcome 只能是 SUCCEEDED 或 FAILED")
        try:
            validated_reference = NodeRunView(
                node_run_id="validation-only",
                plan_run_id="validation-only",
                node_id="validation-only",
                attempt_number=1,
                state=PlanNodeState.WAITING_RECONCILIATION,
                output=reference,
            ).output
            reference_snapshot = json.loads(
                json.dumps(validated_reference, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise PlanStoreInvariantError("对账 reference 必须是 JSON-safe 值") from exc

        with self._lock:
            plan_run = self._plan_runs.get(plan_run_id)
            node = self._nodes.get(node_id)
            if plan_run is None or node is None:
                raise PlanStoreInvariantError("对账引用的计划或节点不存在")
            if (
                node.plan_run_id != plan_run_id
                or node.version_number != plan_run.current_version
            ):
                raise PlanStoreInvariantError("对账节点不属于 PlanRun 当前版本")
            reconciled = self._reconcile_reference_locked(
                plan_run=plan_run,
                node=node,
                target_state=target_state,
                reconciliation_payload={
                    "outcome": target_state.value,
                    "reference": reference_snapshot,
                },
            )
            return self._node_view(reconciled)

    def submit_command(
        self,
        *,
        command: "PlanCommand",
        now: datetime,
    ) -> "PlanCommandResult":
        """原子写入人工命令账本，并在同一锁内应用首次有效 APPROVE。

        幂等查询位于所有 TTL、版本和状态检查之前：命令首次成功后即使节点已经变化、
        重放时钟已经过期，也必须返回首次结果。首次请求只有通过全部乐观并发检查才会
        修改节点，拒绝结果同样进入账本，后续重放不会重新尝试状态修改。
        """
        # 延迟导入解除 commands -> store 的协议依赖环；方法执行时两个模块均已完成加载。
        from src.plan_engine.commands import PlanCommand, PlanCommandResult

        if not isinstance(command, PlanCommand):
            raise PlanStoreInvariantError("submit_command 必须接收 PlanCommand")
        with self._lock:
            existing = self._commands.get(command.command_id)
            if existing is not None:
                return existing.result

            normalized_now = self._aware_utc(now, "命令处理时间")
            plan_run = self._plan_runs.get(command.plan_run_id)
            if plan_run is None:
                raise PlanStoreInvariantError("命令引用的 PlanRun 不存在")

            if normalized_now < command.issued_at:
                # issued_at 来自命令事实而非 Store 时钟；未来时间若被接受会人为延长
                # TTL，甚至在签发前应用审批，因此必须保存一次性 fail-closed 结果。
                result: PlanCommandResult = PlanCommandResult(
                    command_id=command.command_id,
                    command_type=command.command_type,
                    plan_run_id=command.plan_run_id,
                    accepted=False,
                    reason="COMMAND_NOT_YET_VALID",
                    plan_version=plan_run.current_version,
                    node_id=command.node_id,
                    completed_at=normalized_now,
                )
            elif normalized_now >= command.issued_at + command.ttl:
                # TTL 由命令类型的服务端常量决定，payload 中任何同名字段都没有权限
                # 延长有效期；过期拒绝同样永久进入首次账本结果。
                result: PlanCommandResult = PlanCommandResult(
                    command_id=command.command_id,
                    command_type=command.command_type,
                    plan_run_id=command.plan_run_id,
                    accepted=False,
                    reason="COMMAND_EXPIRED",
                    plan_version=plan_run.current_version,
                    node_id=command.node_id,
                    completed_at=normalized_now,
                )
            elif command.expected_plan_version != plan_run.current_version:
                # 版本检查发生在节点读取与任何状态写入之前；拒绝结果也进入账本，避免
                # 同一旧命令在未来版本偶然重新生效。
                result = PlanCommandResult(
                    command_id=command.command_id,
                    command_type=command.command_type,
                    plan_run_id=command.plan_run_id,
                    accepted=False,
                    reason="PLAN_VERSION_MISMATCH",
                    plan_version=plan_run.current_version,
                    node_id=command.node_id,
                    completed_at=normalized_now,
                )
            else:
                result = self._apply_first_command(
                    command=command,
                    plan_run=plan_run,
                    completed_at=normalized_now,
                    result_type=PlanCommandResult,
                )

            self._commands[command.command_id] = _CommandLedgerRecord(
                command=command,
                result=result,
            )
            return result

    def get_command(self, command_id: str) -> "PlanCommandLedgerView":
        """每次读取都重建命令账本视图，隔离 Store 内部首次记录引用。"""
        from src.plan_engine.commands import PlanCommandLedgerView

        with self._lock:
            entry = self._commands.get(command_id)
            if entry is None:
                raise PlanStoreInvariantError("命令账本记录不存在")
            command = entry.command
            result = entry.result
            return PlanCommandLedgerView(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                expected_plan_version=command.expected_plan_version,
                node_id=command.node_id,
                expected_node_status=command.expected_node_status,
                payload=command.payload,
                issued_at=command.issued_at,
                expires_at=command.issued_at + command.ttl,
                accepted=result.accepted,
                reason=result.reason,
                plan_version=result.plan_version,
                resulting_node_status=result.resulting_node_status,
                completed_at=result.completed_at,
            )

    def _apply_first_command(
        self,
        *,
        command: Any,
        plan_run: _PlanRunRecord,
        completed_at: datetime,
        result_type: Any,
    ) -> Any:
        """在已通过 TTL/版本检查后应用命令；调用方必须持有 Store 锁。"""
        from src.plan_engine.models import PlanCommandType

        if command.command_type is PlanCommandType.RESUME:
            if command.node_id is not None or command.expected_node_status is not None:
                return result_type(
                    command_id=command.command_id,
                    command_type=command.command_type,
                    plan_run_id=command.plan_run_id,
                    accepted=False,
                    reason="RESUME_MUST_TARGET_PLAN",
                    plan_version=plan_run.current_version,
                    node_id=command.node_id,
                    completed_at=completed_at,
                )
            if plan_run.state is not PlanRunState.FROZEN:
                return result_type(
                    command_id=command.command_id,
                    command_type=command.command_type,
                    plan_run_id=command.plan_run_id,
                    accepted=False,
                    reason="COMMAND_STATE_NOT_APPLICABLE",
                    plan_version=plan_run.current_version,
                    completed_at=completed_at,
                )
            resumed = replace(
                plan_run,
                state=validate_plan_run_state(PlanRunState.ACTIVE),
            )
            self._plan_runs[plan_run.plan_run_id] = resumed
            return result_type(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=True,
                reason="ACCEPTED",
                plan_version=plan_run.current_version,
                completed_at=completed_at,
            )

        if command.node_id is None or command.node_id not in self._nodes:
            return result_type(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="NODE_NOT_FOUND",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                completed_at=completed_at,
            )
        node = self._nodes[command.node_id]
        if (
            node.plan_run_id != command.plan_run_id
            or node.version_number != plan_run.current_version
        ):
            return result_type(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="NODE_NOT_FOUND",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                completed_at=completed_at,
            )
        if command.expected_node_status is not node.state:
            # expected_node_status 是操作者读取计划时看到的并发快照。必须先比较再解释
            # 命令种类，确保失败路径完全不触碰节点状态。
            return result_type(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="NODE_STATUS_MISMATCH",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                resulting_node_status=node.state,
                completed_at=completed_at,
            )
        if command.command_type is PlanCommandType.RECONCILE:
            return self._apply_reconciliation_command(
                command=command,
                plan_run=plan_run,
                node=node,
                completed_at=completed_at,
                result_type=result_type,
            )
        if command.command_type not in {
            PlanCommandType.APPROVE,
            PlanCommandType.REJECT,
        }:
            return result_type(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="COMMAND_TYPE_NOT_IMPLEMENTED",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                resulting_node_status=node.state,
                completed_at=completed_at,
            )
        if node.state is not PlanNodeState.WAITING_APPROVAL:
            return result_type(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="COMMAND_STATE_NOT_APPLICABLE",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                resulting_node_status=node.state,
                completed_at=completed_at,
            )

        target_state = (
            PlanNodeState.READY
            if command.command_type is PlanCommandType.APPROVE
            else PlanNodeState.FAILED
        )
        updated = replace(
            node,
            state=PlanStateMachine.transition_node(node.state, target_state),
        )
        self._nodes[node.node_id] = updated
        if target_state is PlanNodeState.FAILED:
            self._plan_runs[plan_run.plan_run_id] = replace(
                plan_run,
                state=PlanRunState.FAILED,
            )
        return result_type(
            command_id=command.command_id,
            command_type=command.command_type,
            plan_run_id=command.plan_run_id,
            accepted=True,
            reason="ACCEPTED",
            plan_version=plan_run.current_version,
            node_id=command.node_id,
            resulting_node_status=updated.state,
            completed_at=completed_at,
        )

    def _apply_reconciliation_command(
        self,
        *,
        command: Any,
        plan_run: _PlanRunRecord,
        node: _PlanNodeRecord,
        completed_at: datetime,
        result_type: Any,
    ) -> Any:
        """在锁内用显式 outcome 闭合 WAITING_RECONCILIATION 节点。

        对账命令不能凭“已人工处理”这样的模糊事实推进节点；payload 必须明确选择
        SUCCEEDED 或 FAILED，并将 reference 连同原 Worker 输出一起冻结到当前 NodeRun。
        """
        if node.state is not PlanNodeState.WAITING_RECONCILIATION:
            return result_type(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="COMMAND_STATE_NOT_APPLICABLE",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                resulting_node_status=node.state,
                completed_at=completed_at,
            )
        outcome = command.payload.get("outcome")
        if outcome not in {PlanNodeState.SUCCEEDED.value, PlanNodeState.FAILED.value}:
            return result_type(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="INVALID_RECONCILIATION_PAYLOAD",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                resulting_node_status=node.state,
                completed_at=completed_at,
            )

        node_run_id = self._current_node_run_id(node.node_id)
        if node_run_id is None:
            return result_type(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="NODE_RUN_NOT_FOUND",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                resulting_node_status=node.state,
                completed_at=completed_at,
            )
        node_run = self._node_runs[node_run_id]
        if node_run.state is not PlanNodeState.WAITING_RECONCILIATION:
            return result_type(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="NODE_RUN_STATE_MISMATCH",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                resulting_node_status=node.state,
                completed_at=completed_at,
            )

        target_state = PlanNodeState(outcome)
        reconciled_output = json.loads(
            json.dumps(
                {
                    "worker_output": node_run.output,
                    "reconciliation": command.payload,
                },
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        reconciled_run = replace(
            node_run,
            state=PlanStateMachine.transition_node(node_run.state, target_state),
            output=reconciled_output,
        )
        reconciled_node = replace(
            node,
            state=PlanStateMachine.transition_node(node.state, target_state),
        )
        self._node_runs[node_run_id] = reconciled_run
        self._nodes[node.node_id] = reconciled_node

        if target_state is PlanNodeState.FAILED:
            self._plan_runs[plan_run.plan_run_id] = replace(
                plan_run,
                state=PlanRunState.FAILED,
            )
        else:
            self._ready_satisfied_dependents(plan_run.plan_run_id)

        return result_type(
            command_id=command.command_id,
            command_type=command.command_type,
            plan_run_id=command.plan_run_id,
            accepted=True,
            reason="ACCEPTED",
            plan_version=plan_run.current_version,
            node_id=command.node_id,
            resulting_node_status=reconciled_node.state,
            completed_at=completed_at,
        )

    def _reconcile_reference_locked(
        self,
        *,
        plan_run: _PlanRunRecord,
        node: _PlanNodeRecord,
        target_state: PlanNodeState,
        reconciliation_payload: dict[str, Any],
    ) -> _PlanNodeRecord:
        """在 Store 锁内按 D-015 闭合对账节点并更新对应 NodeRun。

        调用方已经完成 reference 的 JSON-safe 防御复制；本方法仍重验节点和最新
        NodeRun 都处于 WAITING_RECONCILIATION，且目标只能是 SUCCEEDED/FAILED。
        全部校验完成后才替换记录，失败路径不会产生部分状态写入。
        """
        if target_state not in {PlanNodeState.SUCCEEDED, PlanNodeState.FAILED}:
            raise PlanStoreInvariantError("对账目标只能是 SUCCEEDED 或 FAILED")
        if node.state is not PlanNodeState.WAITING_RECONCILIATION:
            raise PlanStoreInvariantError("只有 WAITING_RECONCILIATION 节点可以对账")
        node_run_id = self._current_node_run_id(node.node_id)
        if node_run_id is None:
            raise PlanStoreInvariantError("对账节点缺少 NodeRun")
        node_run = self._node_runs[node_run_id]
        if node_run.state is not PlanNodeState.WAITING_RECONCILIATION:
            raise PlanStoreInvariantError("对账 NodeRun 状态不匹配")

        reconciled_output = json.loads(
            json.dumps(
                {
                    "worker_output": node_run.output,
                    "reconciliation": reconciliation_payload,
                },
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        reconciled_run = replace(
            node_run,
            state=PlanStateMachine.transition_node(node_run.state, target_state),
            output=reconciled_output,
        )
        reconciled_node = replace(
            node,
            state=PlanStateMachine.transition_node(node.state, target_state),
        )
        self._node_runs[node_run_id] = reconciled_run
        self._nodes[node.node_id] = reconciled_node
        if target_state is PlanNodeState.FAILED:
            self._plan_runs[plan_run.plan_run_id] = replace(
                plan_run,
                state=PlanRunState.FAILED,
            )
        else:
            self._ready_satisfied_dependents(plan_run.plan_run_id)
        return reconciled_node

    def _ready_satisfied_dependents(self, plan_run_id: str) -> None:
        """把依赖全部成功的 PENDING 节点推进到 READY；调用方必须持有锁。"""
        plan_run = self._plan_runs[plan_run_id]
        node_ids = self._node_ids_by_plan_version[
            (plan_run_id, plan_run.current_version)
        ]
        states_by_logical_key = {
            self._nodes[item].logical_key: self._nodes[item].state for item in node_ids
        }
        for node_id in node_ids:
            candidate = self._nodes[node_id]
            if candidate.state is not PlanNodeState.PENDING:
                continue
            if all(
                states_by_logical_key[dependency] is PlanNodeState.SUCCEEDED
                for dependency in candidate.depends_on
            ):
                ready = replace(
                    candidate,
                    state=PlanStateMachine.transition_node(
                        candidate.state,
                        PlanNodeState.READY,
                    ),
                )
                self._nodes[node_id] = ready
                states_by_logical_key[ready.logical_key] = ready.state

        if all(
            self._nodes[item].state is PlanNodeState.SUCCEEDED for item in node_ids
        ):
            self._plan_runs[plan_run_id] = replace(
                plan_run,
                state=PlanRunState.SUCCEEDED,
            )

    def list_node_runs(
        self,
        plan_run_id: str,
        node_id: str | None = None,
    ) -> tuple[ClaimedNodeRunView, ...]:
        """按节点物化顺序和 attempt 顺序返回全部历史 claim 的防御性视图。"""
        with self._lock:
            plan_run = self._plan_runs.get(plan_run_id)
            if plan_run is None:
                raise PlanStoreInvariantError("PlanRun 不存在")
            plan_node_ids = self._node_ids_by_plan_version[
                (plan_run_id, plan_run.current_version)
            ]
            if node_id is not None:
                if node_id not in plan_node_ids:
                    raise PlanStoreInvariantError("节点不属于指定 PlanRun")
                selected_node_ids = (node_id,)
            else:
                selected_node_ids = plan_node_ids
            return tuple(
                self._node_run_view(self._node_runs[node_run_id])
                for selected_node_id in selected_node_ids
                for node_run_id in self._node_run_ids_by_node[selected_node_id]
            )

    def _current_node_run_id(self, node_id: str) -> str | None:
        """返回节点最新 claim 身份；调用方必须已持有 Store 锁。"""
        historical_ids = self._node_run_ids_by_node.get(node_id, ())
        return historical_ids[-1] if historical_ids else None

    def _locked_resource_keys(self, now: datetime) -> set[str]:
        """汇总所有当前有效 claim 持有的跨计划资源键。

        调用方必须持有 Store 锁。只有节点最新、仍处于 RUNNING 且 lease 严格晚于
        当前时刻的 NodeRun 才持有资源；这样 claim 与 reclaim 使用完全相同的锁
        判定，避免旧计划在资源已被新计划取得后重新抢占。
        """
        return {
            resource_key
            for node_run in self._node_runs.values()
            if node_run.state is PlanNodeState.RUNNING
            and node_run.lease_until > now
            and self._current_node_run_id(node_run.node_id) == node_run.node_run_id
            for resource_key in node_run.resource_keys
        }

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        """把外部时钟事实规范为 UTC，并拒绝无法可靠比较的 naive datetime。"""
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise PlanStoreInvariantError(f"{field_name}必须包含时区")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _validate_version_number(value: Any) -> int:
        """版本查询只接受精确正整数，拒绝 bool/float 的 Python 等值命中。"""
        if type(value) is not int or value < 1:
            raise PlanStoreInvariantError("PlanVersion 版本必须是大于等于 1 的精确 int")
        return value

    @staticmethod
    def _plan_run_view(record: _PlanRunRecord) -> PlanRunView:
        """通过 Pydantic 视图重新冻结规划输入，阻断内部记录引用泄漏。"""
        return PlanRunView(
            plan_run_id=record.plan_run_id,
            room_id=record.room_id,
            trace_id=record.trace_id,
            run_key=record.run_key,
            current_version=record.current_version,
            state=record.state,
            planning_input=record.planning_input,
        )

    @staticmethod
    def _node_view(record: _PlanNodeRecord) -> PlanNodeView:
        """把锁内节点记录投影为只读 JSON-safe 领域视图。"""
        return PlanNodeView(
            node_id=record.node_id,
            plan_run_id=record.plan_run_id,
            version_number=record.version_number,
            logical_key=record.logical_key,
            node_kind=record.node_kind,
            state=record.state,
            skill_id=record.skill_id,
            input_bindings=record.input_bindings,
        )

    @staticmethod
    def _node_run_view(record: _NodeRunRecord) -> ClaimedNodeRunView:
        """返回含租约和 fencing 的冻结 NodeRun 快照，永不暴露内部记录对象。"""
        return ClaimedNodeRunView(
            node_run_id=record.node_run_id,
            plan_run_id=record.plan_run_id,
            node_id=record.node_id,
            attempt_number=record.attempt_number,
            claim_version=record.claim_version,
            state=record.state,
            worker_id=record.worker_id,
            lease_until=record.lease_until,
            input_snapshot=record.input_snapshot,
            output=record.output,
            resource_keys=record.resource_keys,
            node_type=record.node_type,
            skill_id=record.skill_id,
            skill_version=record.skill_version,
        )
