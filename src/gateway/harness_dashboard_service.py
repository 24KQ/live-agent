# -*- coding: utf-8 -*-
"""Phase 6C Web 副屏 Harness Agent 服务层。

该服务把 Web 请求转换成 LangGraph Harness Agent 的启动和恢复动作：
1. start_session 运行 graph 到 interrupt，并把 pending 审批写入 session store。
2. submit_approval 使用同一 trace_id/thread_id 通过 Command(resume=...) 恢复 graph。
3. 所有返回值都整理成前端稳定消费的状态 payload。
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from src.config.settings import Settings, get_settings
from src.core.langgraph_checkpoint import create_postgres_checkpointer, initialize_postgres_checkpointer
from src.core.on_live_harness_agent_graph import (
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.gateway.harness_session_store import (
    HarnessSessionNotFoundError,
    HarnessSessionRecord,
    InMemoryHarnessSessionStore,
    PostgresHarnessSessionStore,
)
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class DashboardHighRiskPlanner:
    """副屏演示用确定性 planner。

    Phase 6C 的重点是 Web 人审闭环，不依赖真实 LLM。该 planner 固定制造一个
    `handle_sold_out_event` 高风险工具调用，便于稳定展示 interrupt/resume。
    """

    def plan_next_step(self, **kwargs: Any) -> OnLiveHarnessDecision:
        observations = kwargs.get("observations", [])
        if observations:
            return OnLiveHarnessDecision(
                thought="高风险工具已通过人审执行，生成最终播中建议。",
                action="final_answer",
                final_suggestion="建议主播说明当前商品已售罄，并切换到备用讲解节奏。",
                risk_level="LOW",
            )
        return OnLiveHarnessDecision(
            thought="检测到售罄告警，需要处理高风险售罄事件。",
            action="call_tool",
            tool_name="handle_sold_out_event",
            arguments={"product_id": "p001"},
            risk_level="HIGH",
        )


class DashboardDemoExecutor:
    """副屏演示用工具执行器。

    真实平台 API 尚未接入，本执行器只返回结构化 observation，用于证明 approved
    后才会执行工具；rejected 路径不会调用到这里。
    """

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
        **_: Any,
    ) -> dict[str, Any]:
        return {
            "tool_name": tool_name,
            "status": "success",
            "summary": f"{tool_name} approved and executed for {room_id}",
            "arguments": arguments,
            "trace_id": trace_id,
        }


class HarnessDashboardService:
    """Web 副屏使用的 Harness Agent 门面。"""

    def __init__(
        self,
        *,
        store: Any | None = None,
        settings: Settings | None = None,
        use_postgres_checkpointer: bool = True,
        planner: Any | None = None,
        executor: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._store = store or PostgresHarnessSessionStore(self._settings)
        self._use_postgres_checkpointer = use_postgres_checkpointer
        self._planner = planner or DashboardHighRiskPlanner()
        self._executor = executor or DashboardDemoExecutor()
        self._memory_checkpointers: dict[str, InMemorySaver] = {}
        if hasattr(self._store, "initialize_schema"):
            self._store.initialize_schema()

    def start_session(
        self,
        *,
        room_id: str,
        trace_id: str | None = None,
        anchor_id: str | None = "anchor-demo",
    ) -> dict[str, Any]:
        trace = trace_id or f"trace-dashboard-{uuid4()}"
        state = create_initial_on_live_harness_state(
            room_id=room_id,
            trace_id=trace,
            anchor_id=anchor_id,
            inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
            current_product={"product_id": "p001", "name": "演示商品", "inventory": 0},
        )

        with self._checkpointer_context(trace) as checkpointer:
            graph = build_on_live_harness_agent_graph(
                planner=self._planner,
                executor=self._executor,
                checkpointer=checkpointer,
            )
            result = graph.invoke(state, config=self._graph_config(trace))

        interrupt_payload = self._extract_interrupt_payload(result)
        if interrupt_payload:
            record = HarnessSessionRecord(
                trace_id=trace,
                room_id=room_id,
                anchor_id=anchor_id,
                status="pending_human",
                approval_request=interrupt_payload,
                interrupt_payload=interrupt_payload,
                latest_state={
                    "agent_status": "pending_human",
                    "completed_nodes": ["load_context", "pre_reasoning_hook", "agent_reasoning", "route_agent_decision", "pre_tool_call_hook", "route_tool_policy"],
                    "executed_tools": [],
                    "observations": [],
                    "final_suggestion": None,
                },
            )
            return self._store.save_pending(record).to_status_payload()

        status = "completed" if result.get("agent_status") != "error" else "error"
        record = HarnessSessionRecord(
            trace_id=trace,
            room_id=room_id,
            anchor_id=anchor_id,
            status=status,
            latest_state=dict(result),
            audit_status=result.get("audit_status"),
            audit_ids=result.get("audit_ids", []),
            decision_trace_ids=result.get("decision_trace_ids", []),
        )
        return self._store.save_pending(record).to_status_payload()

    def get_status(self, trace_id: str) -> dict[str, Any]:
        return self._store.get(trace_id).to_status_payload()

    def latest_for_room(self, room_id: str) -> dict[str, Any]:
        records = self._store.latest_for_room(room_id, limit=1)
        if not records:
            raise HarnessSessionNotFoundError(room_id)
        return records[0].to_status_payload()

    def submit_approval(
        self,
        *,
        trace_id: str,
        room_id: str,
        tool_name: str,
        decision: str,
        operator_id: str,
        reason: str,
    ) -> dict[str, Any]:
        current = self._store.get(trace_id)
        if current.status in {"completed", "rejected"}:
            return current.to_status_payload()

        try:
            with self._checkpointer_context(trace_id) as checkpointer:
                graph = build_on_live_harness_agent_graph(
                    planner=self._planner,
                    executor=self._executor,
                    checkpointer=checkpointer,
                )
                result = graph.invoke(
                    Command(
                        resume={
                            "trace_id": trace_id,
                            "room_id": room_id,
                            "tool_name": tool_name,
                            "decision": decision,
                            "operator_id": operator_id,
                            "reason": reason,
                        }
                    ),
                    config=self._graph_config(trace_id),
                )
            status = "completed" if decision == "approved" else "rejected"
            saved = self._store.save_final_state(
                trace_id=trace_id,
                status=status,
                latest_state=dict(result),
                approval_decision=decision,
                operator_id=operator_id,
                reason=reason,
                audit_status=result.get("audit_status"),
                audit_ids=result.get("audit_ids", []),
                decision_trace_ids=result.get("decision_trace_ids", []),
            )
            return saved.to_status_payload()
        except Exception as exc:  # noqa: BLE001 - Web 人审必须 fail-closed，并把错误写入会话状态。
            saved = self._store.save_final_state(
                trace_id=trace_id,
                status="error",
                latest_state={
                    **(current.latest_state or {}),
                    "agent_status": "error",
                    "executed_tools": current.latest_state.get("executed_tools", []),
                    "observations": current.latest_state.get("observations", []),
                    "completed_nodes": current.latest_state.get("completed_nodes", []),
                    "error": str(exc),
                },
                approval_decision=decision,
                operator_id=operator_id,
                reason=reason,
                audit_status="error",
                audit_ids=current.audit_ids,
                decision_trace_ids=current.decision_trace_ids,
            )
            return saved.to_status_payload()

    def _checkpointer_context(self, trace_id: str) -> AbstractContextManager[Any]:
        if self._use_postgres_checkpointer:
            initialize_postgres_checkpointer(self._settings)
            return create_postgres_checkpointer(self._settings)
        if trace_id not in self._memory_checkpointers:
            self._memory_checkpointers[trace_id] = InMemorySaver()
        return nullcontext(self._memory_checkpointers[trace_id])

    @staticmethod
    def _graph_config(trace_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": trace_id}}

    @staticmethod
    def _extract_interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
        interrupts = result.get("__interrupt__") or []
        if not interrupts:
            return None
        return dict(interrupts[0].value)


def create_default_harness_dashboard_service() -> HarnessDashboardService:
    """创建生产默认 HarnessDashboardService。"""

    return HarnessDashboardService()


def create_in_memory_harness_dashboard_service() -> HarnessDashboardService:
    """创建单元测试和本地无库演示使用的内存版本服务。"""

    return HarnessDashboardService(store=InMemoryHarnessSessionStore(), use_postgres_checkpointer=False)
