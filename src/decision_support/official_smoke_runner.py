"""Phase 16 正式真实模型 smoke 的隔离 Runner 组合边界。

本模块只装配冻结数据集、六角色证据投影、共享 BoundedSpecialistRunner 和正式账本；它
不创建生产 Proposal、Outcome、OperatorDecision 或经营命令。真实网络端口只能由唯一
CLI 的 ``--execute`` 分支显式注入，默认路径始终保持离线。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from src.decision_support.evidence import EvidenceBundleSnapshot, ProductInventoryPayload
from src.decision_support.models import Incident, LiveSessionWorkspace, WorkspaceView
from src.decision_support.multi_agent import (
    ValidatedConflictAnalysisPayload,
    build_phase16_smoke_evidence_analyst_profile,
    build_phase16_smoke_evidence_planner_profile,
    validate_conflict_analysis_result,
    validate_live_decision_planner_result,
)
from src.decision_support.multi_agent_evaluation import (
    Phase16EvaluationDataset,
    _assemble_bundle,
)
from src.decision_support.official_smoke_evidence import (
    PHASE16_OFFICIAL_SMOKE_RUN_ID,
    Phase16OfficialPriceEvidence,
    Phase16OfficialSmokeEvidenceManifest,
    Phase16OfficialSmokePreflight,
    Phase16OfficialSmokeStatus,
    validate_phase16_official_smoke_receipt,
)
from src.decision_support.official_smoke_ledger import (
    Phase16OfficialSmokeCaseOutcomeStatus,
    Phase16OfficialSmokeDispatchStage,
    Phase16OfficialSmokeLedgerError,
    Phase16OfficialSmokeValidationVerdict,
)
from src.decision_support.store import derive_automatic_escalation_codes
from src.specialist_runtime.budget import BudgetInvariantError
from src.specialist_runtime.evidence import (
    EvidenceResolverRegistry,
    ResolvedEvidence,
)
from src.specialist_runtime.model_port import AgentModelPort, ModelFailure, ModelSuccess
from src.specialist_runtime.models import (
    AgentResult,
    AgentTask,
    EvidenceKind,
    EvidenceRef,
    SpecialistTaskKind,
    _plain_json,
    canonical_json_sha256,
)
from src.specialist_runtime.profiles import SpecialistProfile
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
from src.specialist_runtime.runner import BoundedSpecialistRunner


class _ProjectionEvidenceLoader:
    """仅从一例已冻结的六角色组件读取证据，拒绝任意 Store 扫描或动态查询。"""

    def __init__(self, facts: dict[str, ResolvedEvidence]) -> None:
        self._facts = dict(facts)

    def load(self, evidence_id: str) -> ResolvedEvidence | None:
        """按精确 ID 返回同一份不可变快照；未知 ID 不产生容错或回退。"""

        return self._facts.get(evidence_id)


@dataclass(frozen=True)
class Phase16OfficialSmokeCaseProjection:
    """单个固定 smoke case 的模型安全投影，不保存标签、split 或期望路由。"""

    case_id: str
    case_digest: str
    analyst_task: AgentTask
    planner_profile_id: str
    planner_profile_version: str
    evidence_refs: tuple[EvidenceRef, ...]
    evidence_registry: EvidenceResolverRegistry
    trusted_anchor_id: str
    trigger_codes: tuple[Any, ...]
    available_backup_product_ids: frozenset[str]
    proposal_eligible: bool
    valid_until: datetime
    evidence_bundle_digest: str

    def build_planner_task(
        self,
        analysis: ValidatedConflictAnalysisPayload,
    ) -> AgentTask:
        """仅将已纯验证的 Analyst 载荷与同一 Bundle 摘要交给 Planner。

        六角色原始 payload 已由共享 Runner 的窄只读 Resolver 放进 ``resolved_evidence``；
        这里绝不重复嵌入完整 Bundle，否则同一事实会消耗两遍输入 token 并挤占固定 4000
        token 的正式 Smoke 预算。
        """

        planner_profile = build_phase16_smoke_evidence_planner_profile()
        key = _opaque_case_key(self.case_id)
        return AgentTask(
            task_id=f"formal-planner-{key}",
            task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING,
            profile_id=planner_profile.profile_id,
            profile_version=planner_profile.profile_version,
            room_id=self.analyst_task.room_id,
            trace_id=self.analyst_task.trace_id,
            objective="Generate one to three bounded options for formal human-review smoke.",
            input_snapshot={
                "analysis": analysis.as_model_input(),
                "evidence_bundle_digest": self.evidence_bundle_digest,
            },
            initial_evidence_refs=self.evidence_refs,
        )


def _opaque_case_key(case_id: str) -> str:
    """把账本 case 身份单向映射成临时运行键，避免 split/kind 出现在模型可见字段。"""

    return sha256(case_id.encode("utf-8")).hexdigest()[:24]


def _find_case(dataset: Phase16EvaluationDataset, case_id: str):
    """只接受 Manifest 已冻结的十个 smoke slot，不能把任意评估 case 混进正式运行。"""

    if case_id not in dataset.manifest.smoke_eligible_case_ids:
        raise ValueError("formal smoke case is not a frozen eligible slot")
    matches = [case for case in dataset.cases if case.case_id == case_id]
    if len(matches) != 1:
        raise ValueError("formal smoke case identity is unavailable")
    return matches[0]


def _synthetic_live_parents(*, case_id: str, now: datetime) -> tuple[LiveSessionWorkspace, Incident]:
    """构造仅供六角色 Assembler 使用的临时 LIVE 父事实，不写入生产 Decision Support Store。"""

    key = _opaque_case_key(case_id)
    workspace = LiveSessionWorkspace(
        live_session_id=f"formal-session-{key}",
        run_key=f"formal-run-{key}",
        room_id=f"formal-room-{key}",
        trace_id=f"formal-trace-{key}",
        anchor_id="anchor-phase16-official-smoke",
        root_plan_run_id=f"formal-root-{key}",
        event_inbox_scope_id=f"formal-inbox-{key}",
        decision_trace_scope_id=f"formal-trace-scope-{key}",
        replay_scope_id=f"formal-replay-{key}",
        evaluation_scope_id=f"formal-evaluation-{key}",
        view=WorkspaceView.LIVE,
    )
    incident = Incident(
        incident_id=f"formal-incident-{key}",
        live_session_id=workspace.live_session_id,
        idempotency_key=f"formal-incident-{key}",
        incident_type="SOLD_OUT_COMPOSITE",
        source_ref_ids=(f"event-{key}",),
        snapshot={"product_id": "p001", "expected_version": 2},
        created_at=now,
    )
    return workspace, incident


def _projection_evidence_registry(snapshot: EvidenceBundleSnapshot) -> EvidenceResolverRegistry:
    """将已由六角色 Assembler 验证的组件投影为共享 Runner 所需的窄只读 Registry。"""

    by_kind: dict[EvidenceKind, dict[str, ResolvedEvidence]] = {
        kind: {} for kind in EvidenceKind
    }
    for component in snapshot.components:
        reference = component.reference
        by_kind[reference.kind][reference.evidence_id] = ResolvedEvidence(
            kind=reference.kind,
            evidence_id=reference.evidence_id,
            source_version=reference.source_version,
            digest=reference.digest,
            anchor_id=reference.anchor_id,
            room_id=reference.room_id,
            # 共享 Runner 只能看到已在 Bundle 内冻结的结构化 payload；它拿不到
            # Assembler、Workspace、Incident、Store 或任意按名称搜索的能力。
            payload=component.payload.model_dump(mode="json"),
        )
    return EvidenceResolverRegistry(
        {kind: _ProjectionEvidenceLoader(facts) for kind, facts in by_kind.items()}
    )


def build_phase16_official_smoke_case_projection(
    *,
    dataset: Phase16EvaluationDataset,
    case_id: str,
    now: datetime | None = None,
) -> Phase16OfficialSmokeCaseProjection:
    """从冻结 case 重建正式 Analyst 任务与六角色只读证据投影。

    case 的 ``split``、label、script 和 expected route 仅用于离线评估与账本选择，绝不写入
    ``AgentTask.input_snapshot``。模型会收到的只有受治理 Bundle、确定性触发码和已解析证据。
    """

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("formal smoke projection requires timezone-aware clock")
    case = _find_case(dataset, case_id)
    workspace, incident = _synthetic_live_parents(case_id=case.case_id, now=instant)
    # _assemble_bundle 是 Phase 16 冻结数据集内部唯一的六角色治理装配实现；此处只消费
    # 它签发的不可变 Bundle，不复用评估标签、ScriptedModel 或任何生产 Store 写路径。
    bundle = _assemble_bundle(workspace=workspace, incident=incident, case=case, now=instant)
    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    references = tuple(component.reference for component in snapshot.components)
    trigger_codes = derive_automatic_escalation_codes(bundle)
    if len(trigger_codes) < 2:
        raise ValueError("formal smoke case does not contain a high-conflict trigger set")
    inventory = next(
        component.payload
        for component in snapshot.components
        if component.role.value == "PRODUCT_INVENTORY_SNAPSHOT"
    )
    if not isinstance(inventory, ProductInventoryPayload):
        raise ValueError("formal smoke inventory evidence is invalid")
    analyst_profile = build_phase16_smoke_evidence_analyst_profile()
    planner_profile = build_phase16_smoke_evidence_planner_profile()
    key = _opaque_case_key(case.case_id)
    analyst_task = AgentTask(
        task_id=f"formal-analyst-{key}",
        task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
        profile_id=analyst_profile.profile_id,
        profile_version=analyst_profile.profile_version,
        room_id=snapshot.scope.room_id,
        trace_id=snapshot.scope.trace_id,
        objective="Analyze only governed sold-out conflict evidence for formal operator-review smoke.",
        input_snapshot={
            "trigger_codes": [code.value for code in trigger_codes],
            # 完整六角色正文由 Runner 在 Resolver 作用域校验后以 ``resolved_evidence``
            # 注入；此处仅绑定 Bundle 摘要，防止同一证据 payload 被重复计入 token 预算。
            "evidence_bundle_digest": snapshot.bundle_digest,
        },
        initial_evidence_refs=references,
    )
    return Phase16OfficialSmokeCaseProjection(
        case_id=case.case_id,
        case_digest=dataset.manifest.case_digests[case.case_id],
        analyst_task=analyst_task,
        planner_profile_id=planner_profile.profile_id,
        planner_profile_version=planner_profile.profile_version,
        evidence_refs=references,
        evidence_registry=_projection_evidence_registry(snapshot),
        trusted_anchor_id=snapshot.scope.anchor_id,
        trigger_codes=tuple(trigger_codes),
        available_backup_product_ids=frozenset(
            product.product_id
            for product in inventory.backup_products
            if product.is_active and product.inventory > 0
        ),
        proposal_eligible=snapshot.proposal_eligible,
        valid_until=snapshot.valid_until,
        evidence_bundle_digest=snapshot.bundle_digest,
    )


class Phase16OfficialSmokeExecutionStatus(StrEnum):
    """正式 smoke 的运行结论；它只描述外部证据，不改变生产路由。"""

    DRY_RUN = "DRY_RUN"
    BLOCKED = "BLOCKED"
    PASS = "PASS"
    FAILED = "FAILED"


class Phase16OfficialSmokeEvidenceConclusion(StrEnum):
    """正式证据的可声明程度，与运行控制状态分离避免混淆未发送和发送失败。"""

    # 预检或任一尚未创建 dispatch 的本地阻断不能证明真实模型行为，也不能归咎于模型。
    INCONCLUSIVE = "INCONCLUSIVE"
    # 只有固定十例、二十次调用和全部回执/结构验证都完成后才可声明 PASS。
    PASS = "PASS"
    # 至少一条 dispatch 已创建且随后发生模型、回执或结构失败时才可声明 FAILED。
    FAILED = "FAILED"


@dataclass(frozen=True)
class Phase16OfficialSmokeCaseExecution:
    """单个固定 slot 的脱敏执行结果，不保存 Prompt、模型正文或经营建议。"""

    case_id: str
    status: Phase16OfficialSmokeExecutionStatus
    reason_code: str
    analyst_attempt_id: str | None
    planner_attempt_id: str | None


@dataclass(frozen=True)
class Phase16OfficialSmokeExecutionReport:
    """正式 Runner 的内存汇总；长期报告必须在 Task 4 从 PostgreSQL receipt 重新渲染。"""

    run_id: str
    status: Phase16OfficialSmokeExecutionStatus
    evidence_conclusion: Phase16OfficialSmokeEvidenceConclusion
    reason_codes: tuple[str, ...]
    case_executions: tuple[Phase16OfficialSmokeCaseExecution, ...]
    model_calls: int


class _NoSkillPort:
    """Smoke Profile 零 Skill 的显式兜底端口，任何意外调用都必须直接失败。"""

    async def invoke(self, **_kwargs: Any) -> dict[str, Any]:
        """共享 Runner 若错误尝试 Skill，拒绝其绕过 Profile 的零 Skill 契约。"""

        raise RuntimeError("formal Phase 16 smoke does not permit Skills")


class _CapturingModelPort:
    """在不改变 AgentModelPort 结果的前提下，保留本次调用的最小回执供账本追加。"""

    def __init__(self, delegate: AgentModelPort) -> None:
        self._delegate = delegate
        self.request = None
        self.outcome: ModelSuccess | ModelFailure | None = None

    async def complete(self, request):
        """逐字转发唯一模型请求；正式 Runner 不在 Port 层隐藏重试或 fallback。"""

        self.request = request
        self.outcome = await self._delegate.complete(request)
        return self.outcome


@dataclass(frozen=True)
class _FormalBudgetReservationClaim:
    """满足共享 Runner 最小 ``created`` 契约的本地包装，真实发送意图由 PostgreSQL 账本保存。"""

    created: bool


class _FormalLedgerBudgetAdapter:
    """把 BoundedSpecialistRunner 的 reserve/settle 调用窄化到一个正式账本 stage。

    ``reserve`` 在模型端口之前调用，因此它创建不可重试的 append-only dispatch attempt。
    ``settle`` 只让共享 Runner 完成自身审计；最终费用权威始终是后续 Provider receipt
    由 PostgreSQL 按冻结官方价格重新计算的结果。
    """

    def __init__(
        self,
        *,
        ledger: Any,
        claim_id: str,
        stage: Phase16OfficialSmokeDispatchStage,
        profile: SpecialistProfile,
    ) -> None:
        self._ledger = ledger
        self._claim_id = claim_id
        self._stage = stage
        self._profile = profile
        self._attempt = None
        self._request_id: str | None = None
        self.settled_amount_cny: Decimal | None = None

    @property
    def attempt(self):
        """返回发送前已写入的 append-only attempt；``None`` 表示模型端口尚未可达。"""

        return self._attempt

    def reserve(self, request_id: str, candidate: object, amount_cny: Decimal) -> _FormalBudgetReservationClaim:
        """在唯一 stage 内写入 dispatch intent，拒绝重复 reserve 或跨阶段候选。"""

        if candidate != self._stage.value or self._attempt is not None:
            raise BudgetInvariantError("formal smoke budget adapter identity is invalid")
        try:
            self._attempt = self._ledger.begin_dispatch(
                claim_id=self._claim_id,
                stage=self._stage,
                profile_digest=self._profile.profile_digest,
                internal_request_id=request_id,
            )
        except Phase16OfficialSmokeLedgerError as error:
            raise BudgetInvariantError("formal smoke ledger rejected dispatch") from error
        self._request_id = request_id
        return _FormalBudgetReservationClaim(created=True)

    def settle(self, request_id: str, actual_cost_cny: Decimal | None) -> _FormalBudgetReservationClaim:
        """记录共享 Runner 已完成本地结算；账本 receipt 仍会独立校验真实 usage 与价格。"""

        if self._attempt is None or request_id != self._request_id:
            raise BudgetInvariantError("formal smoke settlement has no matching attempt")
        self.settled_amount_cny = actual_cost_cny
        return _FormalBudgetReservationClaim(created=False)

    def release(self, request_id: str) -> _FormalBudgetReservationClaim:
        """deadline 在端口前耗尽时保留已写 intent，后续由失败 validation 闭合且绝不重发。"""

        if self._attempt is None or request_id != self._request_id:
            raise BudgetInvariantError("formal smoke release has no matching attempt")
        return _FormalBudgetReservationClaim(created=False)


class _OfficialSmokePricingPolicy:
    """只使用冻结官方价格为共享 Runner 计算最坏预留和实际 usage 成本。"""

    def __init__(self, official_price: Phase16OfficialPriceEvidence) -> None:
        self.policy_digest = official_price.official_price_digest
        self._input_per_million = official_price.input_cny_per_million
        self._output_per_million = official_price.output_cny_per_million

    def count_input_tokens(self, request) -> int:
        """使用确定性字节估算避免引入在线 tokenizer；真实账本仍以 Provider usage 为准。"""

        payload = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return max((len(payload) + 3) // 4, 1)

    def worst_case_cost(self, request, profile: SpecialistProfile) -> Decimal:
        """按请求输出上限保守预约，不超过冻结 Profile 的单 stage 费用边界。"""

        return min(
            self._cost(self.count_input_tokens(request), request.max_output_tokens),
            profile.max_case_cost_cny,
        )

    def actual_cost(self, usage, _profile: SpecialistProfile) -> Decimal:
        """按 Provider 明确 usage 计算真实成本；若超出预留，Runner 和账本都会 fail-closed。"""

        return self._cost(usage.input_tokens, usage.output_tokens)

    def _cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        raw = (
            Decimal(input_tokens) * self._input_per_million
            + Decimal(output_tokens) * self._output_per_million
        ) / Decimal("1000000")
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class _StageExecution:
    """仅在同一进程内暂存 stage 验证结果，便于安全地把 Analyst 载荷传给 Planner。"""

    passed: bool
    reason_code: str
    attempt_id: str | None
    analysis: ValidatedConflictAnalysisPayload | None = None
    # dispatch attempt 是崩溃恢复/零重试的意图事实，不等同于网络已发送。该字段只在
    # 当前受控进程内传播，决定正式报告应为 BLOCKED + INCONCLUSIVE 还是 FAILED。
    network_sent: bool = False


class Phase16OfficialSmokeRunner:
    """严格执行一轮十例、零重试的正式 Smoke，不接入生产 Coordinator 或经营动作路径。"""

    def __init__(
        self,
        *,
        dataset: Phase16EvaluationDataset,
        manifest: Phase16OfficialSmokeEvidenceManifest,
        preflight: Phase16OfficialSmokePreflight,
        official_price: Phase16OfficialPriceEvidence,
        ledger: Any,
        model_port: AgentModelPort,
        clock: Any | None = None,
    ) -> None:
        self._dataset = dataset
        self._manifest = manifest
        self._preflight = preflight
        self._official_price = official_price
        self._ledger = ledger
        self._model_port = model_port
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._pricing_policy = _OfficialSmokePricingPolicy(official_price)

    def dry_run(self) -> Phase16OfficialSmokeExecutionReport:
        """只复核预检/Manifest/十 slot 身份，不创建账本 claim、模型端口或联网尝试。"""

        reasons = self._preflight_reasons()
        return Phase16OfficialSmokeExecutionReport(
            run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
            status=(
                Phase16OfficialSmokeExecutionStatus.DRY_RUN
                if not reasons
                else Phase16OfficialSmokeExecutionStatus.BLOCKED
            ),
            evidence_conclusion=Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE,
            reason_codes=reasons,
            case_executions=(),
            model_calls=0,
        )

    async def execute(self) -> Phase16OfficialSmokeExecutionReport:
        """执行唯一正式十例 run；第一个失败 stage 立即闭合并停止，绝不重发。"""

        reasons = self._preflight_reasons()
        if reasons:
            return Phase16OfficialSmokeExecutionReport(
                run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
                status=Phase16OfficialSmokeExecutionStatus.BLOCKED,
                evidence_conclusion=Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE,
                reason_codes=reasons,
                case_executions=(),
                model_calls=0,
            )
        try:
            projections = self._build_all_projections()
        except Exception:
            return Phase16OfficialSmokeExecutionReport(
                run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
                status=Phase16OfficialSmokeExecutionStatus.BLOCKED,
                evidence_conclusion=Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE,
                reason_codes=("CASE_PROJECTION_BLOCKED",),
                case_executions=(),
                model_calls=0,
            )
        # 所有本地输入、Profile、六角色证据和 freshness 均已验证后才创建数据库 run/claim；
        # 若共享 Runner 仍在 begin_dispatch 前阻断，下面会追加受限 BLOCKED 终态而非伪造
        # FAILED validation，从而既关闭 claim，又准确表达“未发送，证据不充分”。
        try:
            self._ledger.ensure_run(self._manifest)
        except Exception:
            # schema contract、数据库连接或冻结 run 身份无法在发送前成立时，外部模型尚未
            # 得到机会；只能返回 BLOCKED + INCONCLUSIVE，不能把基础设施错误伪造成模型
            # FAILED，也不能创建任何 case claim 或 reservation。
            return Phase16OfficialSmokeExecutionReport(
                run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
                status=Phase16OfficialSmokeExecutionStatus.BLOCKED,
                evidence_conclusion=Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE,
                reason_codes=("LEDGER_INITIALIZATION_BLOCKED",),
                case_executions=(),
                model_calls=0,
            )
        try:
            recovered_outcomes = tuple(self._ledger.recover_open_attempts())
        except Exception:
            # 恢复本身发生在任何新的 claim/attempt 之前。此时不能确定旧账本中是否已存在
            # 外部发送意图，因此必须停止本轮，而不能继续向 Provider 发送新的调用掩盖状态。
            return Phase16OfficialSmokeExecutionReport(
                run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
                status=Phase16OfficialSmokeExecutionStatus.BLOCKED,
                evidence_conclusion=Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE,
                reason_codes=("LEDGER_RECOVERY_BLOCKED",),
                case_executions=(),
                model_calls=0,
            )
        for recovered in recovered_outcomes:
            if recovered.status is Phase16OfficialSmokeCaseOutcomeStatus.PASS:
                continue
            # 一个未闭合的 attempt 在重启后被账本收口为 FAILED/BLOCKED，说明本次唯一
            # 正式 run 已经不可能再形成严格 10/10。必须先报告该事实并停止，不能领取
            # 任何新 slot，更不能对同一 case 发送第二次请求。
            is_blocked = recovered.status is Phase16OfficialSmokeCaseOutcomeStatus.BLOCKED
            return Phase16OfficialSmokeExecutionReport(
                run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
                status=(
                    Phase16OfficialSmokeExecutionStatus.BLOCKED
                    if is_blocked
                    else Phase16OfficialSmokeExecutionStatus.FAILED
                ),
                evidence_conclusion=(
                    Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE
                    if is_blocked
                    else Phase16OfficialSmokeEvidenceConclusion.FAILED
                ),
                reason_codes=(recovered.reason_code,),
                case_executions=(
                    Phase16OfficialSmokeCaseExecution(
                        case_id=recovered.case_id,
                        status=(
                            Phase16OfficialSmokeExecutionStatus.BLOCKED
                            if is_blocked
                            else Phase16OfficialSmokeExecutionStatus.FAILED
                        ),
                        reason_code=recovered.reason_code,
                        analyst_attempt_id=None,
                        planner_attempt_id=None,
                    ),
                ),
                model_calls=0,
            )
        executions: list[Phase16OfficialSmokeCaseExecution] = []
        model_calls = 0
        for projection in projections:
            # 崩溃可能发生在两段 PASS validation 已持久化、但 case outcome 尚未写入的
            # 瞬间。恢复会补写并认证该 PASS；后续执行只能读取该既有事实并跳过 slot，
            # 绝不能因为 ``claim_case`` 返回同一 claim 而再次触发模型端口。
            existing_outcome = self._ledger.get_case_outcome(case_id=projection.case_id)
            if existing_outcome is not None:
                if existing_outcome.status is Phase16OfficialSmokeCaseOutcomeStatus.PASS:
                    executions.append(
                        Phase16OfficialSmokeCaseExecution(
                            case_id=projection.case_id,
                            status=Phase16OfficialSmokeExecutionStatus.PASS,
                            reason_code=existing_outcome.reason_code,
                            analyst_attempt_id=None,
                            planner_attempt_id=None,
                        )
                    )
                    continue
                is_blocked = (
                    existing_outcome.status
                    is Phase16OfficialSmokeCaseOutcomeStatus.BLOCKED
                )
                return Phase16OfficialSmokeExecutionReport(
                    run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
                    status=(
                        Phase16OfficialSmokeExecutionStatus.BLOCKED
                        if is_blocked
                        else Phase16OfficialSmokeExecutionStatus.FAILED
                    ),
                    evidence_conclusion=(
                        Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE
                        if is_blocked
                        else Phase16OfficialSmokeEvidenceConclusion.FAILED
                    ),
                    reason_codes=(existing_outcome.reason_code,),
                    case_executions=tuple(executions)
                    + (
                        Phase16OfficialSmokeCaseExecution(
                            case_id=projection.case_id,
                            status=(
                                Phase16OfficialSmokeExecutionStatus.BLOCKED
                                if is_blocked
                                else Phase16OfficialSmokeExecutionStatus.FAILED
                            ),
                            reason_code=existing_outcome.reason_code,
                            analyst_attempt_id=None,
                            planner_attempt_id=None,
                        ),
                    ),
                    model_calls=model_calls,
                )
            claim = self._ledger.claim_case(projection.case_id)
            analyst = await self._execute_stage(
                projection=projection,
                claim_id=claim.claim_id,
                stage=Phase16OfficialSmokeDispatchStage.ANALYST,
                task=projection.analyst_task,
            )
            model_calls += 1 if analyst.network_sent else 0
            if not analyst.passed or analyst.analysis is None:
                pre_send_blocked = not analyst.network_sent
                self._ledger.close_case(
                    claim_id=claim.claim_id,
                    status=(
                        Phase16OfficialSmokeCaseOutcomeStatus.BLOCKED
                        if pre_send_blocked
                        else Phase16OfficialSmokeCaseOutcomeStatus.FAILED
                    ),
                    reason_code=analyst.reason_code,
                )
                executions.append(
                    Phase16OfficialSmokeCaseExecution(
                        case_id=projection.case_id,
                        status=(
                            Phase16OfficialSmokeExecutionStatus.BLOCKED
                            if pre_send_blocked
                            else Phase16OfficialSmokeExecutionStatus.FAILED
                        ),
                        reason_code=analyst.reason_code,
                        analyst_attempt_id=analyst.attempt_id,
                        planner_attempt_id=None,
                    )
                )
                return Phase16OfficialSmokeExecutionReport(
                    run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
                    status=(
                        Phase16OfficialSmokeExecutionStatus.BLOCKED
                        if pre_send_blocked
                        else Phase16OfficialSmokeExecutionStatus.FAILED
                    ),
                    evidence_conclusion=(
                        Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE
                        if pre_send_blocked
                        else Phase16OfficialSmokeEvidenceConclusion.FAILED
                    ),
                    reason_codes=(analyst.reason_code,),
                    case_executions=tuple(executions),
                    model_calls=model_calls,
                )
            planner = await self._execute_stage(
                projection=projection,
                claim_id=claim.claim_id,
                stage=Phase16OfficialSmokeDispatchStage.PLANNER,
                task=projection.build_planner_task(analyst.analysis),
                analysis=analyst.analysis,
            )
            model_calls += 1 if planner.network_sent else 0
            if not planner.passed:
                pre_send_blocked = not planner.network_sent
                self._ledger.close_case(
                    claim_id=claim.claim_id,
                    status=(
                        Phase16OfficialSmokeCaseOutcomeStatus.BLOCKED
                        if pre_send_blocked
                        else Phase16OfficialSmokeCaseOutcomeStatus.FAILED
                    ),
                    reason_code=planner.reason_code,
                )
                executions.append(
                    Phase16OfficialSmokeCaseExecution(
                        case_id=projection.case_id,
                        status=(
                            Phase16OfficialSmokeExecutionStatus.BLOCKED
                            if pre_send_blocked
                            else Phase16OfficialSmokeExecutionStatus.FAILED
                        ),
                        reason_code=planner.reason_code,
                        analyst_attempt_id=analyst.attempt_id,
                        planner_attempt_id=planner.attempt_id,
                    )
                )
                return Phase16OfficialSmokeExecutionReport(
                    run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
                    status=(
                        Phase16OfficialSmokeExecutionStatus.BLOCKED
                        if pre_send_blocked
                        else Phase16OfficialSmokeExecutionStatus.FAILED
                    ),
                    evidence_conclusion=(
                        Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE
                        if pre_send_blocked
                        else Phase16OfficialSmokeEvidenceConclusion.FAILED
                    ),
                    reason_codes=(planner.reason_code,),
                    case_executions=tuple(executions),
                    model_calls=model_calls,
                )
            self._ledger.close_case(
                claim_id=claim.claim_id,
                status=Phase16OfficialSmokeCaseOutcomeStatus.PASS,
                reason_code="MULTI_AGENT_READY",
            )
            executions.append(
                Phase16OfficialSmokeCaseExecution(
                    case_id=projection.case_id,
                    status=Phase16OfficialSmokeExecutionStatus.PASS,
                    reason_code="MULTI_AGENT_READY",
                    analyst_attempt_id=analyst.attempt_id,
                    planner_attempt_id=planner.attempt_id,
                )
            )
        return Phase16OfficialSmokeExecutionReport(
            run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
            status=Phase16OfficialSmokeExecutionStatus.PASS,
            evidence_conclusion=Phase16OfficialSmokeEvidenceConclusion.PASS,
            reason_codes=(),
            case_executions=tuple(executions),
            model_calls=model_calls,
        )

    def _preflight_reasons(self) -> tuple[str, ...]:
        """把预检 provenance、Manifest、价格和固定十 slot 的异常全部收敛为可展示阻断码。"""

        reasons: set[str] = set(self._preflight.reason_codes)
        if (
            not self._preflight.provenance_verified
            or self._preflight.status is not Phase16OfficialSmokeStatus.READY
            or not self._preflight.can_send
        ):
            reasons.add("PREFLIGHT_NOT_VERIFIED")
        expected_case_ids = self._dataset.manifest.smoke_eligible_case_ids
        if (
            self._manifest.run_id != PHASE16_OFFICIAL_SMOKE_RUN_ID
            or self._manifest.case_ids != expected_case_ids
            or len(self._manifest.case_ids) != 10
        ):
            reasons.add("FORMAL_SLOT_IDENTITY_MISMATCH")
        if self._manifest.manifest_digest != self._preflight.manifest_digest:
            reasons.add("FORMAL_MANIFEST_PRECHECK_MISMATCH")
        if self._manifest.official_price_digest != self._official_price.official_price_digest:
            reasons.add("FORMAL_PRICE_IDENTITY_MISMATCH")
        return tuple(sorted(reasons))

    def _build_all_projections(self) -> tuple[Phase16OfficialSmokeCaseProjection, ...]:
        """在任何 claim/attempt 前重建十个冻结 case，阻断陈旧或非高冲突证据。"""

        instant = self._clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("formal smoke clock must be timezone-aware")
        projections = tuple(
            build_phase16_official_smoke_case_projection(
                dataset=self._dataset,
                case_id=case_id,
                now=instant,
            )
            for case_id in self._manifest.case_ids
        )
        if any(
            not projection.proposal_eligible or projection.valid_until <= instant
            for projection in projections
        ):
            raise ValueError("formal smoke evidence is stale or ineligible")
        return projections

    async def _execute_stage(
        self,
        *,
        projection: Phase16OfficialSmokeCaseProjection,
        claim_id: str,
        stage: Phase16OfficialSmokeDispatchStage,
        task: AgentTask,
        analysis: ValidatedConflictAnalysisPayload | None = None,
    ) -> _StageExecution:
        """通过共享 Runner 执行一段，并将已发送回执/验证摘要追加到正式账本。"""

        profile = (
            build_phase16_smoke_evidence_analyst_profile()
            if stage is Phase16OfficialSmokeDispatchStage.ANALYST
            else build_phase16_smoke_evidence_planner_profile()
        )
        capture = _CapturingModelPort(self._model_port)
        budget_adapter = _FormalLedgerBudgetAdapter(
            ledger=self._ledger,
            claim_id=claim_id,
            stage=stage,
            profile=profile,
        )
        bounded_runner = BoundedSpecialistRunner(
            orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
            model_port=capture,
            budget_store=budget_adapter,
            evidence_registry=projection.evidence_registry,
            skill_port=_NoSkillPort(),
            skill_catalog=(),
            trusted_anchor_resolver=lambda _task: projection.trusted_anchor_id,
            pricing_policy=self._pricing_policy,
            # 账本拒绝自由文本 request ID；使用 Manifest/case/stage 派生的稳定 UUID，
            # 同一已发送 attempt 重放也会命中相同身份而非创建第二条外部请求。
            budget_candidate_resolver=lambda _task: stage.value,
            request_id_factory=lambda _task, _execution_id, _index: str(
                uuid5(
                    NAMESPACE_URL,
                    f"{self._manifest.manifest_digest}:{projection.case_id}:{stage.value}",
                )
            ),
            clock=self._clock,
        )
        try:
            resolved_profile = bounded_runner.resolve_profile(task)
            if resolved_profile.profile_digest != profile.profile_digest:
                raise ValueError("formal profile digest mismatch")
            result = await bounded_runner.run(task)
        except Exception:
            # 若异常发生在 reserve 前，账本没有 attempt，说明网络端口没有得到机会；调用方
            # 将它视为发送前 BLOCKED，而不是伪造成可审计的 Provider 失败。
            if budget_adapter.attempt is None:
                return _StageExecution(
                    passed=False,
                    reason_code="FORMAL_RUNNER_PRE_SEND_BLOCKED",
                    attempt_id=None,
                )
            result = None

        attempt = budget_adapter.attempt
        if attempt is None:
            return _StageExecution(
                passed=False,
                reason_code="FORMAL_RUNNER_PRE_SEND_BLOCKED",
                attempt_id=None,
            )
        # reserve 写入的是不可重试的 intent，而不是“Provider 已收包”的断言。端口未被
        # 调用（例如 reserve 后 deadline 到期）或明确返回 request_sent=False 时，追加
        # BLOCKED validation 并让上层形成 INCONCLUSIVE；异常、超时或 request_sent=True
        # 则保守视为已发送/未知，继续走 FAILED，绝不能冒险重发。
        if capture.request is None or (
            isinstance(capture.outcome, ModelFailure)
            and not capture.outcome.request_sent
        ):
            return self._append_blocked_validation(
                attempt_id=attempt.attempt_id,
                stage=stage,
                reason_code="MODEL_REQUEST_NOT_SENT",
            )
        if not isinstance(capture.outcome, ModelSuccess):
            return self._append_failed_validation(
                attempt_id=attempt.attempt_id,
                stage=stage,
                reason_code="MODEL_OUTCOME_UNAVAILABLE",
            )
        try:
            validate_phase16_official_smoke_receipt(capture.outcome)
            usage = capture.outcome.usage
            if usage is None:
                raise ValueError("usage is required")
            self._ledger.append_provider_receipt(
                attempt_id=attempt.attempt_id,
                provider_response_id=capture.outcome.provider_response_id,
                finish_reason=capture.outcome.finish_reason,
                model_id=capture.outcome.model_id,
                response_digest=capture.outcome.response_digest,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                latency_ms=capture.outcome.latency_ms,
            )
        except Exception:
            return self._append_failed_validation(
                attempt_id=attempt.attempt_id,
                stage=stage,
                reason_code="PROVIDER_RECEIPT_INVALID",
            )
        try:
            if not isinstance(result, AgentResult):
                raise ValueError("bounded runner did not return an AgentResult")
            if stage is Phase16OfficialSmokeDispatchStage.ANALYST:
                validated_analysis = validate_conflict_analysis_result(
                    task=task,
                    result=result,
                    expected_profile=profile,
                    expected_evidence_refs=projection.evidence_refs,
                    expected_finding_codes=projection.trigger_codes,
                )
                self._append_validation_pass(
                    attempt_id=attempt.attempt_id,
                    stage=stage,
                    task=task,
                    result=result,
                )
                return _StageExecution(
                    passed=True,
                    reason_code="ANALYST_VALIDATION_PASS",
                    attempt_id=attempt.attempt_id,
                    analysis=validated_analysis,
                    network_sent=True,
                )
            if analysis is None:
                raise ValueError("planner requires validated analyst payload")
            validate_live_decision_planner_result(
                task=task,
                result=result,
                expected_profile=profile,
                expected_evidence_refs=projection.evidence_refs,
                required_risk_codes=frozenset(item.value for item in analysis.risk_codes),
                available_backup_product_ids=projection.available_backup_product_ids,
                # 正式 smoke 在创建任何 claim 前已由 _build_all_projections 一次性验证十份
                # 冻结快照的 proposal_eligible/TTL。此路径不会创建生产 Proposal、Operator
                # Decision 或经营命令；若在每条离线回放后再次按墙钟读取短 TTL，长达 20 次
                # 调用的审计演练会把已验证快照错误当成 LIVE 陈旧事实。生产 Coordinator
                # 保持其逐次墙钟 freshness 检查，二者不能互相放宽。
                proposal_eligible_and_fresh=projection.proposal_eligible,
            )
            self._append_validation_pass(
                attempt_id=attempt.attempt_id,
                stage=stage,
                task=task,
                result=result,
            )
            return _StageExecution(
                passed=True,
                reason_code="PLANNER_VALIDATION_PASS",
                attempt_id=attempt.attempt_id,
                network_sent=True,
            )
        except Exception:
            return self._append_failed_validation(
                attempt_id=attempt.attempt_id,
                stage=stage,
                reason_code=(
                    "ANALYST_VALIDATION_FAILED"
                    if stage is Phase16OfficialSmokeDispatchStage.ANALYST
                    else "PLANNER_VALIDATION_FAILED"
                ),
            )

    def _append_validation_pass(
        self,
        *,
        attempt_id: str,
        stage: Phase16OfficialSmokeDispatchStage,
        task: AgentTask,
        result: AgentResult,
    ) -> None:
        """写入不含模型正文的 PASS validation digest，账本随后才允许 Planner/最终 PASS。"""

        self._ledger.append_validation_fact(
            attempt_id=attempt_id,
            verdict=Phase16OfficialSmokeValidationVerdict.PASS,
            reason_code=f"{stage.value}_VALIDATION_PASS",
            validation_digest=self._validation_digest(
                stage=stage,
                task=task,
                result=result,
            ),
        )

    def _append_failed_validation(
        self,
        *,
        attempt_id: str,
        stage: Phase16OfficialSmokeDispatchStage,
        reason_code: str,
    ) -> _StageExecution:
        """已写 dispatch attempt 后的任意异常都追加 FAILED validation，禁止补发或文本修补。"""

        self._ledger.append_validation_fact(
            attempt_id=attempt_id,
            verdict=Phase16OfficialSmokeValidationVerdict.FAILED,
            reason_code=reason_code,
            validation_digest=canonical_json_sha256(
                {"attempt_id": attempt_id, "stage": stage.value, "reason_code": reason_code}
            ),
        )
        return _StageExecution(
            passed=False,
            reason_code=reason_code,
            attempt_id=attempt_id,
            network_sent=True,
        )

    def _append_blocked_validation(
        self,
        *,
        attempt_id: str,
        stage: Phase16OfficialSmokeDispatchStage,
        reason_code: str,
    ) -> _StageExecution:
        """闭合明确未发送的 intent，保留零重试事实而不伪造模型失败。"""

        self._ledger.append_validation_fact(
            attempt_id=attempt_id,
            verdict=Phase16OfficialSmokeValidationVerdict.BLOCKED,
            reason_code=reason_code,
            validation_digest=canonical_json_sha256(
                {
                    "attempt_id": attempt_id,
                    "stage": stage.value,
                    "reason_code": reason_code,
                    "network_sent": False,
                }
            ),
        )
        return _StageExecution(
            passed=False,
            reason_code=reason_code,
            attempt_id=attempt_id,
            network_sent=False,
        )

    @staticmethod
    def _validation_digest(
        *,
        stage: Phase16OfficialSmokeDispatchStage,
        task: AgentTask,
        result: AgentResult,
    ) -> str:
        """仅哈希结构身份与输出摘要；原始模型正文不进入正式 receipt 或工作日志。"""

        return canonical_json_sha256(
            {
                "stage": stage.value,
                "task_digest": task.task_digest,
                "result_status": result.status.value,
                "output_digest": canonical_json_sha256(_plain_json(result.output)),
                "evidence_refs": [
                    item.model_dump(mode="json") for item in result.evidence_refs
                ],
            }
        )
