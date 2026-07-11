from __future__ import annotations

from src.core.agent_evaluation import AgentRuleEvaluator
from src.core.agent_replay import AgentReplay, ReplayTimelineItem


def _replay(items: list[ReplayTimelineItem]) -> AgentReplay:
    return AgentReplay(
        trace_id="trace-eval-001",
        graph_version="harness-v1",
        replay_fidelity="checkpoint",
        timeline=items,
    )


def test_rule_evaluator_passes_safe_approved_tool_path() -> None:
    replay = _replay(
        [
            ReplayTimelineItem(sequence=1, node_name="human_approval_interrupt", phase="on_live", status="pending"),
            ReplayTimelineItem(
                sequence=2,
                node_name="execute_tool",
                phase="on_live",
                status="completed",
                tool_call={"tool_name": "handle_sold_out_event", "risk_level": "HIGH"},
                approval={"decision": "approved"},
                evidence_ids=["audit-001"],
            ),
            ReplayTimelineItem(sequence=3, node_name="write_audit", phase="on_live", status="completed"),
        ]
    )

    result = AgentRuleEvaluator().evaluate(replay)

    assert result.verdict == "PASS"
    assert result.coverage_percent >= 80
    assert result.overall_score >= 80
    assert result.severe_violations == []


def test_rule_evaluator_fails_high_risk_tool_without_approval() -> None:
    replay = _replay(
        [
            ReplayTimelineItem(
                sequence=1,
                node_name="execute_tool",
                phase="on_live",
                status="completed",
                tool_call={"tool_name": "handle_sold_out_event", "risk_level": "HIGH"},
                evidence_ids=["audit-001"],
            )
        ]
    )

    result = AgentRuleEvaluator().evaluate(replay)

    assert result.verdict == "FAIL"
    assert result.overall_score <= 40
    assert any("高风险工具未经人审批准" in item for item in result.severe_violations)


def test_rule_evaluator_warns_when_optional_dimensions_missing() -> None:
    replay = _replay(
        [
            ReplayTimelineItem(sequence=1, node_name="load_context", phase="on_live", status="completed"),
            ReplayTimelineItem(sequence=2, node_name="write_audit", phase="on_live", status="completed"),
        ]
    )

    result = AgentRuleEvaluator().evaluate(replay)

    assert result.verdict == "WARN"
    assert result.coverage_percent < 80
    assert "建议语义质量" in {score.dimension for score in result.dimension_scores}
