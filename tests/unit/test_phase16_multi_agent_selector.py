"""Phase 16 Task 5 高冲突选择与 EvidenceAnalystAgent 协调器的 RED/GREEN 契约。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Callable

import pytest

from src.decision_support.models import (
    ConflictAnalysisCode,
    EscalationMode,
    EscalationRecord,
    Incident,
    LiveSessionWorkspace,
    MultiAgentFailureCode,
    MultiAgentOutcomeStatus,
    WorkspaceView,
)
from src.decision_support.evidence import EvidenceBundleSnapshot
from src.decision_support.multi_agent import (
    HighConflictEscalationCoordinator,
    build_evidence_analyst_profile,
)
from src.decision_support.store import (
    InMemoryDecisionSupportStore,
    WorkspaceLeaseError,
)
from src.specialist_runtime.models import (
    AgentFailure,
    AgentResult,
    AgentResultStatus,
    AgentTask,
)
from src.specialist_runtime.profiles import SpecialistProfile
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
