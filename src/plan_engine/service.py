"""Phase 12A PlanEngine 生命周期与播前手卡 Graph 服务入口。

生命周期服务不创建隐藏后台线程；CardBatch 服务也不拥有第二份计划状态，只组合
固定 Provider、可信 Capability、PlanStore 和现有 Worker。所有执行与恢复事实仍以
PlanStore 为权威，Graph 只接收终态结果和最小引用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.plan_engine.capabilities import PlanCapabilityProfile, ResolvedPlanCapability
from src.plan_engine.models import (
    CandidatePlanNode,
    CandidatePlanProposal,
    CardBatchPlanningInput,
    InputBindingKind,
    NodeRunView,
    PlanNodeKind,
    PlanNodeState,
    PlanRunState,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.reconciliation import RECONCILIATION_INTERVAL_SECONDS
from src.plan_engine.store import MaterializedPlan, PlanStore, PlanStoreInvariantError
from src.plan_engine.worker import WorkerRunResult
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skills.product_card_generator import ProductCard


class LifecycleReconciler(Protocol):
    """生命周期服务依赖的最小对账接口。"""

    def reconcile_startup(self) -> tuple[Any, ...]:
        """执行一次启动扫描。"""

    def reconcile_active_plans_once(self) -> tuple[Any, ...]:
        """执行一次周期扫描。"""


class PlanEngineService:
    """向应用装配层暴露显式启动和周期入口。"""

    def __init__(self, *, reconciler: LifecycleReconciler) -> None:
        """冻结唯一 Reconciliation Service；不复制 Store 或 checkpoint。"""
        self._reconciler = reconciler
        self.reconciliation_interval_seconds = RECONCILIATION_INTERVAL_SECONDS

    def startup(self) -> tuple[Any, ...]:
        """服务启动后立即执行一次幂等对账扫描。"""
        return self._reconciler.reconcile_startup()

    def run_reconciliation_tick(self) -> tuple[Any, ...]:
        """由外部调度器按固定周期触发一次扫描。"""
        return self._reconciler.reconcile_active_plans_once()


class CardBatchPlanRef(BaseModel):
    """Graph 在创建或恢复 PlanRun 后持有的最小不可变引用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_run_id: str = Field(..., min_length=1)
    plan_version: int = Field(..., ge=1, strict=True)


class CardBatchExecutionResult(BaseModel):
    """手卡 DAG 的终态结果。

    Graph 只能接收成功或失败终态，不能把 ACTIVE/FROZEN 伪装成已经完成。卡片快照
    会先通过 ProductCard 契约，再借 NodeRunView 递归冻结为严格 JSON，避免调用方在
    PlanStore 提交后修改返回容器。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_run_id: str = Field(..., min_length=1)
    plan_version: int = Field(..., ge=1, strict=True)
    status: PlanRunState
    cards_snapshot: tuple[Any, ...] = Field(default_factory=tuple)

    @field_validator("cards_snapshot", mode="after")
    @classmethod
    def _validate_and_freeze_cards(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        """把每张手卡规范为完整 JSON 快照，并递归冻结所有嵌套列表。"""
        normalized = [
            ProductCard.model_validate(card).model_dump(mode="json") for card in value
        ]
        frozen = NodeRunView(
            node_run_id="card-batch-result",
            plan_run_id="card-batch-result",
            node_id="collect-card-results",
            attempt_number=1,
            state=PlanNodeState.SUCCEEDED,
            output=normalized,
        ).output
        return tuple(frozen or ())

    @model_validator(mode="after")
    def _validate_terminal_shape(self) -> "CardBatchExecutionResult":
        """成功必须有卡片，失败不得捎带可能被误用的部分结果。"""
        if self.status not in {PlanRunState.SUCCEEDED, PlanRunState.FAILED}:
            raise ValueError("CardBatchExecutionResult 只允许 SUCCEEDED 或 FAILED")
        if self.status is PlanRunState.SUCCEEDED and not self.cards_snapshot:
            raise ValueError("成功手卡计划必须包含 cards_snapshot")
        if self.status is PlanRunState.FAILED and self.cards_snapshot:
            raise ValueError("失败手卡计划不得作为成功结果返回部分卡片")
        return self


class CardBatchPlanService(Protocol):
    """同步播前 Graph 使用的最小 DAG 服务边界。"""

    def create_or_resume(self, request: CardBatchPlanningInput) -> CardBatchPlanRef:
        """基于冻结输入创建或幂等恢复 PlanRun。"""

    def drive_to_terminal(self, plan_run_id: str) -> CardBatchExecutionResult:
        """驱动已有计划到终态；不得在失败时调用 Legacy。"""


class _SyncProposalProvider(Protocol):
    """Phase 12A 固定候选 Provider 的同步最小接口。"""

    def propose_sync(self, request: CardBatchPlanningInput) -> CandidatePlanProposal:
        """返回经过类型化模型校验的固定候选 DAG。"""


class _SyncPlanWorker(Protocol):
    """CardBatch 服务依赖的同步 Worker 最小接口。"""

    def run_once(self, plan_run_id: str, **kwargs: Any) -> WorkerRunResult:
        """执行一次可 claim 节点批次并先提交 PlanStore。"""


class DefaultCardBatchPlanService:
    """组合现有 Provider、Capability Profile、PlanStore 与 Worker 的同步服务。

    本类不维护第二份执行状态，也不隐藏重试。每次 ``drive_to_terminal`` 都先读取
    PlanStore；已经成功或失败的计划直接复用终态，活动计划才调用 Worker。若当前没有
    可 claim 节点，说明计划在等待 retry/人工处理，Graph 调用明确失败而不是忙等。
    """

    def __init__(
        self,
        *,
        store: PlanStore,
        worker: _SyncPlanWorker,
        proposal_provider: _SyncProposalProvider | None = None,
        capability_profile: PlanCapabilityProfile | None = None,
        max_worker_cycles: int = 16,
    ) -> None:
        """冻结全部协作者和有界驱动次数，防止异常 DAG 形成无限循环。"""
        if type(max_worker_cycles) is not int or max_worker_cycles < 1:
            raise ValueError("max_worker_cycles 必须是正整数")
        self._store = store
        self._worker = worker
        self._proposal_provider = (
            proposal_provider or CanonicalCardBatchProposalProvider()
        )
        self._capability_profile = capability_profile or PlanCapabilityProfile.default(
            catalog=get_default_skill_catalog()
        )
        self._max_worker_cycles = max_worker_cycles

    def create_or_resume(self, request: CardBatchPlanningInput) -> CardBatchPlanRef:
        """物化固定候选及可信能力事实，再委托唯一 PlanStore 幂等创建。"""
        if not isinstance(request, CardBatchPlanningInput):
            raise TypeError("request 必须是 CardBatchPlanningInput")
        frozen_request = CardBatchPlanningInput.model_validate(
            request.model_dump(mode="json")
        )
        proposal = self._proposal_provider.propose_sync(frozen_request)
        capabilities = self._resolve_capabilities(frozen_request, proposal.nodes)
        plan_run = self._store.create_or_resume(
            MaterializedPlan(
                planning_input=frozen_request,
                proposal=proposal,
                capabilities_by_logical_key=capabilities,
            )
        )
        return CardBatchPlanRef(
            plan_run_id=plan_run.plan_run_id,
            plan_version=plan_run.current_version,
        )

    def drive_to_terminal(self, plan_run_id: str) -> CardBatchExecutionResult:
        """有界驱动固定 DAG；无可运行节点时显式失败，留待调度器后续恢复。"""
        for _ in range(self._max_worker_cycles):
            plan_run = self._store.get_plan_run(plan_run_id)
            if plan_run.state in {PlanRunState.SUCCEEDED, PlanRunState.FAILED}:
                return self._terminal_result(plan_run_id)
            batch = self._worker.run_once(plan_run_id)
            if batch.claimed == 0:
                current = self._store.get_plan_run(plan_run_id)
                raise PlanStoreInvariantError(
                    "手卡计划尚未到达终态且当前没有可执行节点: "
                    f"{current.state.value}"
                )
        raise PlanStoreInvariantError("手卡计划超过有界 Worker 驱动次数")

    def _resolve_capabilities(
        self,
        request: CardBatchPlanningInput,
        nodes: Sequence[CandidatePlanNode],
    ) -> dict[str, ResolvedPlanCapability]:
        """只从可信 Profile 补全版本、风险、资源键和并发事实。"""
        capabilities: dict[str, ResolvedPlanCapability] = {}
        for node in nodes:
            if node.node_kind is PlanNodeKind.CONTROL:
                control_types = {
                    "prepare-card-batch": PlanCapabilityProfile.PREPARE_CARD_BATCH,
                    "collect-card-results": PlanCapabilityProfile.COLLECT_CARD_RESULTS,
                }
                control_type = control_types.get(node.logical_key)
                if control_type is None:
                    raise PlanStoreInvariantError("候选包含未知控制节点")
                capability = self._capability_profile.resolve_control_node(
                    control_type=control_type
                )
            else:
                binding = node.input_bindings.get("product")
                if (
                    binding is None
                    or binding.kind is not InputBindingKind.PLAN_INPUT
                    or len(binding.path) != 2
                    or binding.path[0] != "products_by_id"
                    or not isinstance(binding.path[1], str)
                ):
                    raise PlanStoreInvariantError("手卡候选缺少受控商品输入绑定")
                product_id = binding.path[1]
                if product_id not in request.products_by_id:
                    # 候选模型只能验证路径语法；真正的输入闭包必须在 PlanRun 创建前
                    # 与本次冻结快照交叉校验，不能把无效计划持久化后交给 Worker 探测。
                    raise PlanStoreInvariantError("手卡候选引用了冻结输入外的商品")
                capability = self._capability_profile.resolve_skill_node(
                    skill_id=node.skill_id,
                    product_id=product_id,
                    room_id=request.room_id,
                )
            capabilities[node.logical_key] = capability
        return capabilities

    def _terminal_result(self, plan_run_id: str) -> CardBatchExecutionResult:
        """从权威 COLLECT NodeRun 读取终态，不从 Graph checkpoint 推断卡片。"""
        plan_run = self._store.get_plan_run(plan_run_id)
        if plan_run.state is PlanRunState.FAILED:
            return CardBatchExecutionResult(
                plan_run_id=plan_run.plan_run_id,
                plan_version=plan_run.current_version,
                status=plan_run.state,
            )
        if plan_run.state is not PlanRunState.SUCCEEDED:
            raise PlanStoreInvariantError("手卡计划尚未到达终态")

        collect_node = next(
            (
                node
                for node in self._store.list_nodes(plan_run_id)
                if node.logical_key == "collect-card-results"
            ),
            None,
        )
        if collect_node is None:
            raise PlanStoreInvariantError("成功计划缺少 COLLECT_CARD_RESULTS 节点")
        successful_runs = [
            node_run
            for node_run in self._store.list_node_runs(plan_run_id, collect_node.node_id)
            if node_run.state is PlanNodeState.SUCCEEDED
        ]
        if not successful_runs:
            raise PlanStoreInvariantError("成功计划缺少 COLLECT_CARD_RESULTS NodeRun")
        output = successful_runs[-1].output
        raw_cards = output.get("cards") if isinstance(output, Mapping) else None
        if not isinstance(raw_cards, Sequence) or isinstance(
            raw_cards,
            (str, bytes, bytearray),
        ):
            raise PlanStoreInvariantError("COLLECT_CARD_RESULTS 输出缺少 cards")

        cards: list[Any] = []
        for item in raw_cards:
            # SkillExecutor 的标准输出使用 {"card": snapshot} 包络；控制节点只聚合
            # 上游输出，因此在 Graph 边界统一拆包。没有该包络时仍让 ProductCard
            # 校验决定是否是合法历史快照，便于兼容 Task 5 的既有测试事实。
            card = item.get("card") if isinstance(item, Mapping) and "card" in item else item
            cards.append(ProductCard.model_validate(card).model_dump(mode="json"))
        return CardBatchExecutionResult(
            plan_run_id=plan_run.plan_run_id,
            plan_version=plan_run.current_version,
            status=plan_run.state,
            cards_snapshot=tuple(cards),
        )


__all__ = [
    "CardBatchExecutionResult",
    "CardBatchPlanRef",
    "CardBatchPlanService",
    "DefaultCardBatchPlanService",
    "PlanEngineService",
]
