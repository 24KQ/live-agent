# -*- coding: utf-8 -*-
"""Phase 10: LiveAgent End-to-End Agent Story Demo.

A walk-through of a live-streaming agent scenario:
  Product A hot-selling -> danmaku inflow -> inventory alert ->
  Harness Agent decision -> human approval interrupt ->
  resume -> final suggestion -> post-live review -> evaluation.

No external dependencies (no PostgreSQL, Kafka, real LLM).
Run: python scripts/run_story_demo.py
  or: python scripts/run_all.py story
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.on_live_harness_agent_graph import (
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


def _bar(c="=", w=58):
    return c * w


def _act(n, title, tag="SCENE"):
    print()
    print("  " + _bar())
    print("  [%s]  Act %d: %s" % (tag, n, title))
    print("  " + _bar())


def _kv(k, v, i=4):
    print(" " * i + "* " + str(k) + ":  " + str(v))


def _box(title, items):
    w = max([len(l) for l in items] + [len(title)], default=40) + 4
    line = "+-" + _bar("-", w) + "-+"
    print(line)
    print("| " + title + " " * (w - len(title)) + " |")
    print("| " + " " * w + " |")
    for item in items:
        print("| " + item + " " * (w - len(item)) + " |")
    print("| " + " " * w + " |")
    print(line)


def _wait(sec=0.8):
    time.sleep(sec)


class StoryPlanner:
    def __init__(self):
        self._phase = 0

    def plan_next_step(self, **kwargs):
        obs = kwargs.get("observations", [])
        self._phase += 1
        if not obs:
            return OnLiveHarnessDecision(
                thought="Product p001 sold out, need to find a backup.",
                action="call_tool",
                tool_name="recommend_backup_product",
                arguments={"sold_out_product_id": "p001"},
                final_suggestion=None,
                risk_level="MEDIUM",
            )
        if self._phase == 2:
            return OnLiveHarnessDecision(
                thought="Backup found. Generate host script for the transition.",
                action="call_tool",
                tool_name="generate_on_live_prompt",
                arguments={"product_id": "p002", "context": "p001 sold out"},
                final_suggestion=None,
                risk_level="HIGH",
            )
        return OnLiveHarnessDecision(
            thought="All tools done. Compose final suggestion.",
            action="final_answer",
            final_suggestion=(
                "Explain p001 is sold out. "
                "Highlight backup p002 at 169 yuan with exclusive gift offers. "
                "Guide viewers to the new product link."
            ),
            risk_level="LOW",
        )


def _mock_danmaku():
    return {
        "total": 45,
        "keywords": ["price", "stock", "size", "coupon", "shipping"],
        "price_ratio": 0.56,
        "sentiment": "neutral_to_negative",
        "top_questions": [
            ("Too expensive", 15, "price"),
            ("Any coupons?", 10, "price"),
            ("Size guide?", 8, "fit"),
            ("In stock?", 7, "stock"),
            ("Shipping?", 5, "logistics"),
        ],
    }


def _mock_alert():
    return [{"product_id": "p001", "severity": "sold_out",
            "message": "Product p001 (Summer Dress) is now sold out."}]


def _mock_review():
    return {
        "total_decisions": 3,
        "adoption_rate": 0.67,
        "accuracy_rate": 1.0,
        "issues_found": 0,
        "llm_summary": (
            "This session made 3 decisions with 67% adoption and 100% accuracy. "
            "The inventory alert triggered a correct backup product recommendation. "
            "The high-risk prompt generation went through human approval as required."
        ),
    }


def _mock_evaluation():
    return {
        "overall_score": 94.5,
        "coverage_pct": 85.0,
        "verdict": "PASS",
        "violations": 0,
        "dimensions": [
            ("state_completeness", 15.0, 15),
            ("tool_selection", 14.5, 15),
            ("security_compliance", 25.0, 25),
            ("human_approval", 15.0, 15),
            ("execution_efficiency", 10.0, 10),
            ("semantic_quality", 7.0, 10),
            ("business_impact", 8.0, 10),
        ],
    }


def main():
    fast = "--fast" in sys.argv

    # Title card
    print()
    print("  " + _bar("="))
    print("  LiveAgent -- Agent Story Demo")
    print("  " + _bar("="))
    print("  A walk-through of a live-streaming agent session:")
    print("  product hot-selling -> danmaku -> inventory alert ->")
    print("  agent decision -> human approval -> review -> eval")
    if not fast:
        _wait(1.5)
    # Act 1
    _act(1, "Scene Setup")
    _kv("Host", "Summer Dress Live Show")
    _kv("Product A", "Summer Dress (p001)")
    _kv("Original Price", "299 yuan")
    _kv("After Coupon", "129 yuan")
    _kv("Danmaku Volume", "45 messages in first 5 min")
    _kv("Audience Sentiment", "Price-sensitive, neutral to negative")
    if not fast:
        _wait(1)
    # Act 2
    _act(2, "Danmaku Inflow", "CHAT")
    danmaku = _mock_danmaku()
    _kv("Total Messages", danmaku["total"])
    _kv("Price-related", str(int(danmaku["price_ratio"] * 100)) + "%")
    _kv("Keywords", ", ".join(danmaku["keywords"]))
    _kv("Sentiment", danmaku["sentiment"])
    _box("Top Questions",
          ["  " + str(q[1]) + "x " + str(q[0]) for q in danmaku["top_questions"]])
    if not fast:
        _wait(1.5)
    # Act 3
    _act(3, "Inventory Alert", "WARN")
    alert = _mock_alert()
    _kv("Product", alert[0]["product_id"])
    _kv("Severity", alert[0]["severity"])
    _kv("Message", alert[0]["message"])
    _kv("Impact", "Cannot sell p001. Must recommend backup or adjust strategy.")
    if not fast:
        _wait(1)
    # Act 4
    _act(4, "Harness Agent Start", "BOT")
    planner = StoryPlanner()
    graph = build_on_live_harness_agent_graph(planner=planner)
    state = create_initial_on_live_harness_state(
        room_id="room-story-demo", trace_id="trace-story-demo",
        danmaku_summary=[
            {"keyword": "price", "count": 15, "intent": "price"},
            {"keyword": "coupon", "count": 10, "intent": "price"},
            {"keyword": "size", "count": 8, "intent": "fit"},
        ],
        inventory_alerts=alert,
        current_product={"product_id": "p001", "title": "Summer Dress", "price": 129.0},
        trust_score=0.8,
        max_iterations=5,
    )
    _kv("Room", state["room_id"])
    _kv("Trace ID", state["trace_id"])
    _kv("Product", state["current_product"]["title"] + " (" + state["current_product"]["product_id"] + ")")
    _kv("Trust Score", str(state["trust_score"]))
    _kv("Max Iterations", str(state["max_iterations"]))
    if not fast:
        _wait(1)
    # Act 5
    _act(5, "Decision 1: Recommend Backup Product", "REFRESH")
    _kv("Thought", "Product p001 sold out, need to find a backup.")
    _kv("Tool", "recommend_backup_product")
    _kv("Args", "sold_out_product_id=p001")
    _kv("Risk Level", "MEDIUM -> auto execute")
    if not fast:
        _wait(0.5)
    result1 = graph.invoke(state)
    _kv("Agent Status", result1.get("agent_status", "?"))
    tools_done = [t.get("tool_name") for t in (result1.get("executed_tools") or [])]
    _kv("Tool Executed", str(tools_done))
    tool_r = result1.get("tool_result") or {}
    _kv("Tool Result", tool_r.get("summary", "N/A"))
    if not fast:
        _wait(1.5)
    # Act 6
    _act(6, "Decision 2: Generate Host Script", "STOP")
    _kv("Thought", "Backup found. Generate host script for the transition.")
    _kv("Tool", "generate_on_live_prompt")
    _kv("Args", "product_id=p002, context=p001 sold out")
    _kv("Risk Level", "HIGH -> requires human approval")
    if not fast:
        _wait(0.5)
    result2 = graph.invoke(result1)
    _kv("Agent Status", result2.get("agent_status", "?"))
    ptc = result2.get("pending_tool_call") or {}
    print()
    _box("Human Approval Request", [
        "  Trace ID:    " + state["trace_id"],
        "  Room:        " + state["room_id"],
        "  Tool:        " + str(ptc.get("tool_name", "?")),
        "  Risk:        HIGH",
        "  Args:        " + str(ptc.get("arguments", {})),
        "  Context:     " + str(result2.get("context_summary", "?")),
        "",
        "  >>> Decision: approve / reject",
    ])
    if not fast:
        _wait(1.5)
    # Act 7
    _act(7, "Human Approval: Approved", "OK")
    _kv("Operator", "operator-demo")
    _kv("Decision", "approved")
    _kv("Reason", "p001 is sold out. Must guide viewers to backup p002.")
    if not fast:
        _wait(0.5)
    result2["approval_decision"] = "approved"
    result2["approval_operator_id"] = "operator-demo"
    result2["approval_reason"] = "p001 sold out, guide to backup"
    result3 = graph.invoke(result2)
    _kv("Tool Executed", str(result3.get("tool_result", {}).get("tool_name", "?")))
    _kv("Tool Status", str(result3.get("tool_result", {}).get("status", "?")))
    if not fast:
        _wait(1)
    # Act 8
    _act(8, "Agent Final Suggestion", "TARGET")
    result4 = graph.invoke(result3)
    _kv("Suggestion", result4.get("final_suggestion", "N/A"))
    _kv("Agent Status", result4.get("agent_status", "?"))
    _kv("Iterations Used", str(result4.get("iteration", 0)))
    _kv("Audit Status", result4.get("audit_status", "?"))
    _kv("Audit ID", str(result4.get("audit_ids", [])))
    if not fast:
        _wait(1)
    # Act 9
    _act(9, "Post-Live Review Summary", "CHART")
    review = _mock_review()
    _kv("Total Decisions", review["total_decisions"])
    _kv("Adoption Rate", str(int(review["adoption_rate"] * 100)) + "%")
    _kv("Accuracy Rate", str(int(review["accuracy_rate"] * 100)) + "%")
    _kv("Issues Found", review["issues_found"])
    _box("LLM Summary", [review["llm_summary"]])
    if not fast:
        _wait(1.5)
    # Act 10
    _act(10, "Agent Evaluation Report", "CHART")
    ev = _mock_evaluation()
    _kv("Overall Score", str(ev["overall_score"]) + " / 100")
    _kv("Coverage", str(ev["coverage_pct"]) + "%")
    _kv("Verdict", ev["verdict"])
    _kv("Violations", str(ev["violations"]) + " (none)")
    _box("Dimension Scores",
          ["  " + d[0] + ":  " + str(d[1]) + "/" + str(d[2]) for d in ev["dimensions"]])
    if not fast:
        _wait(0.5)
    print()
    print("  " + _bar("="))
    print("  [DONE] Story Demo Complete")
    print("  " + _bar("="))
    print()
    print("  What you just witnessed:")
    print()
    print("    %-25s  %s" % ("Stage", "Outcome"))
    print("    %-25s  %s" % ("-" * 25, "-" * 30))
    print("    %-25s  %s" % ("Danmaku Aggregation", "45 msgs, 56% price-related"))
    s1 = result1.get("agent_status", "?")
    print("    %-25s  %s" % ("Decision 1 (MEDIUM)", "tool=recommend_backup, status=" + str(s1)))
    s2 = result2.get("agent_status", "?")
    print("    %-25s  %s" % ("Decision 2 (HIGH)", "tool=generate_prompt, status=" + str(s2)))
    print("    %-25s  %s" % ("Human Approval", "approved"))
    fs_val = result4.get("final_suggestion", "N/A") or "see above"
    print("    %-25s  %s" % ("Final Suggestion", str(fs_val)[:50]))
    audit_st = result4.get("audit_status", "?")
    print("    %-25s  %s" % ("Audit", str(audit_st)))
    print("    %-25s  %s" % ("Evaluation Score", str(ev["overall_score"]) + "/100 PASS"))
    print()
    print("  Next steps:")
    print("    * python scripts/run_all.py server    # Start the Web dashboard")
    print("    * python scripts/run_all.py daemon    # Start Kafka danmaku consumer")
    print("    * python scripts/run_all.py simulator # Start danmaku simulator")
    print()
    print("  " + _bar("="))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())