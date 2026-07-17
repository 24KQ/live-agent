from __future__ import annotations

import pytest

from src.config.settings import Settings
from src.core.on_live_harness_agent_graph import (
    _execute_tool_node,
    _human_approval_decider,
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.decision_support.routing import DecisionSupportRoute, DecisionSupportRoutePolicy
from src.gateway.harness_dashboard_service import HarnessDashboardService
from src.gateway.harness_session_store import InMemoryHarnessSessionStore
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class _RecordingPlanner:
    """记录旧 Planner 是否在默认关闭路由下被错误调用。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self._fail = fail

    def plan_next_step(self, **_kwargs) -> OnLiveHarnessDecision:
        self.calls += 1
        if self._fail:
            raise RuntimeError("planner unavailable")
        return OnLiveHarnessDecision(
            thought="旧 Planner 请求执行售罄写入",
            action="call_tool",
            tool_name="handle_sold_out_event",
            arguments={"product_id": "p001"},
            risk_level="HIGH",
        )


class _RecordingExecutor:
    """记录新权限门是否错误放行旧 Harness 的经营写调用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, tool_name, arguments, room_id, trace_id, **_kwargs):
        self.calls.append(tool_name)
        return {
            "tool_name": tool_name,
            "status": "success",
            "summary": "unexpected legacy execution",
        }


def test_decision_support_route_defaults_to_deterministic_only() -> None:
    """Phase 14 上线前，生产默认路由不得启动旧 Planner 或新 Copilot。"""

    settings = Settings(_env_file=None)

    assert settings.decision_support_execution_route == "DETERMINISTIC_ONLY"
    assert DecisionSupportRoutePolicy.from_settings(settings).route is DecisionSupportRoute.DETERMINISTIC_ONLY


def test_decision_support_route_can_be_explicitly_enabled_and_is_frozen() -> None:
    """服务启动时复制路由值，运行中修改 Settings 不能改变既有实例。"""

    settings = Settings(
        _env_file=None,
        DECISION_SUPPORT_EXECUTION_ROUTE="DECISION_SUPPORT",
    )
    policy = DecisionSupportRoutePolicy.from_settings(settings)

    settings.decision_support_execution_route = "DETERMINISTIC_ONLY"

    assert policy.route is DecisionSupportRoute.DECISION_SUPPORT


def test_default_route_never_calls_old_planner_or_executor() -> None:
    """默认确定性路由只留事实控制面，不执行任何旧 Agent 决策。"""

    planner = _RecordingPlanner()
    executor = _RecordingExecutor()
    graph = build_on_live_harness_agent_graph(planner=planner, executor=executor)

    result = graph.invoke(
        create_initial_on_live_harness_state(
            room_id="room-phase14-default",
            trace_id="trace-phase14-default",
            inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
        )
    )

    assert planner.calls == 0
    assert executor.calls == []
    assert result["agent_status"] == "decision_support_disabled"
    assert result["pending_tool_call"] is None
    assert result["available_tool_names"] == []


def test_explicit_decision_support_rejects_governed_write_without_operator_decision() -> None:
    """旧 Planner 的人工 approval 不是 Phase 14 OperatorDecision，不能驱动经营写入。"""

    planner = _RecordingPlanner()
    executor = _RecordingExecutor()
    graph = build_on_live_harness_agent_graph(
        planner=planner,
        executor=executor,
        decision_support_route=DecisionSupportRoute.DECISION_SUPPORT,
    )

    result = graph.invoke(
        create_initial_on_live_harness_state(
            room_id="room-phase14-write",
            trace_id="trace-phase14-write",
            inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
        )
    )

    assert planner.calls == 1
    assert executor.calls == []
    assert result["agent_status"] == "operator_decision_required"
    assert result["pending_tool_call"] is None
    assert result["approval_request"] is None
    assert "OperatorDecision" in result["error"]


def test_decision_support_planner_failure_does_not_fallback_to_legacy_execution() -> None:
    """显式新路由失败必须留在当前路径，不能切回 Legacy Planner/Executor。"""

    planner = _RecordingPlanner(fail=True)
    executor = _RecordingExecutor()
    graph = build_on_live_harness_agent_graph(
        planner=planner,
        executor=executor,
        decision_support_route=DecisionSupportRoute.DECISION_SUPPORT,
    )

    result = graph.invoke(
        create_initial_on_live_harness_state(
            room_id="room-phase14-failure",
            trace_id="trace-phase14-failure",
            inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
        )
    )

    assert planner.calls == 1
    assert executor.calls == []
    assert result["agent_status"] == "degraded"
    assert "planner unavailable" in result["error"]


def test_default_decision_support_planner_without_key_is_degraded(monkeypatch) -> None:
    """生产默认 Planner 缺少模型凭据时不得返回 Phase 5F 规则建议。"""

    monkeypatch.setattr(
        "src.skills.on_live_harness_planner.get_settings",
        lambda: Settings(_env_file=None, LLM_API_KEY=""),
    )
    executor = _RecordingExecutor()
    graph = build_on_live_harness_agent_graph(
        executor=executor,
        decision_support_route=DecisionSupportRoute.DECISION_SUPPORT,
    )

    result = graph.invoke(
        create_initial_on_live_harness_state(
            room_id="room-phase14-missing-key",
            trace_id="trace-phase14-missing-key",
            inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
        )
    )

    assert executor.calls == []
    assert result["agent_status"] == "degraded"
    assert "fallback disabled" in result["error"]
    assert result["final_suggestion"] is None


def test_dashboard_freezes_explicit_decision_support_route_at_startup() -> None:
    """Dashboard 多次构图和恢复必须使用同一启动路由快照。"""

    settings = Settings(
        _env_file=None,
        DECISION_SUPPORT_EXECUTION_ROUTE="DECISION_SUPPORT",
    )
    planner = _RecordingPlanner()
    executor = _RecordingExecutor()
    service = HarnessDashboardService(
        store=InMemoryHarnessSessionStore(),
        settings=settings,
        use_postgres_checkpointer=False,
        planner=planner,
        executor=executor,
    )
    settings.decision_support_execution_route = "DETERMINISTIC_ONLY"

    status = service.start_session(
        room_id="room-phase14-dashboard",
        trace_id="trace-phase14-dashboard",
    )

    assert planner.calls == 1
    assert executor.calls == []
    assert status["agent_status"] == "operator_decision_required"
    assert status["pending_approval"] is False


def test_plain_graph_state_cannot_forge_operator_decision_authority() -> None:
    """普通 JSON state 即使伪造 decision ID，也不能恢复旧 interrupt 执行分支。"""

    route = _human_approval_decider(
        {
            "approval_decision": "approved",
            "operator_decision_id": "forged-by-caller",
        }
    )

    assert route == "write_audit"


def test_legacy_checkpoint_already_queued_for_execution_is_blocked_at_boundary() -> None:
    """升级前已排队到 execute_tool 的 checkpoint 仍不能绕过可信授权门。"""

    executor = _RecordingExecutor()
    result = _execute_tool_node(
        {
            "room_id": "room-phase14-old-checkpoint",
            "trace_id": "trace-phase14-old-checkpoint",
            "pending_tool_call": {
                "tool_name": "handle_sold_out_event",
                "arguments": {"product_id": "p001"},
            },
            "executed_tools": [],
            "completed_nodes": [],
        },
        executor,
        frozenset({"handle_sold_out_event"}),
        DecisionSupportRoute.DECISION_SUPPORT,
    )

    assert executor.calls == []
    assert result["agent_status"] == "operator_decision_required"
    assert result["pending_tool_call"] is None


def test_executor_type_error_after_call_is_not_retried() -> None:
    """执行器内部 TypeError 不能触发兼容式第二次经营调用。"""

    class _TypeErrorExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, **_kwargs):
            self.calls += 1
            raise TypeError("side effect may already have happened")

    executor = _TypeErrorExecutor()

    with pytest.raises(TypeError, match="side effect"):
        _execute_tool_node(
            {
                "room_id": "room-phase14-type-error",
                "trace_id": "trace-phase14-type-error",
                "pending_tool_call": {
                    "tool_name": "aggregate_danmaku_questions",
                    "arguments": {},
                },
                "executed_tools": [],
                "completed_nodes": [],
            },
            executor,
            frozenset(),
            DecisionSupportRoute.DECISION_SUPPORT,
        )

    assert executor.calls == 1
