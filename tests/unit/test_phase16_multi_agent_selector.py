"""Phase 16 Task 5 高冲突选择与 EvidenceAnalystAgent 协调器的 RED/GREEN 契约。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Callable

import pytest

from src.decision_support.models import (
    ConflictAnalysisCode,
    DecisionKind,
    EscalationMode,
    EscalationRecord,
    Incident,
    LiveSessionWorkspace,
    MultiAgentFailureCode,
    MultiAgentOutcomeStatus,
    WorkspaceView,
)
from src.decision_support.commands import (
    DecisionCompilationError,
    DecisionExecutionContext,
    DecisionSupportCommandCompiler,
    OperatorDecisionDraft,
)
from src.decision_support.evidence import EvidenceBundleSnapshot
from src.decision_support.multi_agent import (
    HighConflictEscalationCoordinator,
    build_evidence_analyst_profile,
    build_decision_planner_profile,
)
from src.decision_support.proposal import LiveDecisionProposal, ProposalOrigin, ProposalStatus
from src.decision_support.store import (
    InMemoryDecisionSupportStore,
    WorkspaceConflictError,
    WorkspaceLeaseError,
)
from src.specialist_runtime.models import (
    AgentFailure,
    AgentResult,
    AgentResultStatus,
    AgentTask,
)
from src.specialist_runtime.profiles import SpecialistProfile
from src.plan_engine.models import PlanNodeState
from tests.phase14_evidence_factory import build_evidence_bundle


def _now() -> datetime:
    """每个 Fixture 使用当前 UTC，保持十秒 Evidence TTL 是生产门禁而非测试旁路。"""

    return datetime.now(timezone.utc)


def _seed_bundle(
    *,
    suffix: str,
    include_availability_noise: bool = True,
    pause_required: bool = True,
    reconciliation_required: bool = False,
    valid_backup_count: int = 1,
    store_clock: Callable[[], datetime] | None = None,
) -> tuple[InMemoryDecisionSupportStore, Any, Any, Any]:
    """以真实 Workspace、Incident、受治理六角色 Bundle 建立可升级的 LIVE 父事实。"""

    instant = _now()
    session_id = f"phase16-task5-session-{suffix}"
    incident_id = f"phase16-task5-incident-{suffix}"
    room_id = f"room-{suffix}"
    trace_id = f"trace-{suffix}"
    root_plan_run_id = f"root-plan-{suffix}"
    store = InMemoryDecisionSupportStore(clock=store_clock)
    workspace = store.create_workspace(
        LiveSessionWorkspace(
            live_session_id=session_id,
            run_key=f"run-{suffix}",
            room_id=room_id,
            trace_id=trace_id,
            # 共用工厂默认锚点是受治理 Bundle scope 的一部分；Workspace 必须使用
            # 完全相同的身份，不能为了测试选择器绕过 Store 的父事实绑定校验。
            anchor_id="anchor-phase14",
            root_plan_run_id=root_plan_run_id,
            event_inbox_scope_id=f"inbox-{suffix}",
            decision_trace_scope_id=f"decision-trace-{suffix}",
            replay_scope_id=f"replay-{suffix}",
            evaluation_scope_id=f"evaluation-{suffix}",
            view=WorkspaceView.PREPARE,
        )
    )
    lease = store.acquire_operator_lock(
        session_id, "operator-phase16-task5", 60, now=instant
    )
    workspace = store.advance_view(
        session_id,
        target_view=WorkspaceView.LIVE,
        expected_version=workspace.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=instant,
    )
    workspace = store.append_incident(
        Incident(
            incident_id=incident_id,
            live_session_id=session_id,
            idempotency_key=f"incident-idem-{suffix}",
            incident_type="SOLD_OUT_COMPOSITE",
            source_ref_ids=(f"event-{suffix}",),
            snapshot={"product_id": "p001", "expected_version": 2},
            created_at=instant,
        ),
        expected_workspace_version=workspace.version,
    )
    assembled = build_evidence_bundle(
        live_session_id=session_id,
        incident_id=incident_id,
        suffix=suffix,
        idempotency_key=f"bundle-idem-{suffix}",
        evidence_bundle_id=f"bundle-{suffix}",
        room_id=room_id,
        trace_id=trace_id,
        root_plan_run_id=root_plan_run_id,
        created_at=instant,
        evidence_time=instant,
        include_availability_noise=include_availability_noise,
        pause_required=pause_required,
        reconciliation_required=reconciliation_required,
        valid_backup_count=valid_backup_count,
    )
    workspace = store.append_evidence_bundle(
        assembled, expected_workspace_version=workspace.version
    )
    return store, workspace, lease, assembled.bundle


class _ScriptedAnalystRunner:
    """只记录冻结任务并返回受控结果，证明协调器本身不产生网络模型调用。"""

    def __init__(
        self,
        *,
        failure: AgentResultStatus | None = None,
        profile: SpecialistProfile | None = None,
    ) -> None:
        self.calls: list[AgentTask] = []
        self._failure = failure
        self._profile = profile or build_evidence_analyst_profile()

    def resolve_profile(self, _task: AgentTask) -> SpecialistProfile:
        """暴露实际 Runner Registry 的 Profile，供协调器在发送前核对完整冻结身份。"""

        return self._profile

    async def run(self, task: AgentTask) -> AgentResult:
        """以任务自己的六个权威 EvidenceRef 回显，模拟共享 Runner 的结构化 FINAL。"""

        self.calls.append(task)
        return self._result(task)

    def _result(self, task: AgentTask) -> AgentResult:
        """把确定性结果构造与调用计数分离，阻塞并发 Runner 不会重复记录一次 dispatch。"""

        if self._failure is not None:
            return AgentResult(
                task_id=task.task_id,
                profile_id=task.profile_id,
                profile_version=task.profile_version,
                status=self._failure,
                failure=AgentFailure(code="SCRIPTED_ANALYST_FAILURE", details={}),
                summary="SCRIPTED_ANALYST_FAILURE",
            )
        return AgentResult(
            task_id=task.task_id,
            profile_id=task.profile_id,
            profile_version=task.profile_version,
            status=AgentResultStatus.SUCCEEDED,
            output={
                # Runner 只回显 Coordinator 提供的确定性触发事实，模拟正确模型
                # 不可扩张 finding 的 FINAL 输出，而不是把某个测试组合硬编码在 Fake 中。
                "finding_codes": list(task.input_snapshot["trigger_codes"]),
                "constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED"],
                "risk_codes": ["INVENTORY_CONFLICT_REQUIRES_REVIEW"],
                "explanation": "高噪声可用性冲突与暂停节奏同时出现，需要运营确认。",
                "evidence_refs": [
                    reference.model_dump(mode="json") for reference in task.initial_evidence_refs
                ],
            },
            evidence_refs=task.initial_evidence_refs,
            summary="SCRIPTED_ANALYST_SUCCEEDED",
        )


class _ScriptedPlannerRunner:
    """只回显冻结 EvidenceRef 的 Planner 替身，证明协调器不需网络即可验证完整方案链。"""

    def __init__(self, *, options: list[dict[str, object]] | None = None) -> None:
        self.calls: list[AgentTask] = []
        self._profile = build_decision_planner_profile()
        self._options = options

    def resolve_profile(self, _task: AgentTask) -> SpecialistProfile:
        """暴露启动冻结 Planner Profile，供生产协调器在发送前比较完整摘要。"""

        return self._profile

    async def run(self, task: AgentTask) -> AgentResult:
        """返回一个只含受限选项的结构化 FINAL，父事实和 Proposal 身份仍由协调器注入。"""

        self.calls.append(task)
        return self._result(task)

    def _result(self, task: AgentTask) -> AgentResult:
        """按冻结任务重建合法 Planner 回应，阻塞替身可复用且不会二次计数。"""

        options = self._options or [
            {
                "option_id": "switch-backup",
                "product_strategy": "SWITCH_TO_BACKUP",
                "backup_product_id": "p002",
                "host_prompt": "主商品售罄，请等待运营确认后切换备品。",
                "timing": "AFTER_OPERATOR_CONFIRMATION",
                "risk_flags": [
                    "BACKUP_PRODUCT_REQUIRES_CONFIRMATION",
                    "HUMAN_CONFIRMATION_REQUIRED",
                    "INVENTORY_CONFLICT_REQUIRES_REVIEW",
                ],
                "evidence_refs": None,
            }
        ]
        # 测试替身只允许以 None 请求 Coordinator 注入任务已有的 EvidenceRef，避免测试
        # 自己伪造证据身份；其他值原样返回，用于证明生产 Validator 会拒绝坏引用。
        normalized_options = []
        for option in options:
            normalized = dict(option)
            if normalized.get("evidence_refs") is None:
                normalized["evidence_refs"] = [
                    reference.model_dump(mode="json")
                    for reference in task.initial_evidence_refs
                ]
            normalized_options.append(normalized)
        return AgentResult(
            task_id=task.task_id,
            profile_id=task.profile_id,
            profile_version=task.profile_version,
            status=AgentResultStatus.SUCCEEDED,
            output={
                "options": normalized_options
            },
            evidence_refs=task.initial_evidence_refs,
            summary="SCRIPTED_PLANNER_SUCCEEDED",
        )


class _WrongProfilePlannerRunner(_ScriptedPlannerRunner):
    """模拟同一 Coordinator 被错误 Profile 装配，发送前身份校验必须阻断模型调用。"""

    def resolve_profile(self, _task: AgentTask) -> SpecialistProfile:
        """故意返回 Analyst Profile，证明仅名称相近或可运行的 Runner 都不能获得 Planner 权限。"""

        return build_evidence_analyst_profile()


class _ForgedBundleObject:
    """模拟同进程不可信调用方提供的伪对象，仅暴露协调器入口会读取的序列化方法。"""

    def model_dump(self, *, mode: str) -> dict[str, object]:
        """返回缺少 EvidenceBundle 所需父事实的 JSON，必须在模型调用前被重新校验拒绝。"""

        assert mode == "json"
        return {"snapshot": {"forged": True}}


class _DispatchFailureStore:
    """在成功模型返回后分别注入一次 Analysis/Outcome 写入失败，模拟响应丢失窗口。"""

    def __init__(self, delegate: InMemoryDecisionSupportStore) -> None:
        self._delegate = delegate
        self._fail_analysis_once = True
        self._fail_outcome_once = True

    def __getattr__(self, name: str) -> Any:
        """除两个精确失败点外原样委托 Store，避免测试替身扩大持久化接口。"""

        return getattr(self._delegate, name)

    def append_conflict_analysis(self, *args: Any, **kwargs: Any) -> Any:
        """第一次 Analysis 写入失败，模拟模型已经返回而进程尚未保存事实的场景。"""

        if self._fail_analysis_once:
            self._fail_analysis_once = False
            raise RuntimeError("injected analysis persistence loss")
        return self._delegate.append_conflict_analysis(*args, **kwargs)

    def append_multi_agent_outcome(self, *args: Any, **kwargs: Any) -> Any:
        """第一次降级写入同样失败，迫使重试只依赖持久化 dispatch claim 恢复。"""

        if self._fail_outcome_once:
            self._fail_outcome_once = False
            raise RuntimeError("injected outcome persistence loss")
        return self._delegate.append_multi_agent_outcome(*args, **kwargs)


class _BlockingAnalystRunner(_ScriptedAnalystRunner):
    """把首个 Analyst 调用停在发送后边界，用于验证并发协调器不能重复 dispatch。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, task: AgentTask) -> AgentResult:
        """首个调用等待释放，第二个调用若发生会立即暴露为重复发送计数。"""

        self.calls.append(task)
        if len(self.calls) == 1:
            self.entered.set()
            await self.release.wait()
        return self._result(task)


class _BlockingPlannerRunner(_ScriptedPlannerRunner):
    """把首个 Planner 发送停在 claim 窗口内，验证并发恢复不能产生第二次模型调用。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, task: AgentTask) -> AgentResult:
        """首个 Planner 等待测试释放；任何绕过 claim 的第二次调用都会增加 calls 计数。"""

        self.calls.append(task)
        if len(self.calls) == 1:
            self.entered.set()
            await self.release.wait()
        return self._result(task)


class _CoordinatorDeadlinePlannerRunner(_ScriptedPlannerRunner):
    """在 Planner 观察窗口内模拟全局 deadline 耗尽，区分模型错误与协调器超时。"""

    async def run(self, task: AgentTask) -> AgentResult:
        """记录唯一发送后直接抛超时；单调时钟由测试序列推进到五秒外。"""

        self.calls.append(task)
        raise asyncio.TimeoutError


class _ReadyOutcomeFailureStore:
    """只在 Proposal 已提交后的首个 READY Outcome 写入点制造响应丢失窗口。"""

    def __init__(self, delegate: InMemoryDecisionSupportStore) -> None:
        self._delegate = delegate
        self._fail_ready_once = True

    def __getattr__(self, name: str) -> Any:
        """除 READY Outcome 的精确失败点外，所有存储语义仍委托生产内存 Store。"""

        return getattr(self._delegate, name)

    def append_multi_agent_outcome(self, *args: Any, **kwargs: Any) -> Any:
        """模拟 Proposal 已持久化而终态响应尚未写入时的单次进程中断。"""

        fact = args[0]
        if self._fail_ready_once and fact.status is MultiAgentOutcomeStatus.READY:
            self._fail_ready_once = False
            raise RuntimeError("injected READY outcome persistence loss")
        return self._delegate.append_multi_agent_outcome(*args, **kwargs)


class _DelayedBundleReadStore:
    """在权威 Bundle 读取完成后推进单调时钟，验证总预算从公共入口而非内部协调开始。"""

    def __init__(self, delegate: InMemoryDecisionSupportStore, advance: Callable[[], None]) -> None:
        self._delegate = delegate
        self._advance = advance
        self._advanced = False

    def __getattr__(self, name: str) -> Any:
        """除一次可控读取延迟外，保持所有 Store 行为与生产内存实现一致。"""

        return getattr(self._delegate, name)

    def get_evidence_bundle(self, fact_id: str):
        """模拟入口处的权威 Store 读取耗时，不能在读取后重新获得新的五秒模型预算。"""

        bundle = self._delegate.get_evidence_bundle(fact_id)
        if not self._advanced:
            self._advanced = True
            self._advance()
        return bundle


class _ReviewBeforeDegradedOutcomeStore:
    """在首次降级终态写入前推进 REVIEW，复现 LIVE/REVIEW 竞争而不伪造 Store 结果。"""

    def __init__(
        self,
        delegate: InMemoryDecisionSupportStore,
        *,
        lease: Any,
        review_now: datetime,
    ) -> None:
        self._delegate = delegate
        self._lease = lease
        self._review_now = review_now
        self._advanced = False

    def __getattr__(self, name: str) -> Any:
        """除精确竞争点外，所有事实读取和写入仍由生产内存 Store 负责。"""

        return getattr(self._delegate, name)

    def append_multi_agent_outcome(self, fact: Any, **kwargs: Any) -> Any:
        """第一次 DEGRADED 追加前让运营切到 REVIEW，迫使 Coordinator 重建受限终态。"""

        if not self._advanced and fact.status is MultiAgentOutcomeStatus.DEGRADED:
            self._advanced = True
            workspace = self._delegate.get_workspace(fact.live_session_id)
            self._delegate.advance_view(
                fact.live_session_id,
                target_view=WorkspaceView.REVIEW,
                expected_version=workspace.version,
                operator_id=self._lease.operator_id,
                fencing_token=self._lease.fencing_token,
                now=self._review_now,
            )
        return self._delegate.append_multi_agent_outcome(fact, **kwargs)


def test_automatic_three_select_two_persists_analysis_and_retries_without_second_model_call() -> None:
    """自动路径只在精确两项冻结信号成立时升级，并用稳定身份跨重试复用分析。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="automatic")
    runner = _ScriptedAnalystRunner()
    coordinator = HighConflictEscalationCoordinator(
        store=store,
        analyst_runner=runner,
        clock=_now,
    )

    first = asyncio.run(
        coordinator.run_automatic(bundle, expected_workspace_version=workspace.version)
    )
    retry = asyncio.run(
        coordinator.run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert first.selected is True
    assert first.escalation is not None
    assert first.analysis is not None
    assert first.outcome is None
    assert first.escalation.trigger_codes == (
        "AVAILABILITY_NOISE_HIGH",
        "RHYTHM_PAUSE_REQUIRED",
    )
    assert retry.analysis == first.analysis
    assert len(runner.calls) == 1
    assert runner.calls[0].task_id == f"phase16-analyst:{first.escalation.escalation_id}"
    assert runner.calls[0].task_kind.value == "CONFLICT_ANALYSIS"


def test_planner_persists_full_lineage_before_ready_outcome() -> None:
    """Task 6 只能用已验证 Analysis 生成整份 Proposal，READY 仍不授予任何执行权限。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="planner-ready")
    analyst = _ScriptedAnalystRunner()
    planner = _ScriptedPlannerRunner()
    coordinator = HighConflictEscalationCoordinator(
        store=store,
        analyst_runner=analyst,
        planner_runner=planner,
        clock=_now,
    )

    result = asyncio.run(
        coordinator.run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.analysis is not None
    assert result.proposal is not None
    assert result.proposal.status is ProposalStatus.READY
    assert result.proposal.proposal_origin is ProposalOrigin.MULTI_AGENT
    assert result.proposal.multi_agent_lineage is not None
    assert result.proposal.multi_agent_lineage.analysis_id == result.analysis.analysis_id
    assert result.outcome is not None
    assert result.outcome.status is MultiAgentOutcomeStatus.READY
    assert result.outcome.proposal_id == result.proposal.proposal_id
    assert len(planner.calls) == 1
    persisted = store.get_proposal(result.proposal.proposal_id)
    stored_proposal = LiveDecisionProposal.model_validate(persisted.snapshot)
    assert stored_proposal == result.proposal


def test_generic_proposal_store_rejects_multi_agent_proposal_even_on_replay() -> None:
    """通用 Proposal 入口不得创建或重放多 Agent 快照，只有 Coordinator 可写该事实。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="generic-multi-agent-rejected")
    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=_ScriptedPlannerRunner(),
            clock=_now,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )
    assert result.proposal is not None

    with pytest.raises(WorkspaceConflictError, match="multi-agent proposal requires coordinator"):
        # 使用已由 Coordinator 写入的同一 Proposal 重放，证明门禁位于通用 Store 边界，
        # 不会因幂等检查早返回而让 HTTP 或错误装配借既有 payload 绕过专用入口。
        store.append_proposal(
            store.get_proposal(result.proposal.proposal_id),
            expected_workspace_version=store.get_workspace(bundle.live_session_id).version,
        )


def test_multi_agent_approval_requires_exact_ready_outcome() -> None:
    """结构合法且 READY 的多 Agent Proposal 缺少匹配 Outcome 时不得编译经营恢复命令。"""

    store, workspace, operator_lease, bundle = _seed_bundle(
        suffix="multi-agent-approval-outcome"
    )
    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=_ScriptedPlannerRunner(),
            clock=_now,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )
    assert result.proposal is not None
    proposal = store.get_proposal(result.proposal.proposal_id)
    draft = OperatorDecisionDraft(
        decision_id="phase16-decision-missing-outcome",
        proposal_id=proposal.proposal_id,
        expected_proposal_version=proposal.proposal_version,
        operator_id=operator_lease.operator_id,
        decision_kind=DecisionKind.APPROVE,
        reason_code="OPERATOR_CONFIRMED",
        idempotency_key="phase16-decision-missing-outcome",
        option_id=result.proposal.options[0].option_id,
    )

    with pytest.raises(DecisionCompilationError, match="multi-agent proposal requires READY outcome"):
        DecisionSupportCommandCompiler().compile(
            proposal=proposal,
            draft=draft,
            lease=operator_lease,
            execution_context=DecisionExecutionContext(
                plan_run_id="phase16-plan-run",
                expected_plan_version=1,
                node_id="phase16-approval-node",
                expected_node_status=PlanNodeState.WAITING_APPROVAL,
            ),
            now=_now(),
        )

    compiled = DecisionSupportCommandCompiler().compile(
        proposal=proposal,
        draft=draft,
        lease=operator_lease,
        execution_context=DecisionExecutionContext(
            plan_run_id="phase16-plan-run",
            expected_plan_version=1,
            node_id="phase16-approval-node",
            expected_node_status=PlanNodeState.WAITING_APPROVAL,
        ),
        now=_now(),
        multi_agent_ready_outcome=result.outcome,
    )
    # 唯一完整 Outcome 到位后仍只编译人工批准意图，执行提交继续由 Phase 14 恢复门面控制。
    assert compiled.plan_command is not None
    assert compiled.execution_command is not None


def test_planner_task_receives_only_exact_bundle_and_validated_analysis() -> None:
    """Planner 输入不得泄漏 Escalation、操作员或幂等控制字段，只保留冻结的两个事实。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="planner-minimal-input")
    planner = _ScriptedPlannerRunner()
    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=planner,
            clock=_now,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.analysis is not None
    assert len(planner.calls) == 1
    # 任务身份仍在 AgentTask 的固定字段中；模型正文只能读取 Bundle 与已验证 Analysis，
    # 因而不能看到 Escalation mode、operator_id 或 idempotency_key 等控制面字段。
    assert set(planner.calls[0].input_snapshot) == {"analysis", "evidence_bundle"}
    assert (
        planner.calls[0].input_snapshot["analysis"]["analysis_id"]
        == result.analysis.analysis_id
    )


def test_restart_after_persisted_analysis_starts_planner_without_resending_analyst() -> None:
    """分析已落库而 Planner 尚未装配时，后续受控重启只能复用 Analysis 并继续规划。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="planner-restart")
    analyst = _ScriptedAnalystRunner()
    initial = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=analyst,
            clock=_now,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )
    planner = _ScriptedPlannerRunner()
    resumed = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=planner,
            clock=_now,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert initial.analysis is not None
    assert len(analyst.calls) == 1
    assert resumed.analysis == initial.analysis
    assert resumed.proposal is not None
    assert resumed.outcome is not None
    assert resumed.outcome.status is MultiAgentOutcomeStatus.READY
    assert len(planner.calls) == 1


def test_planner_rejects_the_whole_proposal_when_any_option_uses_unavailable_backup() -> None:
    """一至三个选项必须作为整体通过；任何失效备品都只能得到带 Analysis 的降级终态。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="planner-invalid-backup")
    analyst = _ScriptedAnalystRunner()
    planner = _ScriptedPlannerRunner(
        options=[
            {
                "option_id": "unavailable-backup",
                "product_strategy": "SWITCH_TO_BACKUP",
                "backup_product_id": "p999",
                "host_prompt": "备品库存需要运营确认。",
                "timing": "AFTER_OPERATOR_CONFIRMATION",
                "risk_flags": [
                    "BACKUP_PRODUCT_REQUIRES_CONFIRMATION",
                    "HUMAN_CONFIRMATION_REQUIRED",
                    "INVENTORY_CONFLICT_REQUIRES_REVIEW",
                ],
                "evidence_refs": None,
            }
        ]
    )

    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=analyst,
            planner_runner=planner,
            clock=_now,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.analysis is not None
    assert result.proposal is None
    assert result.outcome is not None
    assert result.outcome.status is MultiAgentOutcomeStatus.DEGRADED
    assert result.outcome.failure_code is MultiAgentFailureCode.VALIDATOR_REJECTED
    assert len(planner.calls) == 1


def test_coordinator_does_not_send_planner_after_end_to_end_budget_is_exhausted() -> None:
    """Analysis 落库后若五秒总预算已耗尽，Coordinator 只能降级而不能再发送 Planner。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="planner-total-timeout")
    # 依次覆盖 Coordinator 起点、Analyst 前检查、Analyst 等待裁剪、Analyst 返回后、
    # Analysis 验证后和 Planner 前检查；只有最后一刻跨越五秒，才能证明本用例测试的是
    # Planner 而不是新增的 Analyst 派生事实预算门禁。
    monotonic_values = iter((0.0, 0.0, 0.0, 0.0, 0.0, 5.1))
    planner = _ScriptedPlannerRunner()
    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=planner,
            clock=_now,
            monotonic_clock=lambda: next(monotonic_values),
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.analysis is not None
    assert result.proposal is None
    assert result.outcome is not None
    assert result.outcome.status is MultiAgentOutcomeStatus.DEGRADED
    assert result.outcome.failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT
    assert planner.calls == []


def test_coordinator_does_not_send_analyst_after_end_to_end_budget_is_exhausted() -> None:
    """全局五秒预算在 Analyst claim 前耗尽时，Coordinator 只能持久化降级而不能发送模型。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="analyst-total-timeout")
    monotonic_values = iter((0.0, 5.1))
    analyst = _ScriptedAnalystRunner()

    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=analyst,
            clock=_now,
            monotonic_clock=lambda: next(monotonic_values),
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.selected is True
    assert result.analysis is None
    assert result.outcome is not None
    assert result.outcome.failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT
    assert analyst.calls == []


def test_coordinator_does_not_validate_analyst_response_after_total_budget_expires() -> None:
    """Analyst 返回后若总预算已耗尽，Coordinator 必须先超时降级而不能解析错误响应。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="analyst-post-result-budget")
    # 顺序覆盖入口、发送前检查和 wait_for 裁剪；最后一个值只在 Runner 返回后出现。
    # 使用失败响应可证明此处确实在结构化校验前阻断，而非碰巧在后续写入时失败。
    monotonic_values = iter((0.0, 0.0, 0.0, 5.1))
    analyst = _ScriptedAnalystRunner(failure=AgentResultStatus.MODEL_ERROR)

    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=analyst,
            clock=_now,
            monotonic_clock=lambda: next(monotonic_values),
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.analysis is None
    assert result.outcome is not None
    assert result.outcome.failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT
    assert len(analyst.calls) == 1
    assert store.list_conflict_analyses(bundle.live_session_id) == ()


def test_coordinator_does_not_persist_validated_analysis_after_total_budget_expires() -> None:
    """Analyst 输出验证消耗完五秒后，合法 Analysis 也不得成为新的 append-only 事实。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="analyst-pre-append-budget")
    # 第四次检查发生在 Runner 返回后，仍保留预算；第五次检查位于 Analysis 验证之后、
    # append 前，必须拒绝这条已经迟到但结构正确的模型派生事实。
    monotonic_values = iter((0.0, 0.0, 0.0, 0.0, 5.1))
    analyst = _ScriptedAnalystRunner()

    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=analyst,
            clock=_now,
            monotonic_clock=lambda: next(monotonic_values),
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.analysis is None
    assert result.outcome is not None
    assert result.outcome.failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT
    assert len(analyst.calls) == 1
    assert store.list_conflict_analyses(bundle.live_session_id) == ()


def test_coordinator_budget_starts_before_authoritative_bundle_load() -> None:
    """入口处权威 Bundle 重载耗尽五秒后，Coordinator 不得在内部重新取得完整模型预算。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="entry-budget")
    monotonic_value = [0.0]
    delayed_store = _DelayedBundleReadStore(
        store, lambda: monotonic_value.__setitem__(0, 5.1)
    )
    analyst = _ScriptedAnalystRunner()

    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=delayed_store,
            analyst_runner=analyst,
            clock=_now,
            monotonic_clock=lambda: monotonic_value[0],
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.selected is True
    assert result.analysis is None
    assert result.outcome is not None
    assert result.outcome.failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT
    assert analyst.calls == []


def test_coordinator_does_not_persist_planner_result_after_total_budget_expires() -> None:
    """Planner 在五秒内返回后，验证、Proposal 和 READY 写入前仍必须重新检查端到端期限。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="planner-post-result-budget")
    # 前七次分别覆盖入口、Analyst 发送/验证两处、Planner claim 前、Planner 等待；
    # 第八次才在 Planner 返回后跨越预算，保证本用例仍验证 Proposal 写入门禁。
    monotonic_values = iter((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 5.1))
    planner = _ScriptedPlannerRunner()

    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=planner,
            clock=_now,
            monotonic_clock=lambda: next(monotonic_values),
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.analysis is not None
    assert result.proposal is None
    assert result.outcome is not None
    assert result.outcome.status is MultiAgentOutcomeStatus.DEGRADED
    assert result.outcome.failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT
    assert len(planner.calls) == 1
    assert store.list_proposals(bundle.live_session_id) == ()


def test_planner_profile_mismatch_degrades_before_second_model_call() -> None:
    """Planner Registry 返回错误冻结 Profile 时，Analysis 可保留但 Planner 绝不能被调用。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="planner-profile-mismatch")
    planner = _WrongProfilePlannerRunner()
    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=planner,
            clock=_now,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.analysis is not None
    assert result.proposal is None
    assert result.outcome is not None
    assert result.outcome.failure_code is MultiAgentFailureCode.PLANNER_INVALID_OUTPUT
    assert planner.calls == []


def test_completed_analysis_recovers_after_bundle_expiry_without_reopening_route() -> None:
    """完成事实优先于当前 freshness：过期重试只能恢复，不得错误降为未选中或重发模型。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="expired-recovery")
    first_runner = _ScriptedAnalystRunner()
    first = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store, analyst_runner=first_runner, clock=_now
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )
    recovered_runner = _ScriptedAnalystRunner()
    recovered = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=recovered_runner,
            clock=lambda: _now() + timedelta(minutes=1),
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert first.analysis is not None
    assert recovered.analysis == first.analysis
    assert recovered_runner.calls == []


def test_concurrent_coordinators_use_one_durable_dispatch_claim() -> None:
    """同一 Escalation 的第二个协调器在首个 Analyst 进行时只能观察 pending，不得再发送。"""

    async def scenario() -> tuple[Any, Any, _BlockingAnalystRunner]:
        store, workspace, _lease, bundle = _seed_bundle(suffix="concurrent")
        runner = _BlockingAnalystRunner()
        first_task = asyncio.create_task(
            HighConflictEscalationCoordinator(
                store=store, analyst_runner=runner, clock=_now
            ).run_automatic(bundle, expected_workspace_version=workspace.version)
        )
        await runner.entered.wait()
        second = await HighConflictEscalationCoordinator(
            store=store, analyst_runner=runner, clock=_now
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
        runner.release.set()
        return await first_task, second, runner

    first, second, runner = asyncio.run(scenario())

    assert first.analysis is not None
    assert second.analysis is None
    assert second.outcome is None
    assert len(runner.calls) == 1


def test_concurrent_coordinators_use_one_durable_planner_dispatch_claim() -> None:
    """同一已验证 Analysis 的第二个 Coordinator 只能观察 Planner pending，不能重复发送。"""

    async def scenario() -> tuple[Any, Any, _BlockingPlannerRunner]:
        store, workspace, _lease, bundle = _seed_bundle(suffix="planner-concurrent")
        planner = _BlockingPlannerRunner()
        first_task = asyncio.create_task(
            HighConflictEscalationCoordinator(
                store=store,
                analyst_runner=_ScriptedAnalystRunner(),
                planner_runner=planner,
                clock=_now,
            ).run_automatic(bundle, expected_workspace_version=workspace.version)
        )
        await planner.entered.wait()
        second = await HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=planner,
            clock=_now,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
        planner.release.set()
        return await first_task, second, planner

    first, second, planner = asyncio.run(scenario())

    assert first.proposal is not None
    assert first.outcome is not None
    assert second.analysis is not None
    assert second.proposal is None
    assert second.outcome is None
    assert len(planner.calls) == 1


def test_restart_closes_persisted_proposal_without_resending_planner() -> None:
    """READY 写入短暂失败后，重启必须复用已持久化 Proposal 并只补写唯一终态。"""

    backing_store, workspace, _lease, bundle = _seed_bundle(suffix="planner-ready-loss")
    store = _ReadyOutcomeFailureStore(backing_store)
    first_planner = _ScriptedPlannerRunner()
    first = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=first_planner,
            clock=_now,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )
    restarted_planner = _ScriptedPlannerRunner()
    recovered = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=restarted_planner,
            clock=_now,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert first.analysis is not None
    assert first.proposal is not None
    assert first.outcome is None
    assert recovered.proposal == first.proposal
    assert recovered.outcome is not None
    assert recovered.outcome.status is MultiAgentOutcomeStatus.READY
    assert len(first_planner.calls) == 1
    assert restarted_planner.calls == []


def test_expired_planner_claim_closes_review_with_unlinked_degraded_outcome() -> None:
    """Planner 已发送后切到 REVIEW 时只能追加不携带 Analysis/Proposal 的降级审计闭合。"""

    instant = _now()
    store_clock = [instant]
    backing_store, workspace, lease, bundle = _seed_bundle(
        suffix="planner-review-close", store_clock=lambda: store_clock[0]
    )
    store = _ReadyOutcomeFailureStore(backing_store)
    first = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=_ScriptedPlannerRunner(),
            clock=lambda: instant,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )
    assert first.proposal is not None
    assert first.outcome is None

    # claim 的受控两秒观察窗结束后，运营才可推进 REVIEW；恢复不能在播后写 Proposal
    # 或 READY，只能写不含中间父链的审计终态。
    store_clock[0] = instant + timedelta(seconds=3)
    current = backing_store.get_workspace(bundle.live_session_id)
    backing_store.advance_view(
        bundle.live_session_id,
        target_view=WorkspaceView.REVIEW,
        expected_version=current.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=store_clock[0],
    )
    recovered = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=_ScriptedPlannerRunner(),
            clock=lambda: instant,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert recovered.proposal is None
    assert recovered.analysis is None
    assert recovered.outcome is not None
    assert recovered.outcome.status is MultiAgentOutcomeStatus.DEGRADED
    assert recovered.outcome.analysis_id is None
    assert recovered.outcome.proposal_id is None


def test_expired_planner_claim_race_rebuilds_review_timeout_outcome() -> None:
    """过期 Planner claim 在终态 CAS 时切到 REVIEW，必须同次闭合而不能返回半成品 Analysis。"""

    instant = _now()
    store_clock = [instant]
    backing_store, workspace, lease, bundle = _seed_bundle(
        suffix="planner-expired-review-race", store_clock=lambda: store_clock[0]
    )
    # 先按 Task 5 语义持久化唯一 Analysis，再手工建立已经离开进程的 Planner claim；
    # 这样本用例专门覆盖“非新 claim 且已过期”的恢复分支，不依赖第二次模型调用。
    first = asyncio.run(
        HighConflictEscalationCoordinator(
            store=backing_store,
            analyst_runner=_ScriptedAnalystRunner(),
            clock=lambda: instant,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )
    assert first.escalation is not None
    assert first.analysis is not None
    planner_coordinator = HighConflictEscalationCoordinator(
        store=backing_store,
        analyst_runner=_ScriptedAnalystRunner(),
        planner_runner=_ScriptedPlannerRunner(),
        clock=lambda: instant,
    )
    planner_task = planner_coordinator._build_planner_task(
        bundle, first.escalation, first.analysis
    )
    backing_store.claim_planner_dispatch(
        escalation_id=first.escalation.escalation_id,
        analysis_id=first.analysis.analysis_id,
        analysis_digest=first.analysis.analysis_digest,
        task_digest=planner_task.task_digest,
    )
    store_clock[0] = instant + timedelta(seconds=3)
    racing_store = _ReviewBeforeDegradedOutcomeStore(
        backing_store, lease=lease, review_now=store_clock[0]
    )

    recovered = asyncio.run(
        HighConflictEscalationCoordinator(
            store=racing_store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=_ScriptedPlannerRunner(),
            clock=lambda: instant,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert recovered.analysis is None
    assert recovered.proposal is None
    assert recovered.outcome is not None
    assert recovered.outcome.status is MultiAgentOutcomeStatus.DEGRADED
    assert recovered.outcome.failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT


def test_global_planner_budget_timeout_rebuilds_review_timeout_outcome() -> None:
    """全局 deadline 限制 Planner 时，超时不能被误记为模型错误或返回半成品 Analysis。"""

    instant = _now()
    store_clock = [instant]
    store, workspace, lease, bundle = _seed_bundle(
        suffix="planner-global-budget-review", store_clock=lambda: store_clock[0]
    )
    # 前六次保持零以让 Analyst 完整落库；第七次在 Planner 等待裁剪时保留一秒，
    # 第八次由 timeout handler 观察到五秒已耗尽。Store 视图迁移使用到期的 claim 时钟，
    # 使断言覆盖 D-150/D-152 的真实 LIVE->REVIEW 竞争闭合。
    monotonic_values = iter((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 4.0, 5.1))
    racing_store = _ReviewBeforeDegradedOutcomeStore(
        store, lease=lease, review_now=instant + timedelta(seconds=3)
    )
    planner = _CoordinatorDeadlinePlannerRunner()

    recovered = asyncio.run(
        HighConflictEscalationCoordinator(
            store=racing_store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=planner,
            clock=lambda: instant,
            monotonic_clock=lambda: next(monotonic_values),
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert len(planner.calls) == 1
    assert recovered.analysis is None
    assert recovered.proposal is None
    assert recovered.outcome is not None
    assert recovered.outcome.failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT


def test_live_to_review_race_rebuilds_unlinked_degraded_outcome_without_second_retry() -> None:
    """写降级终态时发生视图切换，Coordinator 必须在同次调用重试无父链闭合而非返回半成品。"""

    instant = _now()
    store_clock = [instant]
    backing_store, workspace, lease, bundle = _seed_bundle(
        suffix="planner-review-race", store_clock=lambda: store_clock[0]
    )
    ready_loss_store = _ReadyOutcomeFailureStore(backing_store)
    first = asyncio.run(
        HighConflictEscalationCoordinator(
            store=ready_loss_store,
            analyst_runner=_ScriptedAnalystRunner(),
            planner_runner=_ScriptedPlannerRunner(),
            clock=lambda: instant,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )
    assert first.escalation is not None
    assert first.analysis is not None
    assert first.proposal is not None
    assert first.outcome is None

    # Planner claim 已经过期，测试替身会在首次 DEGRADED 写入前把 Workspace 推进 REVIEW。
    store_clock[0] = instant + timedelta(seconds=3)
    racing_store = _ReviewBeforeDegradedOutcomeStore(
        backing_store, lease=lease, review_now=store_clock[0]
    )
    recovered = HighConflictEscalationCoordinator(
        store=racing_store,
        analyst_runner=_ScriptedAnalystRunner(),
        planner_runner=_ScriptedPlannerRunner(),
        clock=lambda: instant,
    )._persist_degraded(
        first.escalation,
        MultiAgentFailureCode.COORDINATOR_TIMEOUT,
        analysis=first.analysis,
        allow_review_terminalization=True,
    )

    assert recovered.analysis is None
    assert recovered.proposal is None
    assert recovered.outcome is not None
    assert recovered.outcome.status is MultiAgentOutcomeStatus.DEGRADED
    assert recovered.outcome.analysis_id is None
    assert recovered.outcome.proposal_id is None


def test_response_loss_claim_prevents_second_model_send_and_degrades_after_lease() -> None:
    """模型返回后两次写入都丢失时，过期 claim 必须降级而不是再次发送相同任务。"""

    instant = _now()
    clock = [instant]
    store, workspace, _lease, bundle = _seed_bundle(
        suffix="response-loss", store_clock=lambda: clock[0]
    )
    durable_store = _DispatchFailureStore(store)
    runner = _ScriptedAnalystRunner()
    first_coordinator = HighConflictEscalationCoordinator(
        store=durable_store, analyst_runner=runner, clock=lambda: instant
    )
    with pytest.raises(RuntimeError, match="outcome persistence loss"):
        asyncio.run(
            first_coordinator.run_automatic(
                bundle, expected_workspace_version=workspace.version
            )
        )

    # Coordinator 时钟保持旧值；只有 Store 自己的可信时钟前进，才能证明 claim
    # 过期判定不接受调用方注入的时间或租约来延长观察窗口。
    clock[0] = instant + timedelta(seconds=3)
    recovered = asyncio.run(
        HighConflictEscalationCoordinator(
            store=durable_store,
            analyst_runner=runner,
            clock=lambda: instant,
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert recovered.outcome is not None
    assert recovered.outcome.status is MultiAgentOutcomeStatus.DEGRADED
    assert len(runner.calls) == 1


def test_conflicting_runner_profile_is_rejected_before_model_dispatch() -> None:
    """同名同版本但 Prompt 不同的 Runner Profile 不能冒充冻结零 Skill Analyst。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="profile-mismatch")
    expected = build_evidence_analyst_profile()
    conflicting_prompt = expected.prompt_text + " altered"
    conflicting = SpecialistProfile.model_validate(
        {
            **expected.model_dump(mode="json"),
            "prompt_text": conflicting_prompt,
            "prompt_hash": hashlib.sha256(conflicting_prompt.encode("utf-8")).hexdigest(),
            "profile_digest": "",
        }
    )
    runner = _ScriptedAnalystRunner(profile=conflicting)
    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store, analyst_runner=runner, clock=_now
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.outcome is not None
    assert result.outcome.failure_code is MultiAgentFailureCode.ANALYST_INVALID_OUTPUT
    assert runner.calls == []


def test_existing_escalation_never_dispatches_after_workspace_leaves_live() -> None:
    """崩溃发生在 claim 前而直播已结束时，只保留升级事实，绝不能在 REVIEW 发送 Analyst。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="review-before-claim")
    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    escalation = EscalationRecord(
        escalation_id="phase16-escalation:automatic:bundle-review-before-claim",
        live_session_id=bundle.live_session_id,
        incident_id=bundle.incident_id,
        evidence_bundle_id=bundle.evidence_bundle_id,
        evidence_bundle_digest=snapshot.bundle_digest,
        idempotency_key="phase16-escalation:automatic:bundle-review-before-claim",
        mode=EscalationMode.AUTOMATIC,
        trigger_codes=(
            ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,
            ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED,
        ),
        created_at=_now(),
    )
    workspace = store.append_escalation(
        escalation, expected_workspace_version=workspace.version
    )
    review_lease = store.acquire_operator_lock(
        bundle.live_session_id, "operator-phase16-task5", 60, now=_now()
    )
    store.advance_view(
        bundle.live_session_id,
        target_view=WorkspaceView.REVIEW,
        expected_version=workspace.version,
        operator_id=review_lease.operator_id,
        fencing_token=review_lease.fencing_token,
        now=_now(),
    )
    runner = _ScriptedAnalystRunner()

    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store, analyst_runner=runner, clock=_now
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.selected is True
    assert result.escalation == escalation
    assert result.analysis is None
    assert result.outcome is None
    assert runner.calls == []


@pytest.mark.parametrize(
    ("suffix", "valid_backup_count", "include_noise", "pause_required", "expected_codes"),
    (
        (
            "backup-noise",
            2,
            True,
            False,
            ("MULTIPLE_VALID_BACKUPS", "AVAILABILITY_NOISE_HIGH"),
        ),
        (
            "backup-pause",
            2,
            False,
            True,
            ("MULTIPLE_VALID_BACKUPS", "RHYTHM_PAUSE_REQUIRED"),
        ),
        (
            "noise-pause",
            1,
            True,
            True,
            ("AVAILABILITY_NOISE_HIGH", "RHYTHM_PAUSE_REQUIRED"),
        ),
        (
            "all-three",
            2,
            True,
            True,
            (
                "MULTIPLE_VALID_BACKUPS",
                "AVAILABILITY_NOISE_HIGH",
                "RHYTHM_PAUSE_REQUIRED",
            ),
        ),
    ),
)
def test_automatic_selector_reconstructs_each_three_select_two_combination(
    suffix: str,
    valid_backup_count: int,
    include_noise: bool,
    pause_required: bool,
    expected_codes: tuple[str, ...],
) -> None:
    """任何两项或三项冻结信号均按稳定顺序升级，单项事件则留在默认单 Copilot 路由。"""

    store, workspace, _lease, bundle = _seed_bundle(
        suffix=suffix,
        valid_backup_count=valid_backup_count,
        include_availability_noise=include_noise,
        pause_required=pause_required,
    )
    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store, analyst_runner=_ScriptedAnalystRunner(), clock=_now
        ).run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert result.selected is True
    assert result.escalation is not None
    assert result.escalation.trigger_codes == expected_codes


def test_normal_ineligible_and_adversarial_bundles_never_invoke_analyst() -> None:
    """正常、对账阻断和进程内伪造 Bundle 都必须在模型边界前停止。"""

    runner = _ScriptedAnalystRunner()
    normal_store, normal_workspace, _normal_lease, normal_bundle = _seed_bundle(
        suffix="normal", include_availability_noise=False, pause_required=False
    )
    normal = asyncio.run(
        HighConflictEscalationCoordinator(
            store=normal_store, analyst_runner=runner, clock=_now
        ).run_automatic(normal_bundle, expected_workspace_version=normal_workspace.version)
    )

    ineligible_store, ineligible_workspace, _ineligible_lease, ineligible_bundle = _seed_bundle(
        suffix="ineligible", reconciliation_required=True
    )
    ineligible = asyncio.run(
        HighConflictEscalationCoordinator(
            store=ineligible_store, analyst_runner=runner, clock=_now
        ).run_automatic(ineligible_bundle, expected_workspace_version=ineligible_workspace.version)
    )

    # model_copy 可模拟同进程调用者绕过 Pydantic 构造的对象；协调器仍必须重新载入
    # 冻结 JSON，不能因为对象已经是 EvidenceBundle 类型就信任其内层快照。
    rejected = asyncio.run(
        HighConflictEscalationCoordinator(
            store=normal_store, analyst_runner=runner, clock=_now
        ).run_automatic(
            _ForgedBundleObject(), expected_workspace_version=normal_workspace.version
        )
    )

    assert normal.selected is False
    assert ineligible.selected is False
    assert rejected.selected is False
    assert runner.calls == []


def test_manual_escalation_requires_current_lease_and_reconstructs_one_server_signal() -> None:
    """人工请求只携带 Bundle 与 CAS/lease；服务端可用单项真实冲突成功分析，客户端不能选码。"""

    store, workspace, lease, bundle = _seed_bundle(
        suffix="manual", include_availability_noise=False, pause_required=True
    )
    runner = _ScriptedAnalystRunner()
    coordinator = HighConflictEscalationCoordinator(
        store=store, analyst_runner=runner, clock=_now
    )

    with pytest.raises(WorkspaceLeaseError):
        asyncio.run(
            coordinator.run_operator_requested(
                bundle,
                expected_workspace_version=workspace.version,
                operator_id=lease.operator_id,
                fencing_token=lease.fencing_token + 1,
            )
        )

    result = asyncio.run(
        coordinator.run_operator_requested(
            bundle,
            expected_workspace_version=workspace.version,
            operator_id=lease.operator_id,
            fencing_token=lease.fencing_token,
        )
    )

    assert result.escalation is not None
    assert result.escalation.mode.value == "OPERATOR_REQUESTED"
    assert result.escalation.trigger_codes == (ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED,)
    assert result.analysis is not None
    assert result.outcome is None
    assert len(runner.calls) == 1


def test_automatic_entry_never_dispatches_pending_manual_escalation() -> None:
    """自动入口只能观察人工升级事实，不能在没有当前人工租约时继续其模型阶段。

    人工路径允许一项真实冲突信号，而自动路径要求三选二。若自动入口在发现既有
    ``OPERATOR_REQUESTED`` 事实后继续协调，会把后续 Analyst 发送从操作员租约中
    脱离，既扩大模型调用也破坏人工授权边界。因此它只能返回持久化的 pending 事实。
    """

    store, workspace, lease, bundle = _seed_bundle(
        suffix="manual-automatic-ownership",
        include_availability_noise=False,
        pause_required=True,
    )
    manual = EscalationRecord(
        escalation_id=f"phase16-escalation:operator_requested:{bundle.evidence_bundle_id}",
        live_session_id=bundle.live_session_id,
        incident_id=bundle.incident_id,
        evidence_bundle_id=bundle.evidence_bundle_id,
        evidence_bundle_digest=EvidenceBundleSnapshot.model_validate(
            bundle.snapshot
        ).bundle_digest,
        idempotency_key=f"phase16-escalation:operator_requested:{bundle.evidence_bundle_id}",
        mode=EscalationMode.OPERATOR_REQUESTED,
        trigger_codes=(ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED,),
        operator_id=lease.operator_id,
        created_at=_now(),
    )
    after_manual = store.append_escalation(
        manual,
        expected_workspace_version=workspace.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    )
    runner = _ScriptedAnalystRunner()

    observed = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=runner,
            clock=_now,
        ).run_automatic(
            bundle,
            expected_workspace_version=after_manual.version,
        )
    )

    assert observed.selected is True
    assert observed.escalation == manual
    assert observed.analysis is None
    assert observed.proposal is None
    assert observed.outcome is None
    assert runner.calls == []
    assert store.list_conflict_analyses(bundle.live_session_id) == ()
    assert store.list_multi_agent_outcomes(bundle.live_session_id) == ()


def test_manual_escalation_rejects_existing_automatic_escalation() -> None:
    """Coordinator 最终观察到自动事实时，人工入口不得把它伪装为 manual replay。"""

    store, workspace, lease, bundle = _seed_bundle(suffix="automatic-manual-race")
    runner = _ScriptedAnalystRunner()
    coordinator = HighConflictEscalationCoordinator(
        store=store,
        analyst_runner=runner,
        clock=_now,
    )
    automatic = asyncio.run(
        coordinator.run_automatic(
            bundle,
            expected_workspace_version=workspace.version,
        )
    )

    assert automatic.escalation is not None
    assert automatic.escalation.mode is EscalationMode.AUTOMATIC
    with pytest.raises(WorkspaceConflictError, match="automatic escalation"):
        asyncio.run(
            coordinator.run_operator_requested(
                bundle,
                expected_workspace_version=store.get_workspace(
                    bundle.live_session_id
                ).version,
                operator_id=lease.operator_id,
                fencing_token=lease.fencing_token,
            )
        )
    assert len(runner.calls) == 1


def test_analyst_failure_persists_exactly_one_degraded_outcome_and_retries_without_model() -> None:
    """任一 Analyst 失败只写一条不含 Proposal 的降级终态，重试不得再次调用模型。"""

    store, workspace, _lease, bundle = _seed_bundle(suffix="failure")
    runner = _ScriptedAnalystRunner(failure=AgentResultStatus.MODEL_ERROR)
    coordinator = HighConflictEscalationCoordinator(
        store=store, analyst_runner=runner, clock=_now
    )

    first = asyncio.run(
        coordinator.run_automatic(bundle, expected_workspace_version=workspace.version)
    )
    retry = asyncio.run(
        coordinator.run_automatic(bundle, expected_workspace_version=workspace.version)
    )

    assert first.analysis is None
    assert first.outcome is not None
    assert first.outcome.status is MultiAgentOutcomeStatus.DEGRADED
    assert first.outcome.failure_code is MultiAgentFailureCode.ANALYST_MODEL_ERROR
    assert first.outcome.proposal_id is None
    assert retry.outcome == first.outcome
    assert len(store.list_multi_agent_outcomes(bundle.live_session_id)) == 1
    assert len(runner.calls) == 1
