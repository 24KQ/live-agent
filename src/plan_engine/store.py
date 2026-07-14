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
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

import psycopg
from pydantic import ConfigDict, Field, ValidationError, field_validator
from psycopg.rows import dict_row

from src.plan_engine.bindings import MaterializedNodeInput
from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.models import (
    CandidatePlanProposal,
    CardBatchPlanningInput,
    FrozenDict,
    NodeRunView,
    PlanCommandType,
    PlanNodeKind,
    PlanNodeState,
    PlanNodeView,
    PlanRunKind,
    PlanRunState,
    PlanRunView,
    PlanVersionView,
)
from src.plan_engine.state_machine import PlanStateMachine, validate_plan_run_state
from src.state.models import LifecycleStage, RiskLevel

if TYPE_CHECKING:
    from src.plan_engine.commands import (
        PlanCommand,
        PlanCommandLedgerView,
        PlanCommandResult,
    )


class PlanStoreInvariantError(RuntimeError):
    """表示写入请求与 Store 已保存的权威计划事实发生冲突。"""


def _reconciliation_failure_snapshot(failure: dict[str, Any]) -> dict[str, Any]:
    """用 PlanRunView 的严格 JSON 边界复制对账失败事实。

    Store 的内存和 PostgreSQL 实现必须共享完全相同的输入校验。借助冻结视图重建
    JSON 可以拒绝非字符串 key、非有限浮点和任意 Python 对象，并切断调用方引用。
    """
    if not isinstance(failure, dict):
        raise PlanStoreInvariantError("reconciliation_failure 必须是 JSON object")
    try:
        validated = PlanRunView(
            plan_run_id="validation-plan-run",
            room_id="validation-room",
            trace_id="validation-trace",
            run_key="validation-run-key",
            current_version=1,
            state=PlanRunState.ACTIVE,
            reconciliation_failure=failure,
        ).reconciliation_failure
        return json.loads(
            json.dumps(validated, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise PlanStoreInvariantError(
            "reconciliation_failure 必须是严格 JSON object"
        ) from exc


def _validate_reconciliation_signature(signature: str) -> str:
    """事故签名固定为小写 SHA-256，防止重复扫描使用不稳定身份。"""
    if (
        not isinstance(signature, str)
        or len(signature) != 64
        or any(character not in "0123456789abcdef" for character in signature)
    ):
        raise PlanStoreInvariantError("reconciliation_signature 必须是小写 SHA-256")
    return signature


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
    input_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    deadline_at: datetime | None = None

    @field_validator("lease_until", "deadline_at")
    @classmethod
    def _execution_times_must_include_timezone(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        """租约和 deadline 统一保存为 UTC，禁止本地时区改变执行边界。"""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("NodeRun 执行时间必须包含时区")
        return value.astimezone(timezone.utc)


class PlanStore(Protocol):
    """PlanEngine 服务依赖的完整 Store 协议，不暴露实现锁或数据库细节。"""

    def create_or_resume(self, plan: MaterializedPlan) -> PlanRunView:
        """按冻结输入身份原子创建 PlanRun，或安全重放首次创建结果。"""

    def get_plan_run(self, plan_run_id: str) -> PlanRunView:
        """读取一个 PlanRun 的 JSON-safe 冻结视图。"""

    def list_plan_runs(self, *, include_terminal: bool = False) -> tuple[PlanRunView, ...]:
        """列出可扫描计划；默认只返回非终态或仍有对账事故的计划。"""

    def record_reconciliation_failure(
        self,
        *,
        plan_run_id: str,
        failure: dict[str, Any],
        signature: str,
        now: datetime,
    ) -> PlanRunView:
        """持久化 checkpoint 对账事故并按安全状态冻结计划。"""

    def clear_reconciliation_failure(
        self,
        *,
        plan_run_id: str,
        now: datetime,
    ) -> PlanRunView:
        """证据重新一致后清除当前阻断，但保留累计对账次数。"""

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
        deadline_at: datetime | None = None,
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

    def record_node_input(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        materialized_input: MaterializedNodeInput,
        now: datetime,
    ) -> ClaimedNodeRunView:
        """在外部执行前保存不可变输入快照与指纹。"""

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
    plan_kind: PlanRunKind = PlanRunKind.CARD_BATCH
    priority: int = 0
    root_plan_run_id: str | None = None
    parent_plan_run_id: str | None = None
    trigger_event_id: str | None = None
    reconciliation_required: bool = False
    reconciliation_failure: dict[str, Any] | None = None
    reconciliation_signature: str | None = None
    reconciliation_attempt_count: int = 0
    last_reconciled_at: datetime | None = None


@dataclass(frozen=True)
class _PlanVersionRecord:
    """首次物化的不可变 PlanVersion 与 Provider 审计快照。"""

    plan_run_id: str
    version_number: int
    provider_id: str
    provider_version: str
    proposal: dict[str, Any]
    change_reason: str = "INITIAL"
    source_event_ids: tuple[str, ...] = ()


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
    deadline_at: datetime | None


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
    input_fingerprint: str | None
    deadline_at: datetime | None


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
                    deadline_at=None,
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

    def list_plan_runs(
        self,
        *,
        include_terminal: bool = False,
    ) -> tuple[PlanRunView, ...]:
        """按稳定 ID 返回扫描候选，终态且无事故的计划默认不参与后台扫描。"""
        with self._lock:
            records = sorted(
                self._plan_runs.values(),
                key=lambda item: item.plan_run_id,
            )
            selected = (
                records
                if include_terminal
                else [
                    record
                    for record in records
                    if record.state in {PlanRunState.ACTIVE, PlanRunState.FROZEN}
                    or record.reconciliation_required
                ]
            )
            return tuple(self._plan_run_view(record) for record in selected)

    def record_reconciliation_failure(
        self,
        *,
        plan_run_id: str,
        failure: dict[str, Any],
        signature: str,
        now: datetime,
    ) -> PlanRunView:
        """原子记录当前事故；相同签名重扫只增加次数，不创建第二份事故。"""
        failure_snapshot = _reconciliation_failure_snapshot(failure)
        validated_signature = _validate_reconciliation_signature(signature)
        normalized_now = self._aware_utc(now, "对账时间")
        with self._lock:
            plan_run = self._plan_runs.get(plan_run_id)
            if plan_run is None:
                raise PlanStoreInvariantError("PlanRun 不存在")
            target_state = (
                validate_plan_run_state(PlanRunState.FROZEN)
                if plan_run.state is PlanRunState.ACTIVE
                else plan_run.state
            )
            updated = replace(
                plan_run,
                state=target_state,
                reconciliation_required=True,
                reconciliation_failure=failure_snapshot,
                reconciliation_signature=validated_signature,
                reconciliation_attempt_count=(
                    plan_run.reconciliation_attempt_count + 1
                ),
                last_reconciled_at=normalized_now,
            )
            self._plan_runs[plan_run_id] = updated
            return self._plan_run_view(updated)

    def clear_reconciliation_failure(
        self,
        *,
        plan_run_id: str,
        now: datetime,
    ) -> PlanRunView:
        """一致后清除当前事故，不重置累计次数，也不自动解冻业务计划。"""
        normalized_now = self._aware_utc(now, "对账时间")
        with self._lock:
            plan_run = self._plan_runs.get(plan_run_id)
            if plan_run is None:
                raise PlanStoreInvariantError("PlanRun 不存在")
            if not plan_run.reconciliation_required:
                return self._plan_run_view(plan_run)
            updated = replace(
                plan_run,
                reconciliation_required=False,
                reconciliation_failure=None,
                reconciliation_signature=None,
                last_reconciled_at=normalized_now,
            )
            self._plan_runs[plan_run_id] = updated
            return self._plan_run_view(updated)

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
                change_reason=record.change_reason,
                source_event_ids=record.source_event_ids,
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
        deadline_at: datetime | None = None,
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
        normalized_deadline = (
            None
            if deadline_at is None
            else self._aware_utc(deadline_at, "节点 deadline")
        )

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
                persisted_deadline = node.deadline_at or normalized_deadline
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
                    deadline_at=persisted_deadline,
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
                    input_snapshot={
                        "input_bindings": node.input_bindings,
                        "depends_on": list(node.depends_on),
                    },
                    output=None,
                    resource_keys=node.capability.resource_keys,
                    node_type=node.capability.node_type,
                    skill_id=node.capability.skill_id,
                    skill_version=node.capability.skill_version,
                    input_fingerprint=None,
                    deadline_at=persisted_deadline,
                )
                self._nodes[node_id] = running_node
                self._node_runs[node_run.node_run_id] = node_run
                self._node_run_ids_by_node[node_id] = (*historical_ids, node_run.node_run_id)
                claimed.append(self._node_run_view(node_run))
            return tuple(claimed)

    def record_node_input(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        materialized_input: MaterializedNodeInput,
        now: datetime,
    ) -> ClaimedNodeRunView:
        """在 Skill/控制节点执行前原子保存解析后的输入事实。

        相同 fencing 对同一指纹重复写入按幂等重放处理；不同输入则表示 Worker 在
        claim 后改变了执行语义，必须 fail-closed。只有当前、未过期且仍 RUNNING 的
        NodeRun 可以写入，防止迟到 Worker 篡改新 claim 的审计快照。
        """
        normalized_now = self._aware_utc(now, "输入记录时间")
        if not isinstance(materialized_input, MaterializedNodeInput):
            raise PlanStoreInvariantError("必须使用 MaterializedNodeInput 记录节点输入")
        input_snapshot = json.loads(
            json.dumps(
                materialized_input.parameters,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        with self._lock:
            node_run = self._node_runs.get(node_run_id)
            if node_run is None:
                raise PlanStoreInvariantError("NodeRun 不存在")
            if (
                node_run.worker_id != worker_id
                or node_run.claim_version != claim_version
            ):
                raise PlanStoreInvariantError("输入记录的 worker 或 fencing token 不匹配")
            if self._current_node_run_id(node_run.node_id) != node_run_id:
                raise PlanStoreInvariantError("旧 fencing token 不能记录节点输入")
            if node_run.state is not PlanNodeState.RUNNING:
                raise PlanStoreInvariantError("只有 RUNNING NodeRun 可以记录输入")
            if normalized_now >= node_run.lease_until:
                raise PlanStoreInvariantError("租约已过期，禁止迟到输入写入")
            if node_run.input_fingerprint is not None:
                if (
                    node_run.input_fingerprint != materialized_input.input_fingerprint
                    or node_run.input_snapshot != input_snapshot
                ):
                    raise PlanStoreInvariantError("同一 NodeRun 的输入指纹或快照发生冲突")
                return self._node_run_view(node_run)

            recorded = replace(
                node_run,
                input_snapshot=input_snapshot,
                input_fingerprint=materialized_input.input_fingerprint,
            )
            self._node_runs[node_run_id] = recorded
            return self._node_run_view(recorded)

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
                input_fingerprint=record.input_fingerprint,
                deadline_at=record.deadline_at,
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
            elif (
                plan_run.reconciliation_required
                and command.command_type is not PlanCommandType.RECONCILE
            ):
                # checkpoint 事故属于 PlanRun 级 fail-closed 门禁。普通审批、拒绝和
                # RESUME 均不能绕过；拒绝结果仍进入 Command Ledger，供重放审计。
                result = PlanCommandResult(
                    command_id=command.command_id,
                    command_type=command.command_type,
                    plan_run_id=command.plan_run_id,
                    accepted=False,
                    reason="RECONCILIATION_REQUIRED",
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
            plan_kind=record.plan_kind,
            priority=record.priority,
            root_plan_run_id=record.root_plan_run_id,
            parent_plan_run_id=record.parent_plan_run_id,
            trigger_event_id=record.trigger_event_id,
            reconciliation_required=record.reconciliation_required,
            reconciliation_failure=record.reconciliation_failure,
            reconciliation_signature=record.reconciliation_signature,
            reconciliation_attempt_count=record.reconciliation_attempt_count,
            last_reconciled_at=record.last_reconciled_at,
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
            input_fingerprint=record.input_fingerprint,
            deadline_at=record.deadline_at,
        )


class PostgresPlanStore:
    """使用 PostgreSQL 事务实现跨进程 PlanStore 权威语义。

    每个公开方法使用独立 READ COMMITTED 事务。节点 claim 以
    ``FOR UPDATE SKIP LOCKED`` 争抢关系行，并用事务级 advisory lock 串行化相同
    resource key 的不同节点；NodeRun 的终态、心跳、重试和输入写入都必须同时匹配
    ``node_run_id + lease_owner + claim_version``，且目标必须仍是节点最新 claim。

    本实现只访问 Phase 12A 六张公开表。它不读取 LangGraph PostgresSaver 私有表，
    也不尝试把两个独立连接包装成伪原子事务。
    """

    def __init__(self, settings: Any) -> None:
        """保存数据库连接配置；Store 不持有跨调用连接或进程内权威缓存。"""
        self._settings = settings

    def create_or_resume(self, plan: MaterializedPlan) -> PlanRunView:
        """原子物化完整首版 DAG，或按 run_key 安全重放首次计划。

        ``INSERT ... ON CONFLICT DO NOTHING`` 会在并发首写时等待胜者提交；随后读取
        已提交摘要并严格比较，避免同一 run_key 的不同计划静默覆盖。Run、Version、
        Node 与依赖边位于一个事务，任何异常都会整体回滚。
        """
        if not isinstance(plan, MaterializedPlan):
            raise PlanStoreInvariantError("create_or_resume 必须接收 MaterializedPlan")
        plan_run_id = str(uuid4())
        plan_version_id = str(uuid4())
        version_number = 1
        planning_input = plan.planning_input.model_dump(mode="json")
        proposal = plan.proposal.model_dump(mode="json")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO plan_runs (
                            plan_run_id, room_id, trace_id, run_key, plan_digest,
                            current_version, execution_route, state, planning_input
                        ) VALUES (
                            %(plan_run_id)s::uuid, %(room_id)s, %(trace_id)s,
                            %(run_key)s, %(plan_digest)s, %(current_version)s,
                            'PLAN_ENGINE', %(state)s, %(planning_input)s
                        )
                        ON CONFLICT (run_key) DO NOTHING
                        RETURNING plan_run_id::text AS plan_run_id;
                        """,
                        {
                            "plan_run_id": plan_run_id,
                            "room_id": plan.planning_input.room_id,
                            "trace_id": plan.planning_input.trace_id,
                            "run_key": plan.planning_input.run_key,
                            "plan_digest": plan.digest,
                            "current_version": version_number,
                            "state": PlanRunState.ACTIVE.value,
                            "planning_input": self._jsonb(planning_input),
                        },
                    )
                    inserted = cursor.fetchone()
                    if inserted is None:
                        cursor.execute(
                            """
                            SELECT plan_run_id::text AS plan_run_id, plan_digest
                            FROM plan_runs
                            WHERE run_key = %(run_key)s;
                            """,
                            {"run_key": plan.planning_input.run_key},
                        )
                        existing = cursor.fetchone()
                        if existing is None:
                            raise PlanStoreInvariantError(
                                "并发创建完成后无法读取 PlanRun"
                            )
                        if str(existing["plan_digest"]) != plan.digest:
                            raise PlanStoreInvariantError(
                                "同一 run_key 的物化计划摘要冲突"
                            )
                        record = self._load_plan_run(
                            cursor,
                            str(existing["plan_run_id"]),
                        )
                        connection.commit()
                        return self._plan_run_view(record)

                    cursor.execute(
                        """
                        INSERT INTO plan_versions (
                            plan_version_id, plan_run_id, version_number,
                            provider_id, provider_version, proposal
                        ) VALUES (
                            %(plan_version_id)s::uuid, %(plan_run_id)s::uuid,
                            %(version_number)s, %(provider_id)s,
                            %(provider_version)s, %(proposal)s
                        );
                        """,
                        {
                            "plan_version_id": plan_version_id,
                            "plan_run_id": plan_run_id,
                            "version_number": version_number,
                            "provider_id": plan.proposal.provider_id,
                            "provider_version": plan.proposal.provider_version,
                            "proposal": self._jsonb(proposal),
                        },
                    )
                    node_id_by_logical_key: dict[str, str] = {}
                    for node_order, candidate in enumerate(plan.proposal.nodes):
                        capability = plan.capabilities_by_logical_key[
                            candidate.logical_key
                        ]
                        node_id = str(uuid4())
                        node_id_by_logical_key[candidate.logical_key] = node_id
                        initial_state = (
                            PlanNodeState.READY
                            if capability.node_type == "PREPARE_CARD_BATCH"
                            else PlanNodeState.PENDING
                        )
                        cursor.execute(
                            """
                            INSERT INTO plan_nodes (
                                node_id, plan_version_id, plan_run_id,
                                version_number, node_order, logical_key,
                                node_kind, state, skill_id, skill_version,
                                input_bindings, capability, resource_keys
                            ) VALUES (
                                %(node_id)s::uuid, %(plan_version_id)s::uuid,
                                %(plan_run_id)s::uuid, %(version_number)s,
                                %(node_order)s, %(logical_key)s, %(node_kind)s,
                                %(state)s, %(skill_id)s, %(skill_version)s,
                                %(input_bindings)s, %(capability)s,
                                %(resource_keys)s::text[]
                            );
                            """,
                            {
                                "node_id": node_id,
                                "plan_version_id": plan_version_id,
                                "plan_run_id": plan_run_id,
                                "version_number": version_number,
                                "node_order": node_order,
                                "logical_key": candidate.logical_key,
                                "node_kind": candidate.node_kind.value,
                                "state": initial_state.value,
                                "skill_id": capability.skill_id,
                                "skill_version": capability.skill_version,
                                "input_bindings": self._jsonb(
                                    {
                                        key: binding.model_dump(mode="json")
                                        for key, binding in candidate.input_bindings.items()
                                    }
                                ),
                                "capability": self._jsonb(
                                    self._capability_snapshot(capability)
                                ),
                                "resource_keys": list(capability.resource_keys),
                            },
                        )
                    for candidate in plan.proposal.nodes:
                        for dependency_order, dependency_key in enumerate(
                            candidate.depends_on
                        ):
                            cursor.execute(
                                """
                                INSERT INTO plan_node_dependencies (
                                    plan_version_id, plan_run_id, node_id,
                                    dependency_node_id, dependency_order
                                ) VALUES (
                                    %(plan_version_id)s::uuid,
                                    %(plan_run_id)s::uuid,
                                    %(node_id)s::uuid,
                                    %(dependency_node_id)s::uuid,
                                    %(dependency_order)s
                                );
                                """,
                                {
                                    "plan_version_id": plan_version_id,
                                    "plan_run_id": plan_run_id,
                                    "node_id": node_id_by_logical_key[
                                        candidate.logical_key
                                    ],
                                    "dependency_node_id": node_id_by_logical_key[
                                        dependency_key
                                    ],
                                    "dependency_order": dependency_order,
                                },
                            )
                    record = self._load_plan_run(cursor, plan_run_id)
                connection.commit()
                return self._plan_run_view(record)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("持久化 PlanRun 失败") from exc

    def get_plan_run(self, plan_run_id: str) -> PlanRunView:
        """从权威关系行读取 PlanRun，不使用进程内缓存。"""
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    record = self._load_plan_run(cursor, plan_run_id)
                connection.commit()
                return self._plan_run_view(record)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("读取 PlanRun 失败") from exc

    def list_plan_runs(
        self,
        *,
        include_terminal: bool = False,
    ) -> tuple[PlanRunView, ...]:
        """列出后台对账候选，默认排除无事故的成功/失败终态计划。"""
        predicate = (
            "TRUE"
            if include_terminal
            else "(state IN ('ACTIVE', 'FROZEN') OR reconciliation_required)"
        )
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT plan_run_id::text AS plan_run_id
                        FROM plan_runs
                        WHERE {predicate}
                        ORDER BY plan_run_id;
                        """
                    )
                    plan_run_ids = [str(row["plan_run_id"]) for row in cursor.fetchall()]
                    records = tuple(
                        self._load_plan_run(cursor, plan_run_id)
                        for plan_run_id in plan_run_ids
                    )
                connection.commit()
                return tuple(self._plan_run_view(record) for record in records)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("列出 PlanRun 失败") from exc

    def record_reconciliation_failure(
        self,
        *,
        plan_run_id: str,
        failure: dict[str, Any],
        signature: str,
        now: datetime,
    ) -> PlanRunView:
        """在 PlanRun 行锁内记录事故、累计扫描次数并安全冻结活动计划。"""
        failure_snapshot = _reconciliation_failure_snapshot(failure)
        validated_signature = _validate_reconciliation_signature(signature)
        normalized_now = self._aware_utc(now, "对账时间")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    plan_run = self._load_plan_run(
                        cursor,
                        plan_run_id,
                        for_update=True,
                    )
                    target_state = (
                        PlanRunState.FROZEN
                        if plan_run.state is PlanRunState.ACTIVE
                        else plan_run.state
                    )
                    cursor.execute(
                        """
                        UPDATE plan_runs
                        SET state = %(state)s,
                            reconciliation_required = TRUE,
                            reconciliation_failure = %(failure)s,
                            reconciliation_signature = %(signature)s,
                            reconciliation_attempt_count =
                                reconciliation_attempt_count + 1,
                            last_reconciled_at = %(now)s,
                            updated_at = %(now)s
                        WHERE plan_run_id = %(plan_run_id)s::uuid;
                        """,
                        {
                            "state": target_state.value,
                            "failure": self._jsonb(failure_snapshot),
                            "signature": validated_signature,
                            "now": normalized_now,
                            "plan_run_id": plan_run_id,
                        },
                    )
                    if cursor.rowcount != 1:
                        raise PlanStoreInvariantError("记录 PlanRun 对账事故失败")
                    updated = self._load_plan_run(cursor, plan_run_id)
                connection.commit()
                return self._plan_run_view(updated)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("持久化 PlanRun 对账事故失败") from exc

    def clear_reconciliation_failure(
        self,
        *,
        plan_run_id: str,
        now: datetime,
    ) -> PlanRunView:
        """证据一致后清除当前事故字段，累计次数和业务状态保持不变。"""
        normalized_now = self._aware_utc(now, "对账时间")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    plan_run = self._load_plan_run(
                        cursor,
                        plan_run_id,
                        for_update=True,
                    )
                    if plan_run.reconciliation_required:
                        cursor.execute(
                            """
                            UPDATE plan_runs
                            SET reconciliation_required = FALSE,
                                reconciliation_failure = NULL,
                                reconciliation_signature = NULL,
                                last_reconciled_at = %(now)s,
                                updated_at = %(now)s
                            WHERE plan_run_id = %(plan_run_id)s::uuid;
                            """,
                            {"now": normalized_now, "plan_run_id": plan_run_id},
                        )
                        if cursor.rowcount != 1:
                            raise PlanStoreInvariantError("清除 PlanRun 对账事故失败")
                    updated = self._load_plan_run(cursor, plan_run_id)
                connection.commit()
                return self._plan_run_view(updated)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("清除 PlanRun 对账事故失败") from exc

    def get_plan_version(
        self,
        plan_run_id: str,
        version_number: int,
    ) -> PlanVersionView:
        """按严格正整数版本读取不可变候选快照。"""
        selected_version = self._validate_version_number(version_number)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    record = self._load_plan_version(
                        cursor,
                        plan_run_id,
                        selected_version,
                    )
                connection.commit()
                return PlanVersionView(
                    plan_run_id=record.plan_run_id,
                    version_number=record.version_number,
                    provider_id=record.provider_id,
                    provider_version=record.provider_version,
                    proposal=record.proposal,
                    change_reason=record.change_reason,
                    source_event_ids=record.source_event_ids,
                )
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("读取 PlanVersion 失败") from exc

    def list_nodes(
        self,
        plan_run_id: str,
        version_number: int | None = None,
    ) -> tuple[PlanNodeView, ...]:
        """按物化顺序读取指定版本节点，并重建防御性 JSON 视图。"""
        selected_version = (
            None
            if version_number is None
            else self._validate_version_number(version_number)
        )
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    plan_run = self._load_plan_run(cursor, plan_run_id)
                    effective_version = selected_version or plan_run.current_version
                    # 先确认版本存在，使“空版本”不会被误报为空节点列表。
                    self._load_plan_version(cursor, plan_run_id, effective_version)
                    records = self._load_node_records(
                        cursor,
                        plan_run_id,
                        effective_version,
                    )
                connection.commit()
                return tuple(self._node_view(record) for record in records)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("读取 PlanNode 失败") from exc

    def claim_ready_nodes(
        self,
        *,
        plan_run_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        limit: int = 1,
        deadline_at: datetime | None = None,
    ) -> tuple[ClaimedNodeRunView, ...]:
        """用 SKIP LOCKED、资源 advisory lock 与关系状态原子创建 NodeRun。

        节点行锁解决同一节点的并发 claim；资源键可能出现在不同 PlanRun 的不同
        节点上，因此额外按稳定文本哈希取得事务级 advisory lock。哈希碰撞只会让
        无关资源保守串行，不会放宽互斥。取得锁后仍重查有效最新 NodeRun，数据库
        行事实而非 advisory lock 才是最终审计依据。
        """
        normalized_now = self._aware_utc(now, "claim 时间")
        if not worker_id:
            raise PlanStoreInvariantError("worker_id 不能为空")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise PlanStoreInvariantError("lease_seconds 必须是正整数")
        if type(limit) is not int or limit <= 0:
            raise PlanStoreInvariantError("limit 必须是正整数")
        normalized_deadline = (
            None
            if deadline_at is None
            else self._aware_utc(deadline_at, "节点 deadline")
        )
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    # claim 与 freeze/resume 必须锁同一 PlanRun 行。否则并发冻结尚未
                    # 提交时，本事务会读取旧 ACTIVE 并在冻结边界之后偷跑新节点。
                    plan_run = self._load_plan_run(
                        cursor,
                        plan_run_id,
                        for_update=True,
                    )
                    if plan_run.state is not PlanRunState.ACTIVE:
                        connection.commit()
                        return ()
                    cursor.execute(
                        """
                        UPDATE plan_nodes
                        SET state = %(ready)s, retry_at = NULL, updated_at = %(now)s
                        WHERE plan_run_id = %(plan_run_id)s::uuid
                          AND version_number = %(version_number)s
                          AND state = %(retry_wait)s
                          AND retry_at IS NOT NULL
                          AND retry_at <= %(now)s;
                        """,
                        {
                            "ready": PlanNodeState.READY.value,
                            "retry_wait": PlanNodeState.RETRY_WAIT.value,
                            "now": normalized_now,
                            "plan_run_id": plan_run_id,
                            "version_number": plan_run.current_version,
                        },
                    )
                    cursor.execute(
                        """
                        SELECT n.*
                        FROM plan_nodes AS n
                        WHERE n.plan_run_id = %(plan_run_id)s::uuid
                          AND n.version_number = %(version_number)s
                          AND n.state = %(ready)s
                          AND NOT EXISTS (
                              SELECT 1
                              FROM plan_node_dependencies AS d
                              JOIN plan_nodes AS dependency
                                ON dependency.node_id = d.dependency_node_id
                              WHERE d.node_id = n.node_id
                                AND dependency.state <> %(succeeded)s
                          )
                        ORDER BY n.node_order
                        FOR UPDATE OF n SKIP LOCKED;
                        """,
                        {
                            "plan_run_id": plan_run_id,
                            "version_number": plan_run.current_version,
                            "ready": PlanNodeState.READY.value,
                            "succeeded": PlanNodeState.SUCCEEDED.value,
                        },
                    )
                    candidates = tuple(cursor.fetchall())
                    claimed: list[ClaimedNodeRunView] = []
                    for candidate in candidates:
                        resource_keys = tuple(candidate["resource_keys"] or ())
                        if not self._try_lock_resource_keys(cursor, resource_keys):
                            continue
                        if self._resources_are_held(
                            cursor,
                            resource_keys=resource_keys,
                            now=normalized_now,
                        ):
                            continue
                        cursor.execute(
                            """
                            SELECT
                                COALESCE(max(attempt_number), 0) + 1 AS next_attempt,
                                COALESCE(max(claim_version), 0) + 1 AS next_claim_version
                            FROM node_runs
                            WHERE node_id = %(node_id)s::uuid;
                            """,
                            {"node_id": str(candidate["node_id"])},
                        )
                        sequence = cursor.fetchone()
                        persisted_deadline = (
                            candidate["deadline_at"] or normalized_deadline
                        )
                        node_run_id = str(uuid4())
                        lease_until = normalized_now + timedelta(
                            seconds=lease_seconds
                        )
                        dependencies = self._load_dependencies_for_node(
                            cursor,
                            str(candidate["node_id"]),
                        )
                        cursor.execute(
                            """
                            UPDATE plan_nodes
                            SET state = %(running)s,
                                deadline_at = %(deadline_at)s,
                                updated_at = %(now)s
                            WHERE node_id = %(node_id)s::uuid
                              AND state = %(ready)s;
                            """,
                            {
                                "running": PlanNodeState.RUNNING.value,
                                "deadline_at": persisted_deadline,
                                "now": normalized_now,
                                "node_id": str(candidate["node_id"]),
                                "ready": PlanNodeState.READY.value,
                            },
                        )
                        if cursor.rowcount != 1:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO node_runs (
                                node_run_id, plan_run_id, node_id,
                                attempt_number, claim_version, state,
                                lease_owner, lease_until, input_snapshot,
                                resource_keys, node_type, skill_id,
                                skill_version, deadline_at
                            ) VALUES (
                                %(node_run_id)s::uuid, %(plan_run_id)s::uuid,
                                %(node_id)s::uuid, %(attempt_number)s,
                                %(claim_version)s, %(state)s, %(lease_owner)s,
                                %(lease_until)s, %(input_snapshot)s,
                                %(resource_keys)s::text[], %(node_type)s,
                                %(skill_id)s, %(skill_version)s, %(deadline_at)s
                            );
                            """,
                            {
                                "node_run_id": node_run_id,
                                "plan_run_id": plan_run_id,
                                "node_id": str(candidate["node_id"]),
                                "attempt_number": int(sequence["next_attempt"]),
                                "claim_version": int(
                                    sequence["next_claim_version"]
                                ),
                                "state": PlanNodeState.RUNNING.value,
                                "lease_owner": worker_id,
                                "lease_until": lease_until,
                                "input_snapshot": self._jsonb(
                                    {
                                        "input_bindings": dict(
                                            candidate["input_bindings"]
                                        ),
                                        "depends_on": list(dependencies),
                                    }
                                ),
                                "resource_keys": list(resource_keys),
                                "node_type": str(
                                    dict(candidate["capability"])["node_type"]
                                ),
                                "skill_id": candidate["skill_id"],
                                "skill_version": candidate["skill_version"],
                                "deadline_at": persisted_deadline,
                            },
                        )
                        record = self._load_node_run(cursor, node_run_id)
                        claimed.append(self._node_run_view(record))
                        if len(claimed) == limit:
                            break
                connection.commit()
                return tuple(claimed)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("claim READY 节点失败") from exc

    def record_node_input(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        materialized_input: MaterializedNodeInput,
        now: datetime,
    ) -> ClaimedNodeRunView:
        """在执行前以当前 fencing 原子保存解析后的输入快照与指纹。"""
        normalized_now = self._aware_utc(now, "输入记录时间")
        if not isinstance(materialized_input, MaterializedNodeInput):
            raise PlanStoreInvariantError("必须使用 MaterializedNodeInput 记录节点输入")
        input_snapshot = self._json_snapshot(materialized_input.parameters, "节点输入")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    record = self._load_node_run(
                        cursor,
                        node_run_id,
                        for_update=True,
                    )
                    self._assert_live_current_claim(
                        cursor,
                        record=record,
                        worker_id=worker_id,
                        claim_version=claim_version,
                        now=normalized_now,
                        operation="输入记录",
                    )
                    if record.input_fingerprint is not None:
                        if (
                            record.input_fingerprint
                            != materialized_input.input_fingerprint
                            or record.input_snapshot != input_snapshot
                        ):
                            raise PlanStoreInvariantError(
                                "同一 NodeRun 的输入指纹或快照发生冲突"
                            )
                        connection.commit()
                        return self._node_run_view(record)
                    cursor.execute(
                        """
                        UPDATE node_runs
                        SET input_snapshot = %(input_snapshot)s,
                            input_fingerprint = %(input_fingerprint)s,
                            updated_at = %(now)s
                        WHERE node_run_id = %(node_run_id)s::uuid
                          AND lease_owner = %(worker_id)s
                          AND claim_version = %(claim_version)s
                          AND state = %(running)s;
                        """,
                        {
                            "input_snapshot": self._jsonb(input_snapshot),
                            "input_fingerprint": materialized_input.input_fingerprint,
                            "now": normalized_now,
                            "node_run_id": node_run_id,
                            "worker_id": worker_id,
                            "claim_version": claim_version,
                            "running": PlanNodeState.RUNNING.value,
                        },
                    )
                    if cursor.rowcount != 1:
                        raise PlanStoreInvariantError("节点输入条件更新失败")
                    updated = self._load_node_run(cursor, node_run_id)
                connection.commit()
                return self._node_run_view(updated)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("持久化节点输入失败") from exc

    def schedule_retry(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        now: datetime,
        retry_at: datetime,
    ) -> PlanNodeView:
        """闭合当前 NodeRun，并把节点持久化为有权威到期时间的 RETRY_WAIT。"""
        normalized_now = self._aware_utc(now, "重试调度时间")
        normalized_retry_at = self._aware_utc(retry_at, "retry_at")
        if normalized_retry_at < normalized_now:
            raise PlanStoreInvariantError("retry_at 不能早于重试调度时间")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    # NodeRun 的 plan_run_id 是创建后不可变的归属事实。先做无锁定位，
                    # 再按全局顺序 PlanRun -> NodeRun -> Node 加锁，避免与命令路径
                    # （同样先锁 PlanRun）形成 Node/PlanRun 的循环等待。
                    discovered = self._load_node_run(cursor, node_run_id)
                    self._load_plan_run(
                        cursor,
                        discovered.plan_run_id,
                        for_update=True,
                    )
                    node_run = self._load_node_run(
                        cursor,
                        node_run_id,
                        for_update=True,
                    )
                    self._assert_live_current_claim(
                        cursor,
                        record=node_run,
                        worker_id=worker_id,
                        claim_version=claim_version,
                        now=normalized_now,
                        operation="重试调度",
                    )
                    node = self._load_node_record(
                        cursor,
                        node_run.node_id,
                        for_update=True,
                    )
                    retry_state = PlanStateMachine.transition_node(
                        node.state,
                        PlanNodeState.RETRY_WAIT,
                    )
                    cursor.execute(
                        """
                        UPDATE node_runs
                        SET state = %(state)s, completed_at = %(now)s,
                            updated_at = %(now)s
                        WHERE node_run_id = %(node_run_id)s::uuid
                          AND lease_owner = %(worker_id)s
                          AND claim_version = %(claim_version)s
                          AND state = %(running)s;
                        """,
                        {
                            "state": retry_state.value,
                            "now": normalized_now,
                            "node_run_id": node_run_id,
                            "worker_id": worker_id,
                            "claim_version": claim_version,
                            "running": PlanNodeState.RUNNING.value,
                        },
                    )
                    if cursor.rowcount != 1:
                        raise PlanStoreInvariantError("NodeRun 重试条件更新失败")
                    cursor.execute(
                        """
                        UPDATE plan_nodes
                        SET state = %(state)s, retry_at = %(retry_at)s,
                            updated_at = %(now)s
                        WHERE node_id = %(node_id)s::uuid
                          AND state = %(running)s;
                        """,
                        {
                            "state": retry_state.value,
                            "retry_at": normalized_retry_at,
                            "now": normalized_now,
                            "node_id": node.node_id,
                            "running": PlanNodeState.RUNNING.value,
                        },
                    )
                    if cursor.rowcount != 1:
                        raise PlanStoreInvariantError("PlanNode 重试条件更新失败")
                    updated = self._load_node_record(cursor, node.node_id)
                connection.commit()
                return self._node_view(updated)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("调度节点重试失败") from exc

    def heartbeat_node_run(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        claim_version: int,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedNodeRunView:
        """只允许当前有效 claim 延长 lease，且续租永不缩短原截止时间。"""
        normalized_now = self._aware_utc(now, "heartbeat 时间")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise PlanStoreInvariantError("lease_seconds 必须是正整数")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    record = self._load_node_run(
                        cursor,
                        node_run_id,
                        for_update=True,
                    )
                    self._assert_live_current_claim(
                        cursor,
                        record=record,
                        worker_id=worker_id,
                        claim_version=claim_version,
                        now=normalized_now,
                        operation="heartbeat",
                    )
                    requested_lease = normalized_now + timedelta(
                        seconds=lease_seconds
                    )
                    cursor.execute(
                        """
                        UPDATE node_runs
                        SET lease_until = GREATEST(lease_until, %(lease_until)s),
                            updated_at = %(now)s
                        WHERE node_run_id = %(node_run_id)s::uuid
                          AND lease_owner = %(worker_id)s
                          AND claim_version = %(claim_version)s
                          AND state = %(running)s
                        RETURNING node_run_id::text AS node_run_id;
                        """,
                        {
                            "lease_until": requested_lease,
                            "now": normalized_now,
                            "node_run_id": node_run_id,
                            "worker_id": worker_id,
                            "claim_version": claim_version,
                            "running": PlanNodeState.RUNNING.value,
                        },
                    )
                    if cursor.fetchone() is None:
                        raise PlanStoreInvariantError("heartbeat 条件更新失败")
                    renewed = self._load_node_run(cursor, node_run_id)
                connection.commit()
                return self._node_run_view(renewed)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("NodeRun heartbeat 失败") from exc

    def reclaim_expired_node(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedNodeRunView:
        """回收已过期的最新 claim，追加历史 NodeRun 并签发更高 fencing。"""
        normalized_now = self._aware_utc(now, "reclaim 时间")
        if not worker_id:
            raise PlanStoreInvariantError("worker_id 不能为空")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise PlanStoreInvariantError("lease_seconds 必须是正整数")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    record = self._load_node_run(
                        cursor,
                        node_run_id,
                        for_update=True,
                    )
                    if not self._is_current_claim(cursor, record):
                        raise PlanStoreInvariantError("只能回收节点的当前 NodeRun")
                    if record.state is not PlanNodeState.RUNNING:
                        raise PlanStoreInvariantError("只有 RUNNING NodeRun 可以回收")
                    if normalized_now < record.lease_until:
                        raise PlanStoreInvariantError(
                            "NodeRun 租约尚未过期，禁止 reclaim"
                        )
                    if not self._try_lock_resource_keys(
                        cursor,
                        record.resource_keys,
                    ):
                        raise PlanStoreInvariantError(
                            "节点资源正在被其他事务协调，禁止 reclaim"
                        )
                    if self._resources_are_held(
                        cursor,
                        resource_keys=record.resource_keys,
                        now=normalized_now,
                        excluded_node_id=record.node_id,
                    ):
                        raise PlanStoreInvariantError(
                            "节点资源已被其他有效 claim 持有，禁止 reclaim"
                        )
                    cursor.execute(
                        """
                        SELECT
                            max(attempt_number) + 1 AS next_attempt,
                            max(claim_version) + 1 AS next_claim_version
                        FROM node_runs
                        WHERE node_id = %(node_id)s::uuid;
                        """,
                        {"node_id": record.node_id},
                    )
                    sequence = cursor.fetchone()
                    reclaimed_id = str(uuid4())
                    cursor.execute(
                        """
                        INSERT INTO node_runs (
                            node_run_id, plan_run_id, node_id,
                            attempt_number, claim_version, state,
                            lease_owner, lease_until, input_snapshot,
                            input_fingerprint, resource_keys, node_type,
                            skill_id, skill_version, deadline_at
                        ) VALUES (
                            %(node_run_id)s::uuid, %(plan_run_id)s::uuid,
                            %(node_id)s::uuid, %(attempt_number)s,
                            %(claim_version)s, %(state)s, %(lease_owner)s,
                            %(lease_until)s, %(input_snapshot)s,
                            %(input_fingerprint)s, %(resource_keys)s::text[],
                            %(node_type)s, %(skill_id)s, %(skill_version)s,
                            %(deadline_at)s
                        );
                        """,
                        {
                            "node_run_id": reclaimed_id,
                            "plan_run_id": record.plan_run_id,
                            "node_id": record.node_id,
                            "attempt_number": int(sequence["next_attempt"]),
                            "claim_version": int(
                                sequence["next_claim_version"]
                            ),
                            "state": PlanNodeState.RUNNING.value,
                            "lease_owner": worker_id,
                            "lease_until": normalized_now
                            + timedelta(seconds=lease_seconds),
                            "input_snapshot": self._jsonb(record.input_snapshot),
                            "input_fingerprint": record.input_fingerprint,
                            "resource_keys": list(record.resource_keys),
                            "node_type": record.node_type,
                            "skill_id": record.skill_id,
                            "skill_version": record.skill_version,
                            "deadline_at": record.deadline_at,
                        },
                    )
                    reclaimed = self._load_node_run(cursor, reclaimed_id)
                connection.commit()
                return self._node_run_view(reclaimed)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("回收过期 NodeRun 失败") from exc

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
        """在当前有效 fencing 下同时闭合 NodeRun、节点和 PlanRun 聚合状态。"""
        normalized_now = self._aware_utc(now, "结果提交时间")
        output_snapshot = self._json_snapshot(output, "节点输出")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    # 先无锁读取不可变归属，再按 PlanRun -> NodeRun -> Node 顺序加锁。
                    # Command/Reconcile 也从 PlanRun 开始，统一顺序可消除并发人工命令
                    # 与 Worker 结果闭合之间的循环等待。
                    discovered = self._load_node_run(cursor, node_run_id)
                    self._load_plan_run(
                        cursor,
                        discovered.plan_run_id,
                        for_update=True,
                    )
                    node_run = self._load_node_run(
                        cursor,
                        node_run_id,
                        for_update=True,
                    )
                    self._assert_live_current_claim(
                        cursor,
                        record=node_run,
                        worker_id=worker_id,
                        claim_version=claim_version,
                        now=normalized_now,
                        operation="结果提交",
                    )
                    node = self._load_node_record(
                        cursor,
                        node_run.node_id,
                        for_update=True,
                    )
                    target_state = PlanStateMachine.transition_node(node.state, state)
                    failure_fact = (
                        output_snapshot.get("failure")
                        if isinstance(output_snapshot, dict)
                        and isinstance(output_snapshot.get("failure"), dict)
                        else None
                    )
                    cursor.execute(
                        """
                        UPDATE node_runs
                        SET state = %(state)s, output = %(output)s,
                            failure_fact = %(failure_fact)s,
                            completed_at = %(now)s, updated_at = %(now)s
                        WHERE node_run_id = %(node_run_id)s::uuid
                          AND lease_owner = %(worker_id)s
                          AND claim_version = %(claim_version)s
                          AND state = %(running)s;
                        """,
                        {
                            "state": target_state.value,
                            "output": (
                                None
                                if output_snapshot is None
                                else self._jsonb(output_snapshot)
                            ),
                            "failure_fact": (
                                None
                                if failure_fact is None
                                else self._jsonb(failure_fact)
                            ),
                            "now": normalized_now,
                            "node_run_id": node_run_id,
                            "worker_id": worker_id,
                            "claim_version": claim_version,
                            "running": PlanNodeState.RUNNING.value,
                        },
                    )
                    if cursor.rowcount != 1:
                        raise PlanStoreInvariantError("NodeRun 终态条件更新失败")
                    cursor.execute(
                        """
                        UPDATE plan_nodes
                        SET state = %(state)s, updated_at = %(now)s
                        WHERE node_id = %(node_id)s::uuid
                          AND state = %(running)s;
                        """,
                        {
                            "state": target_state.value,
                            "now": normalized_now,
                            "node_id": node.node_id,
                            "running": PlanNodeState.RUNNING.value,
                        },
                    )
                    if cursor.rowcount != 1:
                        raise PlanStoreInvariantError("PlanNode 终态条件更新失败")

                    if target_state is PlanNodeState.SUCCEEDED:
                        self._ready_satisfied_dependents_sql(
                            cursor,
                            plan_run_id=node_run.plan_run_id,
                            now=normalized_now,
                        )
                    elif target_state is PlanNodeState.FAILED:
                        self._update_plan_run_state(
                            cursor,
                            node_run.plan_run_id,
                            PlanRunState.FAILED,
                            normalized_now,
                        )
                    elif target_state is PlanNodeState.FROZEN:
                        self._update_plan_run_state(
                            cursor,
                            node_run.plan_run_id,
                            PlanRunState.FROZEN,
                            normalized_now,
                        )
                    completed = self._load_node_run(cursor, node_run_id)
                connection.commit()
                return self._node_run_view(completed)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("提交 NodeRun 结果失败") from exc

    def list_node_runs(
        self,
        plan_run_id: str,
        node_id: str | None = None,
    ) -> tuple[ClaimedNodeRunView, ...]:
        """按节点物化顺序与 attempt_number 返回永久 NodeRun 历史。"""
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    self._load_plan_run(cursor, plan_run_id)
                    parameters: dict[str, Any] = {"plan_run_id": plan_run_id}
                    node_filter = ""
                    if node_id is not None:
                        cursor.execute(
                            """
                            SELECT 1
                            FROM plan_nodes
                            WHERE node_id = %(node_id)s::uuid
                              AND plan_run_id = %(plan_run_id)s::uuid;
                            """,
                            {"node_id": node_id, "plan_run_id": plan_run_id},
                        )
                        if cursor.fetchone() is None:
                            raise PlanStoreInvariantError(
                                "节点不属于指定 PlanRun"
                            )
                        parameters["node_id"] = node_id
                        node_filter = "AND r.node_id = %(node_id)s::uuid"
                    cursor.execute(
                        f"""
                        SELECT r.*
                        FROM node_runs AS r
                        JOIN plan_nodes AS n ON n.node_id = r.node_id
                        WHERE r.plan_run_id = %(plan_run_id)s::uuid
                          {node_filter}
                        ORDER BY n.node_order, r.attempt_number;
                        """,
                        parameters,
                    )
                    records = tuple(
                        self._node_run_record(row) for row in cursor.fetchall()
                    )
                connection.commit()
                return tuple(self._node_run_view(record) for record in records)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("读取 NodeRun 历史失败") from exc

    def freeze_plan(self, *, plan_run_id: str) -> PlanRunView:
        """原子冻结 ACTIVE PlanRun；重复冻结幂等，终态计划拒绝。"""
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    plan_run = self._load_plan_run(
                        cursor,
                        plan_run_id,
                        for_update=True,
                    )
                    if plan_run.state is PlanRunState.FROZEN:
                        connection.commit()
                        return self._plan_run_view(plan_run)
                    if plan_run.state is not PlanRunState.ACTIVE:
                        raise PlanStoreInvariantError(
                            "只有 ACTIVE PlanRun 可以冻结"
                        )
                    self._update_plan_run_state(
                        cursor,
                        plan_run_id,
                        validate_plan_run_state(PlanRunState.FROZEN),
                        datetime.now(timezone.utc),
                    )
                    frozen = self._load_plan_run(cursor, plan_run_id)
                connection.commit()
                return self._plan_run_view(frozen)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("冻结 PlanRun 失败") from exc

    def reconcile_plan_reference(
        self,
        *,
        plan_run_id: str,
        node_id: str,
        outcome: PlanNodeState | str,
        reference: Any,
    ) -> PlanNodeView:
        """以单事务闭合对账节点，并冻结人工确认的外部引用事实。"""
        try:
            target_state = PlanNodeState(outcome)
        except (TypeError, ValueError) as exc:
            raise PlanStoreInvariantError("对账 outcome 非法") from exc
        if target_state not in {PlanNodeState.SUCCEEDED, PlanNodeState.FAILED}:
            raise PlanStoreInvariantError(
                "对账 outcome 只能是 SUCCEEDED 或 FAILED"
            )
        reference_snapshot = self._json_snapshot(reference, "对账 reference")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    plan_run = self._load_plan_run(
                        cursor,
                        plan_run_id,
                        for_update=True,
                    )
                    node = self._load_node_record(
                        cursor,
                        node_id,
                        for_update=True,
                    )
                    if (
                        node.plan_run_id != plan_run_id
                        or node.version_number != plan_run.current_version
                    ):
                        raise PlanStoreInvariantError(
                            "对账节点不属于 PlanRun 当前版本"
                        )
                    reconciled = self._reconcile_locked_sql(
                        cursor,
                        plan_run=plan_run,
                        node=node,
                        target_state=target_state,
                        reconciliation_payload={
                            "outcome": target_state.value,
                            "reference": reference_snapshot,
                        },
                        completed_at=datetime.now(timezone.utc),
                    )
                connection.commit()
                return self._node_view(reconciled)
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("持久化对账引用失败") from exc

    def submit_command(
        self,
        *,
        command: "PlanCommand",
        now: datetime,
    ) -> "PlanCommandResult":
        """原子记录命令首次结果，并在同一事务完成乐观状态修改。

        command_id 先取得事务级 advisory lock，使两个连接不能同时越过“尚无账本”
        检查。重复投递在 TTL、版本和状态检查之前直接读取首次结果，保证过期后的
        重放也不会改变既有结论。
        """
        from src.plan_engine.commands import PlanCommand, PlanCommandResult

        if not isinstance(command, PlanCommand):
            raise PlanStoreInvariantError("submit_command 必须接收 PlanCommand")
        normalized_now = self._aware_utc(now, "命令处理时间")
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%(key)s, 0));",
                        {"key": f"plan-command:{command.command_id}"},
                    )
                    existing = self._load_command_result(
                        cursor,
                        command.command_id,
                    )
                    if existing is not None:
                        connection.commit()
                        return existing
                    plan_run = self._load_plan_run(
                        cursor,
                        command.plan_run_id,
                        for_update=True,
                    )
                    if normalized_now < command.issued_at:
                        result = PlanCommandResult(
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
                        result = PlanCommandResult(
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
                    elif (
                        plan_run.reconciliation_required
                        and command.command_type is not PlanCommandType.RECONCILE
                    ):
                        # 与内存 Store 保持一致：事故期间只有 RECONCILE 有资格进入
                        # 后续状态校验，其他命令以首次拒绝事实写入账本。
                        result = PlanCommandResult(
                            command_id=command.command_id,
                            command_type=command.command_type,
                            plan_run_id=command.plan_run_id,
                            accepted=False,
                            reason="RECONCILIATION_REQUIRED",
                            plan_version=plan_run.current_version,
                            node_id=command.node_id,
                            completed_at=normalized_now,
                        )
                    else:
                        result = self._apply_command_sql(
                            cursor,
                            command=command,
                            plan_run=plan_run,
                            completed_at=normalized_now,
                        )
                    cursor.execute(
                        """
                        INSERT INTO plan_commands (
                            command_id, command_type, plan_run_id,
                            expected_plan_version, node_id,
                            expected_node_status, payload, issued_at,
                            expires_at, accepted, reason, plan_version,
                            resulting_node_status, completed_at
                        ) VALUES (
                            %(command_id)s, %(command_type)s,
                            %(plan_run_id)s::uuid, %(expected_plan_version)s,
                            %(node_id)s::uuid, %(expected_node_status)s,
                            %(payload)s, %(issued_at)s, %(expires_at)s,
                            %(accepted)s, %(reason)s, %(plan_version)s,
                            %(resulting_node_status)s, %(completed_at)s
                        );
                        """,
                        {
                            "command_id": command.command_id,
                            "command_type": command.command_type.value,
                            "plan_run_id": command.plan_run_id,
                            "expected_plan_version": command.expected_plan_version,
                            "node_id": command.node_id,
                            "expected_node_status": (
                                None
                                if command.expected_node_status is None
                                else command.expected_node_status.value
                            ),
                            "payload": self._jsonb(
                                self._json_snapshot(command.payload, "命令 payload")
                            ),
                            "issued_at": command.issued_at,
                            "expires_at": command.issued_at + command.ttl,
                            "accepted": result.accepted,
                            "reason": result.reason,
                            "plan_version": result.plan_version,
                            "resulting_node_status": (
                                None
                                if result.resulting_node_status is None
                                else result.resulting_node_status.value
                            ),
                            "completed_at": result.completed_at,
                        },
                    )
                connection.commit()
                return result
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("提交 PlanCommand 失败") from exc

    def get_command(self, command_id: str) -> "PlanCommandLedgerView":
        """读取首次命令请求和结果，返回与内存 Store 相同的冻结扁平视图。"""
        from src.plan_engine.commands import PlanCommandLedgerView

        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT *
                        FROM plan_commands
                        WHERE command_id = %(command_id)s;
                        """,
                        {"command_id": command_id},
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise PlanStoreInvariantError("命令账本记录不存在")
                connection.commit()
                return PlanCommandLedgerView(
                    command_id=str(row["command_id"]),
                    command_type=str(row["command_type"]),
                    plan_run_id=str(row["plan_run_id"]),
                    expected_plan_version=int(row["expected_plan_version"]),
                    node_id=(
                        None if row["node_id"] is None else str(row["node_id"])
                    ),
                    expected_node_status=row["expected_node_status"],
                    # payload 契约允许对象、数组和标量；Pydantic 视图负责递归冻结，
                    # 这里不能用 dict() 把合法 JSON 数组错误强制转换为对象。
                    payload=row["payload"],
                    issued_at=row["issued_at"],
                    expires_at=row["expires_at"],
                    accepted=bool(row["accepted"]),
                    reason=str(row["reason"]),
                    plan_version=int(row["plan_version"]),
                    resulting_node_status=row["resulting_node_status"],
                    completed_at=row["completed_at"],
                )
        except PlanStoreInvariantError:
            raise
        except psycopg.Error as exc:
            raise PlanStoreInvariantError("读取 PlanCommand 失败") from exc

    def _apply_command_sql(
        self,
        cursor: Any,
        *,
        command: Any,
        plan_run: _PlanRunRecord,
        completed_at: datetime,
    ) -> Any:
        """在已锁定 PlanRun 的事务内应用首次命令，不负责写账本行。"""
        from src.plan_engine.commands import PlanCommandResult
        from src.plan_engine.models import PlanCommandType

        if command.command_type is PlanCommandType.RESUME:
            if command.node_id is not None or command.expected_node_status is not None:
                return PlanCommandResult(
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
                return PlanCommandResult(
                    command_id=command.command_id,
                    command_type=command.command_type,
                    plan_run_id=command.plan_run_id,
                    accepted=False,
                    reason="COMMAND_STATE_NOT_APPLICABLE",
                    plan_version=plan_run.current_version,
                    completed_at=completed_at,
                )
            self._update_plan_run_state(
                cursor,
                plan_run.plan_run_id,
                PlanRunState.ACTIVE,
                completed_at,
            )
            return PlanCommandResult(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=True,
                reason="ACCEPTED",
                plan_version=plan_run.current_version,
                completed_at=completed_at,
            )

        if command.node_id is None:
            return PlanCommandResult(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="NODE_NOT_FOUND",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                completed_at=completed_at,
            )
        try:
            node = self._load_node_record(
                cursor,
                command.node_id,
                for_update=True,
            )
        except PlanStoreInvariantError:
            return PlanCommandResult(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=False,
                reason="NODE_NOT_FOUND",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                completed_at=completed_at,
            )
        if (
            node.plan_run_id != command.plan_run_id
            or node.version_number != plan_run.current_version
        ):
            return PlanCommandResult(
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
            return PlanCommandResult(
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
            if node.state is not PlanNodeState.WAITING_RECONCILIATION:
                return PlanCommandResult(
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
            if outcome not in {
                PlanNodeState.SUCCEEDED.value,
                PlanNodeState.FAILED.value,
            }:
                return PlanCommandResult(
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
            target_state = PlanNodeState(outcome)
            reconciled = self._reconcile_locked_sql(
                cursor,
                plan_run=plan_run,
                node=node,
                target_state=target_state,
                reconciliation_payload=self._json_snapshot(
                    command.payload,
                    "对账命令 payload",
                ),
                completed_at=completed_at,
            )
            return PlanCommandResult(
                command_id=command.command_id,
                command_type=command.command_type,
                plan_run_id=command.plan_run_id,
                accepted=True,
                reason="ACCEPTED",
                plan_version=plan_run.current_version,
                node_id=command.node_id,
                resulting_node_status=reconciled.state,
                completed_at=completed_at,
            )
        if command.command_type not in {
            PlanCommandType.APPROVE,
            PlanCommandType.REJECT,
        }:
            return PlanCommandResult(
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
            return PlanCommandResult(
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
        PlanStateMachine.transition_node(node.state, target_state)
        cursor.execute(
            """
            UPDATE plan_nodes
            SET state = %(state)s, updated_at = %(completed_at)s
            WHERE node_id = %(node_id)s::uuid
              AND state = %(expected_state)s;
            """,
            {
                "state": target_state.value,
                "completed_at": completed_at,
                "node_id": node.node_id,
                "expected_state": node.state.value,
            },
        )
        if cursor.rowcount != 1:
            raise PlanStoreInvariantError("人工命令节点条件更新失败")
        if target_state is PlanNodeState.FAILED:
            self._update_plan_run_state(
                cursor,
                plan_run.plan_run_id,
                PlanRunState.FAILED,
                completed_at,
            )
        return PlanCommandResult(
            command_id=command.command_id,
            command_type=command.command_type,
            plan_run_id=command.plan_run_id,
            accepted=True,
            reason="ACCEPTED",
            plan_version=plan_run.current_version,
            node_id=command.node_id,
            resulting_node_status=target_state,
            completed_at=completed_at,
        )

    def _connect(self) -> Any:
        """创建一个 READ COMMITTED 连接；调用方必须使用上下文及时提交或回滚。"""
        connection = psycopg.connect(
            **self._settings.postgres_connection_kwargs,
            row_factory=dict_row,
        )
        connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        return connection

    @staticmethod
    def _jsonb(value: Any) -> Any:
        """统一使用 psycopg Jsonb 适配器，避免字符串拼接和隐式 JSON 转换。"""
        return psycopg.types.json.Jsonb(value)

    @staticmethod
    def _json_snapshot(value: Any, field_name: str) -> Any:
        """借 NodeRun 视图校验 JSON-safe，并返回与调用方引用隔离的普通值。"""
        try:
            validated = NodeRunView(
                node_run_id="validation-only",
                plan_run_id="validation-only",
                node_id="validation-only",
                attempt_number=1,
                state=PlanNodeState.RUNNING,
                output=value,
            ).output
            return json.loads(
                json.dumps(
                    validated,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError) as exc:
            raise PlanStoreInvariantError(
                f"{field_name}必须是 JSON-safe 值"
            ) from exc

    @staticmethod
    def _capability_snapshot(
        capability: ResolvedPlanCapability,
    ) -> dict[str, Any]:
        """把可信 Capability 投影为稳定 JSON，供重启后精确重建执行约束。"""
        return {
            "node_type": capability.node_type,
            "skill_id": capability.skill_id,
            "skill_version": capability.skill_version,
            "lifecycle": sorted(stage.value for stage in capability.lifecycle),
            "risk_level": (
                None
                if capability.risk_level is None
                else capability.risk_level.value
            ),
            "max_attempt_seconds": capability.max_attempt_seconds,
            "resource_keys": list(capability.resource_keys),
            "max_concurrency": capability.max_concurrency,
        }

    @staticmethod
    def _capability_from_snapshot(value: Any) -> ResolvedPlanCapability:
        """从数据库 JSONB 重建 frozen Capability，并复核必需字段类型。"""
        snapshot = dict(value)
        risk_value = snapshot.get("risk_level")
        try:
            return ResolvedPlanCapability(
                node_type=str(snapshot["node_type"]),
                skill_id=(
                    None
                    if snapshot.get("skill_id") is None
                    else str(snapshot["skill_id"])
                ),
                skill_version=(
                    None
                    if snapshot.get("skill_version") is None
                    else str(snapshot["skill_version"])
                ),
                lifecycle=frozenset(
                    LifecycleStage(item) for item in snapshot.get("lifecycle", [])
                ),
                risk_level=(None if risk_value is None else RiskLevel(risk_value)),
                max_attempt_seconds=snapshot.get("max_attempt_seconds"),
                resource_keys=tuple(str(item) for item in snapshot["resource_keys"]),
                max_concurrency=int(snapshot["max_concurrency"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanStoreInvariantError("数据库 Capability 快照非法") from exc

    def _load_plan_run(
        self,
        cursor: Any,
        plan_run_id: str,
        *,
        for_update: bool = False,
    ) -> _PlanRunRecord:
        """读取 PlanRun 内部记录；状态修改路径可请求行锁。"""
        lock_clause = "FOR UPDATE" if for_update else ""
        cursor.execute(
            f"""
            SELECT
                plan_run_id::text AS plan_run_id, room_id, trace_id,
                run_key, current_version, state, planning_input,
                COALESCE(to_jsonb(plan_runs)->>'plan_kind', 'CARD_BATCH')
                    AS plan_kind,
                COALESCE(to_jsonb(plan_runs)->>'priority', '0')::integer
                    AS priority,
                to_jsonb(plan_runs)->>'root_plan_run_id' AS root_plan_run_id,
                to_jsonb(plan_runs)->>'parent_plan_run_id' AS parent_plan_run_id,
                to_jsonb(plan_runs)->>'trigger_event_id' AS trigger_event_id,
                reconciliation_required, reconciliation_failure,
                reconciliation_signature, reconciliation_attempt_count,
                last_reconciled_at
            FROM plan_runs
            WHERE plan_run_id::text = %(plan_run_id)s
            {lock_clause};
            """,
            {"plan_run_id": plan_run_id},
        )
        row = cursor.fetchone()
        if row is None:
            raise PlanStoreInvariantError("PlanRun 不存在")
        return _PlanRunRecord(
            plan_run_id=str(row["plan_run_id"]),
            room_id=str(row["room_id"]),
            trace_id=str(row["trace_id"]),
            run_key=str(row["run_key"]),
            current_version=int(row["current_version"]),
            state=PlanRunState(str(row["state"])),
            planning_input=dict(row["planning_input"]),
            plan_kind=PlanRunKind(str(row["plan_kind"])),
            priority=int(row["priority"]),
            root_plan_run_id=(
                None
                if row["root_plan_run_id"] is None
                else str(row["root_plan_run_id"])
            ),
            parent_plan_run_id=(
                None
                if row["parent_plan_run_id"] is None
                else str(row["parent_plan_run_id"])
            ),
            trigger_event_id=(
                None
                if row["trigger_event_id"] is None
                else str(row["trigger_event_id"])
            ),
            reconciliation_required=bool(row["reconciliation_required"]),
            reconciliation_failure=(
                None
                if row["reconciliation_failure"] is None
                else dict(row["reconciliation_failure"])
            ),
            reconciliation_signature=(
                None
                if row["reconciliation_signature"] is None
                else str(row["reconciliation_signature"])
            ),
            reconciliation_attempt_count=int(
                row["reconciliation_attempt_count"]
            ),
            last_reconciled_at=row["last_reconciled_at"],
        )

    def _load_plan_version(
        self,
        cursor: Any,
        plan_run_id: str,
        version_number: int,
    ) -> _PlanVersionRecord:
        """读取不可变 PlanVersion 内部记录。"""
        cursor.execute(
            """
            SELECT
                plan_run_id::text AS plan_run_id, version_number,
                provider_id, provider_version, proposal,
                COALESCE(to_jsonb(plan_versions)->>'change_reason', 'INITIAL')
                    AS change_reason,
                COALESCE(
                    to_jsonb(plan_versions)->'source_event_ids',
                    '[]'::jsonb
                ) AS source_event_ids
            FROM plan_versions
            WHERE plan_run_id::text = %(plan_run_id)s
              AND version_number = %(version_number)s;
            """,
            {
                "plan_run_id": plan_run_id,
                "version_number": version_number,
            },
        )
        row = cursor.fetchone()
        if row is None:
            raise PlanStoreInvariantError("PlanVersion 不存在")
        return _PlanVersionRecord(
            plan_run_id=str(row["plan_run_id"]),
            version_number=int(row["version_number"]),
            provider_id=str(row["provider_id"]),
            provider_version=str(row["provider_version"]),
            proposal=dict(row["proposal"]),
            change_reason=str(row["change_reason"]),
            source_event_ids=tuple(str(item) for item in row["source_event_ids"]),
        )

    def _load_node_records(
        self,
        cursor: Any,
        plan_run_id: str,
        version_number: int,
    ) -> tuple[_PlanNodeRecord, ...]:
        """批量读取节点并按 dependency_order 重建逻辑依赖键。"""
        cursor.execute(
            """
            SELECT *
            FROM plan_nodes
            WHERE plan_run_id::text = %(plan_run_id)s
              AND version_number = %(version_number)s
            ORDER BY node_order;
            """,
            {
                "plan_run_id": plan_run_id,
                "version_number": version_number,
            },
        )
        rows = tuple(cursor.fetchall())
        if not rows:
            return ()
        cursor.execute(
            """
            SELECT
                d.node_id::text AS node_id,
                dependency.logical_key,
                d.dependency_order
            FROM plan_node_dependencies AS d
            JOIN plan_nodes AS dependency
              ON dependency.node_id = d.dependency_node_id
            WHERE d.plan_run_id::text = %(plan_run_id)s
              AND dependency.version_number = %(version_number)s
            ORDER BY d.node_id, d.dependency_order;
            """,
            {
                "plan_run_id": plan_run_id,
                "version_number": version_number,
            },
        )
        dependencies: dict[str, list[str]] = {}
        for dependency in cursor.fetchall():
            dependencies.setdefault(str(dependency["node_id"]), []).append(
                str(dependency["logical_key"])
            )
        return tuple(
            self._node_record(
                row,
                tuple(dependencies.get(str(row["node_id"]), ())),
            )
            for row in rows
        )

    def _load_node_record(
        self,
        cursor: Any,
        node_id: str,
        *,
        for_update: bool = False,
    ) -> _PlanNodeRecord:
        """读取一个节点及其有序依赖；修改路径先锁节点关系行。"""
        lock_clause = "FOR UPDATE" if for_update else ""
        cursor.execute(
            f"""
            SELECT *
            FROM plan_nodes
            WHERE node_id::text = %(node_id)s
            {lock_clause};
            """,
            {"node_id": node_id},
        )
        row = cursor.fetchone()
        if row is None:
            raise PlanStoreInvariantError("PlanNode 不存在")
        dependencies = self._load_dependencies_for_node(cursor, node_id)
        return self._node_record(row, dependencies)

    def _load_dependencies_for_node(
        self,
        cursor: Any,
        node_id: str,
    ) -> tuple[str, ...]:
        """读取节点声明的上游 logical_key，保持候选中的顺序。"""
        cursor.execute(
            """
            SELECT dependency.logical_key
            FROM plan_node_dependencies AS d
            JOIN plan_nodes AS dependency
              ON dependency.node_id = d.dependency_node_id
            WHERE d.node_id::text = %(node_id)s
            ORDER BY d.dependency_order;
            """,
            {"node_id": node_id},
        )
        return tuple(str(row["logical_key"]) for row in cursor.fetchall())

    def _node_record(
        self,
        row: Any,
        dependencies: tuple[str, ...],
    ) -> _PlanNodeRecord:
        """把关系行和依赖边组合为 Store 内部不可变节点记录。"""
        return _PlanNodeRecord(
            node_id=str(row["node_id"]),
            plan_run_id=str(row["plan_run_id"]),
            version_number=int(row["version_number"]),
            logical_key=str(row["logical_key"]),
            node_kind=PlanNodeKind(str(row["node_kind"])),
            state=PlanNodeState(str(row["state"])),
            skill_id=(None if row["skill_id"] is None else str(row["skill_id"])),
            input_bindings=dict(row["input_bindings"]),
            depends_on=dependencies,
            capability=self._capability_from_snapshot(row["capability"]),
            retry_at=row["retry_at"],
            deadline_at=row["deadline_at"],
        )

    def _load_node_run(
        self,
        cursor: Any,
        node_run_id: str,
        *,
        for_update: bool = False,
    ) -> _NodeRunRecord:
        """读取一个 NodeRun；条件更新前可锁住该历史事实行。"""
        lock_clause = "FOR UPDATE" if for_update else ""
        cursor.execute(
            f"""
            SELECT *
            FROM node_runs
            WHERE node_run_id::text = %(node_run_id)s
            {lock_clause};
            """,
            {"node_run_id": node_run_id},
        )
        row = cursor.fetchone()
        if row is None:
            raise PlanStoreInvariantError("NodeRun 不存在")
        return self._node_run_record(row)

    @staticmethod
    def _node_run_record(row: Any) -> _NodeRunRecord:
        """把数据库 NodeRun 行转换为不可变执行事实。"""
        raw_input = row["input_snapshot"]
        return _NodeRunRecord(
            node_run_id=str(row["node_run_id"]),
            plan_run_id=str(row["plan_run_id"]),
            node_id=str(row["node_id"]),
            attempt_number=int(row["attempt_number"]),
            claim_version=int(row["claim_version"]),
            state=PlanNodeState(str(row["state"])),
            worker_id=str(row["lease_owner"]),
            lease_until=row["lease_until"],
            input_snapshot=dict(raw_input),
            output=row["output"],
            resource_keys=tuple(str(item) for item in row["resource_keys"]),
            node_type=str(row["node_type"]),
            skill_id=(None if row["skill_id"] is None else str(row["skill_id"])),
            skill_version=(
                None
                if row["skill_version"] is None
                else str(row["skill_version"])
            ),
            input_fingerprint=row["input_fingerprint"],
            deadline_at=row["deadline_at"],
        )

    @staticmethod
    def _try_lock_resource_keys(
        cursor: Any,
        resource_keys: tuple[str, ...],
    ) -> bool:
        """按排序顺序尝试事务级资源锁，避免多键请求形成锁顺序反转。"""
        for resource_key in sorted(set(resource_keys)):
            cursor.execute(
                """
                SELECT pg_try_advisory_xact_lock(
                    hashtextextended(%(resource_key)s, 0)
                ) AS acquired;
                """,
                {"resource_key": f"plan-resource:{resource_key}"},
            )
            if not bool(cursor.fetchone()["acquired"]):
                return False
        return True

    @staticmethod
    def _resources_are_held(
        cursor: Any,
        *,
        resource_keys: tuple[str, ...],
        now: datetime,
        excluded_node_id: str | None = None,
    ) -> bool:
        """检查其他节点最新且未过期的 RUNNING NodeRun 是否持有任一资源。"""
        if not resource_keys:
            return False
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM node_runs AS active
                WHERE active.state = %(running)s
                  AND active.lease_until > %(now)s
                  AND active.resource_keys && %(resource_keys)s::text[]
                  AND (
                      %(excluded_node_id)s::text IS NULL
                      OR active.node_id::text <> %(excluded_node_id)s
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM node_runs AS newer
                      WHERE newer.node_id = active.node_id
                        AND newer.claim_version > active.claim_version
                  )
            ) AS held;
            """,
            {
                "running": PlanNodeState.RUNNING.value,
                "now": now,
                "resource_keys": list(resource_keys),
                "excluded_node_id": excluded_node_id,
            },
        )
        return bool(cursor.fetchone()["held"])

    @staticmethod
    def _is_current_claim(cursor: Any, record: _NodeRunRecord) -> bool:
        """以数据库最大 claim_version 判断记录是否仍是节点当前 fencing。"""
        cursor.execute(
            """
            SELECT max(claim_version) AS latest_claim_version
            FROM node_runs
            WHERE node_id = %(node_id)s::uuid;
            """,
            {"node_id": record.node_id},
        )
        row = cursor.fetchone()
        return int(row["latest_claim_version"]) == record.claim_version

    def _assert_live_current_claim(
        self,
        cursor: Any,
        *,
        record: _NodeRunRecord,
        worker_id: str,
        claim_version: int,
        now: datetime,
        operation: str,
    ) -> None:
        """集中校验 Worker、fencing、最新身份、状态和 lease，失败前不写任何行。"""
        if record.worker_id != worker_id or record.claim_version != claim_version:
            raise PlanStoreInvariantError(
                f"{operation}的 worker 或 fencing token 不匹配"
            )
        if not self._is_current_claim(cursor, record):
            raise PlanStoreInvariantError(
                f"旧 fencing token 永久不能执行{operation}"
            )
        if record.state is not PlanNodeState.RUNNING:
            raise PlanStoreInvariantError(
                f"只有 RUNNING NodeRun 可以执行{operation}"
            )
        if now >= record.lease_until:
            raise PlanStoreInvariantError(f"租约已过期，禁止迟到{operation}")

    def _ready_satisfied_dependents_sql(
        self,
        cursor: Any,
        *,
        plan_run_id: str,
        now: datetime,
    ) -> None:
        """开放依赖全部成功的 PENDING 节点，并在全部成功时闭合 PlanRun。"""
        plan_run = self._load_plan_run(cursor, plan_run_id, for_update=True)
        cursor.execute(
            """
            UPDATE plan_nodes AS candidate
            SET state = %(ready)s, updated_at = %(now)s
            WHERE candidate.plan_run_id = %(plan_run_id)s::uuid
              AND candidate.version_number = %(version_number)s
              AND candidate.state = %(pending)s
              AND NOT EXISTS (
                  SELECT 1
                  FROM plan_node_dependencies AS d
                  JOIN plan_nodes AS dependency
                    ON dependency.node_id = d.dependency_node_id
                  WHERE d.node_id = candidate.node_id
                    AND dependency.state <> %(succeeded)s
              );
            """,
            {
                "ready": PlanNodeState.READY.value,
                "now": now,
                "plan_run_id": plan_run_id,
                "version_number": plan_run.current_version,
                "pending": PlanNodeState.PENDING.value,
                "succeeded": PlanNodeState.SUCCEEDED.value,
            },
        )
        cursor.execute(
            """
            SELECT bool_and(state = %(succeeded)s) AS all_succeeded
            FROM plan_nodes
            WHERE plan_run_id = %(plan_run_id)s::uuid
              AND version_number = %(version_number)s;
            """,
            {
                "succeeded": PlanNodeState.SUCCEEDED.value,
                "plan_run_id": plan_run_id,
                "version_number": plan_run.current_version,
            },
        )
        if bool(cursor.fetchone()["all_succeeded"]):
            self._update_plan_run_state(
                cursor,
                plan_run_id,
                PlanRunState.SUCCEEDED,
                now,
            )

    @staticmethod
    def _update_plan_run_state(
        cursor: Any,
        plan_run_id: str,
        state: PlanRunState,
        now: datetime,
    ) -> None:
        """更新已经过上层状态机验证的 PlanRun 聚合状态。"""
        cursor.execute(
            """
            UPDATE plan_runs
            SET state = %(state)s, updated_at = %(now)s
            WHERE plan_run_id = %(plan_run_id)s::uuid;
            """,
            {
                "state": state.value,
                "now": now,
                "plan_run_id": plan_run_id,
            },
        )
        if cursor.rowcount != 1:
            raise PlanStoreInvariantError("PlanRun 状态更新失败")

    def _reconcile_locked_sql(
        self,
        cursor: Any,
        *,
        plan_run: _PlanRunRecord,
        node: _PlanNodeRecord,
        target_state: PlanNodeState,
        reconciliation_payload: dict[str, Any],
        completed_at: datetime,
    ) -> _PlanNodeRecord:
        """在调用方已锁 PlanRun/Node 的事务内闭合最新对账 NodeRun。"""
        if target_state not in {PlanNodeState.SUCCEEDED, PlanNodeState.FAILED}:
            raise PlanStoreInvariantError("对账目标只能是 SUCCEEDED 或 FAILED")
        if node.state is not PlanNodeState.WAITING_RECONCILIATION:
            raise PlanStoreInvariantError(
                "只有 WAITING_RECONCILIATION 节点可以对账"
            )
        cursor.execute(
            """
            SELECT *
            FROM node_runs
            WHERE node_id = %(node_id)s::uuid
            ORDER BY claim_version DESC
            LIMIT 1
            FOR UPDATE;
            """,
            {"node_id": node.node_id},
        )
        row = cursor.fetchone()
        if row is None:
            raise PlanStoreInvariantError("对账节点缺少 NodeRun")
        node_run = self._node_run_record(row)
        if node_run.state is not PlanNodeState.WAITING_RECONCILIATION:
            raise PlanStoreInvariantError("对账 NodeRun 状态不匹配")
        output = {
            "worker_output": node_run.output,
            "reconciliation": reconciliation_payload,
        }
        PlanStateMachine.transition_node(node_run.state, target_state)
        cursor.execute(
            """
            UPDATE node_runs
            SET state = %(state)s, output = %(output)s,
                updated_at = %(completed_at)s,
                completed_at = %(completed_at)s
            WHERE node_run_id = %(node_run_id)s::uuid
              AND state = %(waiting)s;
            """,
            {
                "state": target_state.value,
                "output": self._jsonb(output),
                "completed_at": completed_at,
                "node_run_id": node_run.node_run_id,
                "waiting": PlanNodeState.WAITING_RECONCILIATION.value,
            },
        )
        if cursor.rowcount != 1:
            raise PlanStoreInvariantError("对账 NodeRun 条件更新失败")
        cursor.execute(
            """
            UPDATE plan_nodes
            SET state = %(state)s, updated_at = %(completed_at)s
            WHERE node_id = %(node_id)s::uuid
              AND state = %(waiting)s;
            """,
            {
                "state": target_state.value,
                "completed_at": completed_at,
                "node_id": node.node_id,
                "waiting": PlanNodeState.WAITING_RECONCILIATION.value,
            },
        )
        if cursor.rowcount != 1:
            raise PlanStoreInvariantError("对账 PlanNode 条件更新失败")
        if target_state is PlanNodeState.FAILED:
            self._update_plan_run_state(
                cursor,
                plan_run.plan_run_id,
                PlanRunState.FAILED,
                completed_at,
            )
        else:
            self._ready_satisfied_dependents_sql(
                cursor,
                plan_run_id=plan_run.plan_run_id,
                now=completed_at,
            )
        return self._load_node_record(cursor, node.node_id)

    @staticmethod
    def _load_command_result(cursor: Any, command_id: str) -> Any | None:
        """读取命令首次结果；不存在时返回 None 供首次提交继续。"""
        from src.plan_engine.commands import PlanCommandResult

        cursor.execute(
            """
            SELECT *
            FROM plan_commands
            WHERE command_id = %(command_id)s;
            """,
            {"command_id": command_id},
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return PlanCommandResult(
            command_id=str(row["command_id"]),
            command_type=str(row["command_type"]),
            plan_run_id=str(row["plan_run_id"]),
            accepted=bool(row["accepted"]),
            reason=str(row["reason"]),
            plan_version=int(row["plan_version"]),
            node_id=(None if row["node_id"] is None else str(row["node_id"])),
            resulting_node_status=row["resulting_node_status"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        """规范数据库比较时间为 UTC，并拒绝 naive datetime。"""
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise PlanStoreInvariantError(f"{field_name}必须包含时区")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _validate_version_number(value: Any) -> int:
        """版本查询只接受精确正整数，拒绝 bool 与 float 等值命中。"""
        if type(value) is not int or value < 1:
            raise PlanStoreInvariantError(
                "PlanVersion 版本必须是大于等于 1 的精确 int"
            )
        return value

    @staticmethod
    def _plan_run_view(record: _PlanRunRecord) -> PlanRunView:
        """把关系记录投影为不可变、JSON-safe PlanRun 视图。"""
        return PlanRunView(
            plan_run_id=record.plan_run_id,
            room_id=record.room_id,
            trace_id=record.trace_id,
            run_key=record.run_key,
            current_version=record.current_version,
            state=record.state,
            planning_input=record.planning_input,
            plan_kind=record.plan_kind,
            priority=record.priority,
            root_plan_run_id=record.root_plan_run_id,
            parent_plan_run_id=record.parent_plan_run_id,
            trigger_event_id=record.trigger_event_id,
            reconciliation_required=record.reconciliation_required,
            reconciliation_failure=record.reconciliation_failure,
            reconciliation_signature=record.reconciliation_signature,
            reconciliation_attempt_count=record.reconciliation_attempt_count,
            last_reconciled_at=record.last_reconciled_at,
        )

    @staticmethod
    def _node_view(record: _PlanNodeRecord) -> PlanNodeView:
        """把关系记录投影为不泄漏 capability 内部对象的节点视图。"""
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
        """把 NodeRun 关系事实投影为带 lease 与 fencing 的冻结视图。"""
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
            input_fingerprint=record.input_fingerprint,
            deadline_at=record.deadline_at,
        )


def initialize_plan_engine_schema(settings: Any) -> None:
    """执行 Phase 12A DDL，供迁移、真实 PostgreSQL 测试和本地装配显式调用。"""
    project_root = Path(__file__).resolve().parents[2]
    sql_path = project_root / "docker" / "init_phase12a_plan_engine.sql"
    sql = sql_path.read_text(encoding="utf-8")
    try:
        with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()
    except psycopg.Error as exc:
        raise PlanStoreInvariantError("初始化 PlanEngine Schema 失败") from exc
