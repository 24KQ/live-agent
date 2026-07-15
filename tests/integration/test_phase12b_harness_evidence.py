"""Phase 12B PlanEngine 路由下 Harness 证据消费集成测试。"""

from __future__ import annotations

from typing import Any

from src.core.on_live_harness_agent_graph import (
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.plan_engine.preemption import PreemptionEvidenceRef, SoldOutExecutionRoute
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class _HighRiskSoldOutPlanner:
    """模拟模型试图重复售罄写，验证 Graph 路由边界而非模型能力。"""

    def plan_next_step(self, **_: Any) -> OnLiveHarnessDecision:
        return OnLiveHarnessDecision(
            thought="尝试直接处理售罄",
            action="call_tool",
            tool_name="handle_sold_out_event",
            arguments={"product_id": "p001"},
            risk_level="HIGH",
        )


class _RecordingExecutor:
    """任何执行调用都记录下来，测试要求 PlanEngine 路由下为空。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, tool_name: str, arguments: dict[str, Any], room_id: str, trace_id: str, **_: Any) -> dict[str, Any]:
        self.calls.append(tool_name)
        return {"tool_name": tool_name, "status": "success", "summary": "unexpected"}


def test_harness_consumes_preemption_evidence_without_duplicate_sold_out_write() -> None:
    """Harness 只输出持久化建议事实，审计状态也保留 EvidenceRef 和路由。"""

    executor = _RecordingExecutor()
    evidence = PreemptionEvidenceRef.create(
        event_id="event-harness-integration-001",
        root_plan_run_id="root-harness-integration-001",
        application_state="APPLIED",
        emergency_plan_run_id="child-harness-integration-001",
        applied_plan_version=2,
        final_suggestion_fact="已完成售罄处理，请切换备选商品",
    )
    graph = build_on_live_harness_agent_graph(
        planner=_HighRiskSoldOutPlanner(),
        executor=executor,
        sold_out_execution_route=SoldOutExecutionRoute.PLAN_ENGINE,
    )
    result = graph.invoke(
        create_initial_on_live_harness_state(
            room_id="room-integration-001",
            trace_id="trace-harness-integration-001",
            inventory_alerts=[{"product_id": "p001", "event_type": "SOLD_OUT"}],
            preemption_evidence_refs=[evidence],
            final_suggestion_fact=evidence.final_suggestion_fact,
        )
    )

    assert executor.calls == []
    assert result["agent_status"] == "evidence_only"
    assert result["audit_payload"]["result_payload"]["sold_out_execution_route"] == "PLAN_ENGINE"
    assert result["audit_payload"]["result_payload"]["preemption_evidence_refs"][0]["event_id"] == evidence.event_id
