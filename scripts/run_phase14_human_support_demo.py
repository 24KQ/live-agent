"""Phase 14 Task 12 的三场景人机协同 Demo 与 Acceptance 报告生成器。

Demo 只使用固定时钟、内存 Store 和既有受控领域服务，因此可以在没有 PostgreSQL、
淘宝 API 或外部模型的环境中重复回放。它故意把 ``DECISION_SUPPORT`` 作为演示路由，
同时把生产默认路由和经营写入边界写进输出，避免把“模型给出建议”误报为“系统自动
完成经营恢复”。真实模型没有在 Task 11 预检后重新运行时，阶段结论保持
``INCONCLUSIVE``。
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
import json
from pathlib import Path
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


if __package__ in {None, ""}:
    # 直接执行 scripts/*.py 时，解释器默认只把 scripts 放入 sys.path；
    # 这里仅补充仓库根目录，不改变业务运行时的模块搜索或路由配置。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.decision_support.commands import (
    DecisionExecutionContext,
    DecisionSupportCommandCompiler,
    OperatorDecisionDraft,
    OperatorModification,
)
from src.decision_support.formal_evaluation import (
    FormalEvaluationStatus,
    run_scripted_formal_rehearsal,
)
from src.decision_support.models import (
    DecisionKind,
    LiveSessionWorkspace,
    OperatorLease,
    WorkspaceView,
)
from src.decision_support.proposal import (
    DecisionOption,
    DecisionTiming,
    LiveDecisionProposal,
    ProductStrategy,
    ProposalStatus,
)
from src.decision_support.review_feedback import (
    InMemoryDecisionTraceResolver,
    InMemoryReviewFeedbackStore,
    ReviewFeedbackService,
)
from src.decision_support.sold_out_flow import (
    HumanGuidedSoldOutFlow,
    SoldOutFlowResult,
)
from src.decision_support.store import InMemoryDecisionSupportStore
from src.memory.candidate_store import (
    InMemoryMemoryCandidateStore,
    MemoryCandidate,
    MemoryCandidateStatus,
)
from src.memory.promotion_policy import PromotionPolicy
from src.plan_engine.event_state_machine import EventApplicationState, EventInboxState
from src.plan_engine.event_store import EventDelivery, InMemoryEventStore
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.preemption import PreemptionEvidenceRef, PreemptionResult, PreemptionStatus
from src.plan_engine.models import PlanNodeState
from src.specialist_runtime.models import EvidenceKind, EvidenceRef


DEMO_SESSION_ID = "live-session-p001-sold-out-v1"
DEMO_ROOM_ID = "room-phase14-demo"
DEMO_ROOT_PLAN_ID = "plan-root-phase14-demo"
DEMO_EVENT_ID = "event-phase14-demo-sold-out"
DEMO_INCIDENT_ID = f"incident:{DEMO_EVENT_ID}:{DEMO_ROOT_PLAN_ID}"
DEMO_NOW = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)
# 该数值来自 Task 11 的实时状态留痕；Task 12 自身不新增模型费用。
RECORDED_PHASE14_MODEL_COST_CNY = Decimal("0.042344")


class DemoStatus(StrEnum):
    """Demo/Acceptance 的阶段级结论，而不是生产流量结果。"""

    PASS = "PASS"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAIL = "FAIL"


class DemoResult(BaseModel):
    """Demo 输出的稳定 JSON 投影，字段只表达事实和门禁结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: DemoStatus
    live_session_id: str = Field(..., min_length=1)
    replay_live_session_id: str = Field(..., min_length=1)
    views: tuple[str, ...] = Field(..., min_length=3)
    replay_stable: bool
    route: str
    production_default_route: str
    automatic_protection_status: str
    operator_decision_required: bool
    operator_decision_kind: str
    operator_decision_evidence_ids: tuple[str, ...]
    compiled_command_id: str
    execution_command_submitted: bool
    memory_promotion_status: str
    memory_replay_status: str
    formal_evaluation_status: FormalEvaluationStatus
    formal_evaluation_reason_codes: tuple[str, ...]
    offline_evaluation_gate_passed: bool
    real_model_cost_cny: str
    safety_invariants: tuple[str, ...]


class _DemoProtectionCoordinator:
    """演示用只读保护替身，返回既有 Phase 12B 的 APPLIED 证据。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_next(self, *, root_plan_run_id: str, now: datetime) -> PreemptionResult:
        """记录一次确定性自动保护，不执行平台写操作。"""

        self.calls.append(root_plan_run_id)
        evidence = PreemptionEvidenceRef.create(
            event_id=DEMO_EVENT_ID,
            root_plan_run_id=root_plan_run_id,
            application_state=EventApplicationState.APPLIED,
            emergency_plan_run_id="plan-emergency-phase14-demo",
            applied_plan_version=2,
            final_suggestion_fact="售罄保护已完成，备品与主播提示等待运营决定",
        )
        return PreemptionResult(
            status=PreemptionStatus.APPLIED,
            event_id=DEMO_EVENT_ID,
            root_plan_run_id=root_plan_run_id,
            evidence_ref=evidence,
        )

    async def reconcile_waiting(self, *, event_id: str, root_plan_run_id: str, now: datetime) -> PreemptionResult:
        """Demo 不制造未知副作用；该端口仍保留严格只读形状。"""

        return PreemptionResult(
            status=PreemptionStatus.WAITING_RECONCILIATION,
            event_id=event_id,
            root_plan_run_id=root_plan_run_id,
        )


class _DemoCommandService:
    """记录命令调用次数，证明 Demo 没有自动提交经营恢复。"""

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, command: Any, *, now: datetime) -> Any:
        self.submitted += 1
        raise AssertionError("Demo 不得在没有人工提交动作时执行恢复命令")


class _DemoActiveMemoryPort:
    """最小 active-memory Port，复用 PromotionPolicy 的唯一写入协议。"""

    def __init__(self) -> None:
        self.entries: list[Any] = []

    def write_memory(self, entry: Any) -> str:
        """按 memory_key 幂等写入结构化记忆。"""

        for index, existing in enumerate(self.entries):
            if existing.memory_key == entry.memory_key:
                self.entries[index] = entry
                return "memory-phase14-demo"
        self.entries.append(entry)
        return "memory-phase14-demo"

    def list_memories(self, _anchor_id: str, _room_id: str | None = None) -> list[Any]:
        """只返回作用域内的已晋升记忆；Demo 只有一个 scope。"""

        return list(self.entries)

    def promotion_scope_lock(self, _anchor_id: str, _room_id: str | None = None):
        """内存 Demo 不需要跨进程锁，但保留生产 Port 的锁调用形状。"""

        return nullcontext()


def _workspace_store() -> InMemoryDecisionSupportStore:
    """创建固定身份的 PREPARE Workspace，并按真实 Store API 推进视图。"""

    store = InMemoryDecisionSupportStore()
    store.create_workspace(
        LiveSessionWorkspace(
            live_session_id=DEMO_SESSION_ID,
            run_key="run-phase14-demo",
            room_id=DEMO_ROOM_ID,
            trace_id="trace-phase14-demo",
            anchor_id="anchor-phase14-demo",
            root_plan_run_id=DEMO_ROOT_PLAN_ID,
            event_inbox_scope_id="event-inbox-phase14-demo",
            decision_trace_scope_id="decision-trace-phase14-demo",
            replay_scope_id="replay-phase14-demo",
            evaluation_scope_id="evaluation-phase14-demo",
        )
    )
    lease = store.acquire_operator_lock(DEMO_SESSION_ID, "operator-demo", 60, now=DEMO_NOW)
    store.advance_view(
        DEMO_SESSION_ID,
        target_view=WorkspaceView.LIVE,
        expected_version=1,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=DEMO_NOW,
    )
    store.release_operator_lock(
        DEMO_SESSION_ID,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=DEMO_NOW,
    )
    return store


def _event_store() -> InMemoryEventStore:
    """登记由既有 Event Inbox 验证的售罄事实，不伪造私有可信标记。"""

    event_store = InMemoryEventStore()
    event = InventoryFactEvent.create_sold_out(
        event_id=DEMO_EVENT_ID,
        room_id=DEMO_ROOM_ID,
        product_id="p001",
        observed_version=2,
        occurred_at=DEMO_NOW - timedelta(seconds=2),
        source="inventory-service",
    )
    provenance = VerifiedIngressProvenance(
        provenance_id="provenance-phase14-demo",
        profile_id="inventory-profile-v1",
        transport="kafka",
        topic="inventory.sold-out",
        source=event.source,
        received_at=DEMO_NOW - timedelta(seconds=1),
        payload_digest=event.payload_digest,
    )
    event_store.register_event(
        event,
        provenance,
        EventDelivery(
            occurrence_id="occurrence-phase14-demo",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=1,
            received_at=DEMO_NOW - timedelta(seconds=1),
        ),
    )
    return event_store


def _mark_event_applied(event_store: InMemoryEventStore) -> None:
    """模拟保护协调器提交后的 Inbox 事实，供第二次调用验证重放。"""

    claim = event_store.claim_next_for_room(
        "demo-replay-worker",
        room_id=DEMO_ROOM_ID,
        now=DEMO_NOW,
        lease_seconds=60,
    )
    assert claim is not None
    event_store.transition_inbox(
        DEMO_EVENT_ID,
        expected_state=EventInboxState.PROCESSING,
        target_state=EventInboxState.APPLIED,
        now=DEMO_NOW,
        worker_id="demo-replay-worker",
        fencing_token=claim.fencing_token,
    )


def _demo_proposal() -> tuple[LiveDecisionProposal, tuple[EvidenceRef, ...]]:
    """生成不含工具字段的结构化建议，供 Compiler 形成未提交命令。"""

    refs = tuple(
        EvidenceRef(
            kind=kind,
            evidence_id=f"demo-{kind.value.lower()}",
            source_version="1.0.0",
            digest=(f"{index:064x}"[-64:]),
            anchor_id="anchor-phase14-demo",
            room_id=DEMO_ROOM_ID,
        )
        for index, kind in enumerate((EvidenceKind.EVENT, EvidenceKind.PLAN, EvidenceKind.AUDIT), 1)
    )
    option = DecisionOption(
        option_id="switch-to-backup",
        product_strategy=ProductStrategy.SWITCH_TO_BACKUP,
        backup_product_id="p002",
        host_prompt="请运营确认备品后再恢复讲解。",
        timing=DecisionTiming.AFTER_OPERATOR_CONFIRMATION,
        risk_flags=("HUMAN_CONFIRMATION_REQUIRED", "INVENTORY_CONFLICT_REQUIRES_REVIEW"),
        evidence_refs=refs,
    )
    return (
        LiveDecisionProposal(
            proposal_id="proposal-phase14-demo",
            live_session_id=DEMO_SESSION_ID,
            incident_id=DEMO_INCIDENT_ID,
            trace_id="trace-phase14-demo",
            evidence_bundle_id="evidence-phase14-demo",
            status=ProposalStatus.READY,
            options=(option,),
            evidence_refs=refs,
        ),
        refs,
    )


def _compile_operator_decision() -> tuple[str, str, tuple[str, ...]]:
    """用真实 Compiler 生成 MODIFY 事实和命令，但不把命令交给 Runtime。"""

    proposal_view, refs = _demo_proposal()
    proposal = {
        "proposal_id": proposal_view.proposal_id,
        "live_session_id": proposal_view.live_session_id,
        "incident_id": proposal_view.incident_id,
        "evidence_bundle_id": proposal_view.evidence_bundle_id,
        "idempotency_key": "proposal-phase14-demo-idem",
        "proposal_key": "sold-out-response",
        "proposal_version": 1,
        "profile_id": "live_ops_decision_support",
        "profile_version": "1.0.0",
        "snapshot": proposal_view.model_dump(mode="json"),
        "created_at": DEMO_NOW,
    }
    from src.decision_support.models import Proposal

    persisted_proposal = Proposal.model_validate(proposal)
    lease = OperatorLease(
        live_session_id=DEMO_SESSION_ID,
        operator_id="operator-demo",
        fencing_token=1,
        lease_until=DEMO_NOW + timedelta(seconds=60),
    )
    compiled = DecisionSupportCommandCompiler().compile(
        proposal=persisted_proposal,
        draft=OperatorDecisionDraft(
            decision_id="decision-phase14-demo",
            proposal_id=proposal_view.proposal_id,
            expected_proposal_version=1,
            operator_id="operator-demo",
            decision_kind=DecisionKind.MODIFY,
            reason_code="OPERATOR_CONFIRMED_BACKUP",
            idempotency_key="decision-phase14-demo-idem",
            option_id="switch-to-backup",
            modification=OperatorModification(
                host_prompt="请先确认备品库存，再恢复讲解。",
                priority=60,
            ),
        ),
        lease=lease,
        execution_context=DecisionExecutionContext(
            plan_run_id=DEMO_ROOT_PLAN_ID,
            expected_plan_version=2,
            node_id="node-phase14-demo-recovery",
            expected_node_status=PlanNodeState.WAITING_APPROVAL,
        ),
        now=DEMO_NOW,
    )
    assert compiled.execution_command is not None
    assert compiled.plan_command is not None
    return (
        compiled.operator_decision.decision_kind.value,
        compiled.plan_command.command_id,
        tuple(reference.evidence_id for reference in refs),
    )


def _run_memory_loop() -> tuple[str, str]:
    """运行两条独立 Trace、规则资格、人工确认和幂等重放闭环。"""

    candidate_store = InMemoryMemoryCandidateStore()
    feedback_store = InMemoryReviewFeedbackStore()
    active_memory = _DemoActiveMemoryPort()
    traces = (
        {"trace_id": "trace-post-live-a", "anchor_id": "anchor-phase14-demo", "room_id": DEMO_ROOM_ID},
        {"trace_id": "trace-post-live-b", "anchor_id": "anchor-phase14-demo", "room_id": DEMO_ROOM_ID},
    )
    resolver = InMemoryDecisionTraceResolver(traces)
    service = ReviewFeedbackService(
        candidate_store=candidate_store,
        feedback_store=feedback_store,
        decision_trace_resolver=resolver,
        promotion_policy=PromotionPolicy(
            store=candidate_store,
            active_memory_port=active_memory,
            eligibility_store=feedback_store,
            decision_trace_resolver=resolver,
        ),
    )
    candidate = candidate_store.stage(
        MemoryCandidate(
            candidate_id="candidate-phase14-demo",
            idempotency_key="candidate-phase14-demo-idem",
            anchor_id="anchor-phase14-demo",
            room_id=DEMO_ROOM_ID,
            evidence_ids=("trace-post-live-a", "trace-post-live-b"),
            preferred_category="kitchen",
            preferred_tags=("profit",),
            preferred_product_ids=("p002",),
            confidence=Decimal("0.90"),
        )
    )
    eligible = service.evaluate_eligibility(
        command_id="eligibility-phase14-demo",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=("trace-post-live-a", "trace-post-live-b"),
        product_whitelist={"p002"},
    )
    result = service.confirm_promotion(
        command_id="confirm-phase14-demo",
        candidate_id=candidate.candidate_id,
        expected_version=eligible.version,
        operator_id="operator-demo",
    )
    replay = service.confirm_promotion(
        command_id="confirm-phase14-demo",
        candidate_id=candidate.candidate_id,
        expected_version=eligible.version,
        operator_id="operator-demo",
    )
    return result.status.value, replay.status.value


def run_demo(evaluation_root: Path) -> DemoResult:
    """运行完整三场景 Demo，返回可序列化且可重复比较的结果。"""

    workspace_store = _workspace_store()
    event_store = _event_store()
    protection = _DemoProtectionCoordinator()
    command_service = _DemoCommandService()
    flow = HumanGuidedSoldOutFlow(
        workspace_store=workspace_store,
        event_store=event_store,
        protection_coordinator=protection,
        command_service=command_service,
    )
    first: SoldOutFlowResult = asyncio.run(
        flow.handle_verified_event(
            event_id=DEMO_EVENT_ID,
            root_plan_run_id=DEMO_ROOT_PLAN_ID,
            now=DEMO_NOW,
        )
    )
    _mark_event_applied(event_store)
    replay = asyncio.run(
        flow.handle_verified_event(
            event_id=DEMO_EVENT_ID,
            root_plan_run_id=DEMO_ROOT_PLAN_ID,
            now=DEMO_NOW,
        )
    )
    replay_stable = (
        first.status == replay.status
        and first.incident_id == replay.incident_id
        and first.protection_status == replay.protection_status
        and len(protection.calls) == 1
    )

    lease = workspace_store.acquire_operator_lock(DEMO_SESSION_ID, "operator-demo", 60, now=DEMO_NOW)
    current = workspace_store.get_workspace(DEMO_SESSION_ID)
    workspace_store.advance_view(
        DEMO_SESSION_ID,
        target_view=WorkspaceView.REVIEW,
        expected_version=current.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=DEMO_NOW,
    )
    workspace = workspace_store.get_workspace(DEMO_SESSION_ID)
    decision_kind, command_id, evidence_ids = _compile_operator_decision()
    memory_status, memory_replay_status = _run_memory_loop()
    formal = run_scripted_formal_rehearsal(Path(evaluation_root))
    status = DemoStatus.INCONCLUSIVE
    if not replay_stable or first.status.value != "PROTECTED":
        status = DemoStatus.FAIL
    return DemoResult(
        status=status,
        live_session_id=workspace.live_session_id,
        replay_live_session_id=workspace.live_session_id,
        views=("PREPARE", "LIVE", "REVIEW"),
        replay_stable=replay_stable,
        route="DECISION_SUPPORT",
        production_default_route="DETERMINISTIC_ONLY",
        # Flow 的业务展示状态是 PROTECTED，Demo 的审计字段要落到底层
        # PreemptionStatus.APPLIED，避免把“已保护”与“保护已提交”混成一个概念。
        automatic_protection_status=(
            first.protection_status.value
            if first.protection_status is not None
            else first.status.value
        ),
        operator_decision_required=True,
        operator_decision_kind=decision_kind,
        operator_decision_evidence_ids=evidence_ids,
        compiled_command_id=command_id,
        execution_command_submitted=command_service.submitted > 0,
        memory_promotion_status=memory_status,
        memory_replay_status=memory_replay_status,
        formal_evaluation_status=formal.status,
        formal_evaluation_reason_codes=formal.reason_codes,
        offline_evaluation_gate_passed=formal.scripted_gate_passed,
        real_model_cost_cny=f"{RECORDED_PHASE14_MODEL_COST_CNY:.6f}",
        safety_invariants=(
            "no_operator_decision_no_recovery",
            "automatic_protection_is_deterministic",
            "agent_output_never_writes_active_memory",
            "production_default_route_is_deterministic_only",
        ),
    )


def render_demo_report(result: DemoResult) -> str:
    """渲染固定顺序的 Markdown 报告，便于提交后审计和字节级重放。"""

    payload = result.model_dump(mode="json")
    lines = [
        "# Phase 14 Human-Centered Decision Support Acceptance",
        "",
        "本报告由无外部依赖 Demo 生成；它不是生产 A/B，也不把人工对照或 ScriptedModel 结果冒充真实模型证据。",
        "",
        f"- Stage status: `{payload['status']}`",
        "- Final phase state: `AWAITING_PHASE_15_GATE`",
        f"- Route used by Demo: `{payload['route']}`",
        f"- Production default route: `{payload['production_default_route']}`",
        f"- Live session: `{payload['live_session_id']}`",
        f"- Views: `{', '.join(payload['views'])}`",
        f"- Replay stable: `{str(payload['replay_stable']).lower()}`",
        "",
        "## Business Loop",
        "",
        f"- Automatic protection: `{payload['automatic_protection_status']}`",
        f"- Operator decision: `{payload['operator_decision_kind']}`",
        f"- Operator decision evidence: `{', '.join(payload['operator_decision_evidence_ids'])}`",
        f"- Compiled command: `{payload['compiled_command_id']}`",
        f"- Execution command submitted by Demo: `{str(payload['execution_command_submitted']).lower()}`",
        f"- Memory promotion: `{payload['memory_promotion_status']}`",
        f"- Memory replay: `{payload['memory_replay_status']}`",
        "",
        "## Evaluation Gates",
        "",
        f"- Offline Scripted rehearsal gate: `{str(payload['offline_evaluation_gate_passed']).lower()}`",
        f"- Formal model status: `{payload['formal_evaluation_status']}`",
        f"- Formal reason codes: `{', '.join(payload['formal_evaluation_reason_codes'])}`",
        f"- Recorded Phase 14 model cost: `{payload['real_model_cost_cny']} CNY`",
        "",
        "## Safety Invariants",
        "",
    ]
    lines.extend(f"- `{item}`" for item in payload["safety_invariants"])
    lines.extend(
        [
            "",
            "由于本轮没有新的真实模型 smoke 证据，阶段结论保持 `INCONCLUSIVE`；生产默认路由不切换。",
            "",
        ]
    )
    return "\n".join(lines)


def write_acceptance_report(root: Path, result: DemoResult) -> Path:
    """把报告写到调用方指定目录，测试和 CLI 都使用同一个渲染函数。"""

    output = Path(root) / "phase-14-human-centered-decision-support-acceptance.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_demo_report(result), encoding="utf-8", newline="\n")
    return output


def main() -> int:
    """CLI 只运行确定性 Demo 并把报告落到仓库报告目录。"""

    repository_root = Path(__file__).resolve().parents[1]
    result = run_demo(repository_root / "evaluation")
    report_path = repository_root / "docs" / "superpowers" / "reports"
    write_acceptance_report(report_path, result)
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if result.status is not DemoStatus.FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
