# -*- coding: utf-8 -*-

"""Phase 10 story demo."""

from __future__ import annotations
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.core.on_live_harness_agent_graph import build_on_live_harness_agent_graph, create_initial_on_live_harness_state
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class SP:
    def plan_next_step(self, **kw):
        obs = kw.get("observations", [])
        if not obs:
            return OnLiveHarnessDecision(
                thought="need backup",
                action="call_tool",
                tool_name="recommend_backup_product",
                arguments={"sold_out_product_id": "p001"},
                risk_level="MEDIUM",
            )
        if len(obs) == 1:
            return OnLiveHarnessDecision(
                thought="generate prompt",
                action="call_tool",
                tool_name="generate_on_live_prompt",
                arguments={"product_id": "p002"},
                risk_level="HIGH",
            )
        return OnLiveHarnessDecision(
            thought="done",
            action="final_answer",
            final_suggestion="Explain p001 sold out, highlight p002 at 169 yuan.",
            risk_level="LOW",
        )


def _kv(k, v, i=4):
    print(" " * i + "* " + str(k) + ": " + str(v))


def main():
    fast = "--fast" in sys.argv
    print()
    print("  " + "=" * 60)
    print("  [PLAY] LiveAgent Agent Story Demo")
    print("  " + "=" * 60)
    planner = SP()
    graph = build_on_live_harness_agent_graph(planner=planner)
    state = create_initial_on_live_harness_state(
        room_id="room-story", trace_id="trace-story",
        danmaku_summary=[{"keyword":"price","count":15}],
        inventory_alerts=[{"product_id":"p001","severity":"sold_out"}],
        current_product={"product_id":"p001","title":"Summer dress","price":129.0},
        trust_score=0.8,
    )
    _kv("Room", state["room_id"])
    result = graph.invoke(state)
    _kv("Decision 1", result.get("agent_status"))
    if not fast: time.sleep(0.5)
    result2 = graph.invoke(result)
    _kv("Decision 2", result2.get("agent_status"))
    ptc = result2.get("pending_tool_call"); _kv("Pending", str(ptc.get("tool_name","-") if ptc else "-"))
    if not fast: time.sleep(0.5)
    result2["approval_decision"] = "approved"
    result3 = graph.invoke(result2)
    _kv("Result", str(result3.get("tool_result",{})))
    if not fast: time.sleep(0.5)
    result4 = graph.invoke(result3)
    _kv("Final", result4.get("final_suggestion"))
    _kv("Audit", result4.get("audit_status"))
    print()
    print("  [PLAY] Complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())