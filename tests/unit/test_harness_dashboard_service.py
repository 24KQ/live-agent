from __future__ import annotations

from src.gateway.harness_dashboard_service import HarnessDashboardService
from src.gateway.harness_session_store import InMemoryHarnessSessionStore
from src.config.settings import Settings
from src.plan_engine.preemption import PreemptionEvidenceRef


class _RecordingExecutor:
    """记录 Dashboard 是否越过 PlanEngine 证据门禁执行工具。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, tool_name, arguments, room_id, trace_id, **kwargs):
        self.calls.append(tool_name)
        return {"tool_name": tool_name, "status": "success", "summary": "unexpected"}


class _AtomicTerminalStore(InMemoryHarnessSessionStore):
    """证明无 interrupt 的会话只能通过单次终态写入创建。"""

    def __init__(self) -> None:
        super().__init__()
        self.terminal_writes = 0

    def save_pending(self, record):
        raise AssertionError("terminal session must never pass through pending_human")

    def save_terminal(self, record):
        self.terminal_writes += 1
        return super().save_terminal(record)


def test_start_defaults_to_completed_deterministic_only_session() -> None:
    store = InMemoryHarnessSessionStore()
    service = HarnessDashboardService(store=store, use_postgres_checkpointer=False)

    status = service.start_session(room_id="room-dashboard-001", trace_id="trace-dashboard-start")

    assert status["trace_id"] == "trace-dashboard-start"
    assert status["status"] == "completed"
    assert status["pending_approval"] is False
    assert status["interrupt_payload"] == {}
    assert status["agent_status"] == "decision_support_disabled"
    assert store.get("trace-dashboard-start").status == "completed"


def test_default_completion_is_created_by_one_atomic_terminal_write() -> None:
    """进程故障不能在终态创建途中留下虚假的待审批记录。"""

    store = _AtomicTerminalStore()
    service = HarnessDashboardService(store=store, use_postgres_checkpointer=False)

    status = service.start_session(
        room_id="room-dashboard-atomic",
        trace_id="trace-dashboard-atomic",
    )

    assert store.terminal_writes == 1
    assert status["status"] == "completed"
    assert status["pending_approval"] is False


def test_legacy_approval_cannot_mutate_completed_default_session() -> None:
    store = InMemoryHarnessSessionStore()
    service = HarnessDashboardService(store=store, use_postgres_checkpointer=False)
    service.start_session(room_id="room-dashboard-001", trace_id="trace-dashboard-approve")

    status = service.submit_approval(
        trace_id="trace-dashboard-approve",
        room_id="room-dashboard-001",
        tool_name="handle_sold_out_event",
        decision="approved",
        operator_id="operator-dashboard",
        reason="确认售罄处理",
    )

    assert status["status"] == "completed"
    assert status["pending_approval"] is False
    assert status["approval_decision"] is None
    assert status["executed_tools"] == []
    assert status["observations"] == []
    assert status["agent_status"] == "decision_support_disabled"


def test_legacy_rejection_cannot_reopen_completed_default_session() -> None:
    store = InMemoryHarnessSessionStore()
    service = HarnessDashboardService(store=store, use_postgres_checkpointer=False)
    service.start_session(room_id="room-dashboard-001", trace_id="trace-dashboard-reject")

    status = service.submit_approval(
        trace_id="trace-dashboard-reject",
        room_id="room-dashboard-001",
        tool_name="handle_sold_out_event",
        decision="rejected",
        operator_id="operator-dashboard",
        reason="主播决定人工处理",
    )

    assert status["status"] == "completed"
    assert status["approval_decision"] is None
    assert status["executed_tools"] == []
    assert status["agent_status"] == "decision_support_disabled"


def test_mismatched_legacy_approval_is_noop_after_default_completion() -> None:
    store = InMemoryHarnessSessionStore()
    service = HarnessDashboardService(store=store, use_postgres_checkpointer=False)
    service.start_session(room_id="room-dashboard-001", trace_id="trace-dashboard-mismatch")

    status = service.submit_approval(
        trace_id="trace-dashboard-mismatch",
        room_id="room-dashboard-001",
        tool_name="wrong_tool",
        decision="approved",
        operator_id="operator-dashboard",
        reason="错误工具名",
    )

    assert status["status"] == "completed"
    assert status["error"] is None
    assert status["executed_tools"] == []


def test_dashboard_uses_frozen_plan_engine_route_with_evidence_input() -> None:
    """生产 Dashboard 构图消费启动配置，并提供 EvidenceRef 正向建议入口。"""

    store = InMemoryHarnessSessionStore()
    executor = _RecordingExecutor()
    settings = Settings(_env_file=None, SOLD_OUT_EXECUTION_ROUTE="PLAN_ENGINE")
    service = HarnessDashboardService(
        store=store,
        settings=settings,
        use_postgres_checkpointer=False,
        executor=executor,
    )
    evidence = PreemptionEvidenceRef.create(
        event_id="event-dashboard-plan-engine",
        root_plan_run_id="root-dashboard-plan-engine",
        application_state="APPLIED",
        emergency_plan_run_id="child-dashboard-plan-engine",
        applied_plan_version=2,
        final_suggestion_fact="已完成售罄处理，请切换备选商品",
    )

    status = service.start_session(
        room_id="room-dashboard-plan-engine",
        trace_id="trace-dashboard-plan-engine",
        preemption_evidence_refs=[evidence],
        final_suggestion_fact=evidence.final_suggestion_fact,
    )

    assert executor.calls == []
    assert status["agent_status"] == "evidence_only"
    assert status["final_suggestion"] == evidence.final_suggestion_fact
    assert store.get("trace-dashboard-plan-engine").latest_state[
        "sold_out_execution_route"
    ] == "PLAN_ENGINE"
