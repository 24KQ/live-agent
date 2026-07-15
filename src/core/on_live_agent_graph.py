"""Phase 5C 播中 Agent 动态决策小循环。

播中使用 LangGraph 做观察-决策-建议小循环：
1. collect_on_live_context：收集弹幕聚合、库存状态、当前商品
2. on_live_planner：调用 AgentRulesPlanner 确定本轮目标
3. route_by_decision：条件路由节点
4. execute_tools：根据目标执行工具
5. observe_result：收集结果生成建议
6. write_audit：写入审计

播中 Agent 只建议，不自动执行高风险动作（hard-gate 仍需人审）。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from src.core.agent_decision import AgentReplanRoute
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.compatibility import observation_from_skill_result
from src.skill_runtime.executor import SyncSkillExecutorAdapter
from src.skill_runtime.models import (
    ApprovalContext,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionResult,
    SkillExecutionRoute,
    SkillExecutionStatus,
    _build_human_interrupt_approval,
)
from src.state.models import LifecycleStage


class OnLiveAgentGraphState(TypedDict, total=False):
    """播中 Agent 图状态。JSON 可序列化，适合 checkpoint。"""

    room_id: str
    trace_id: str
    # 播中上下文
    current_product: dict[str, Any] | None
    product_count: int
    danmaku_summary: list[dict[str, Any]]
    inventory_alerts: list[dict[str, Any]]
    # 决策状态
    planner_route: str | None
    goal: str | None
    observations: list[dict[str, Any]]
    executed_tools: list[dict[str, Any]]
    completed_nodes: list[str]
    # 记忆与信任
    trust_score: float
    memory_summary: str | None
    # 结果
    suggestion: str | None
    setup_status: str | None
    error: str | None


def create_initial_on_live_state(
    room_id: str,
    trace_id: str,
    trust_score: float = 0.7,
    danmaku_summary: list[dict[str, Any]] | None = None,
    inventory_alerts: list[dict[str, Any]] | None = None,
) -> OnLiveAgentGraphState:
    """创建播中 Agent 初始状态。"""
    return {
        "room_id": room_id,
        "trace_id": trace_id,
        "current_product": None,
        "product_count": 0,
        "danmaku_summary": danmaku_summary or [],
        "inventory_alerts": inventory_alerts or [],
        "planner_route": None,
        "goal": None,
        "observations": [],
        "executed_tools": [],
        "completed_nodes": [],
        "trust_score": trust_score,
        "memory_summary": None,
        "suggestion": None,
        "setup_status": None,
        "error": None,
    }


def build_on_live_agent_graph(
    planner: Any | None = None,
    executor: Any | None = None,
    service: Any = None,
    *,
    checkpointer: Any | None = None,
):
    """构建播中 Agent 决策小循环图。

    播中流程：
        START -> collect_on_live_context -> on_live_planner
          -> route_by_decision (conditional edge)
              -> execute_tools
              -> END（finish 路由时直接结束）
          -> observe_result -> write_audit -> END
    """
    _planner = planner or _DefaultPlanner()
    _executor = executor or _DefaultExecutor()

    graph = StateGraph(OnLiveAgentGraphState)

    graph.add_node("collect_on_live_context", lambda state: _collect_context_node(state))
    graph.add_node("on_live_planner", lambda state: _planner_node(state, _planner))
    graph.add_node("route_by_decision", _route_by_decision_node)
    graph.add_node("execute_tools", lambda state: _execute_tools_node(state, _executor))
    graph.add_node("observe_result", _observe_result_node)
    graph.add_node("write_audit", _write_audit_node)

    graph.add_edge(START, "collect_on_live_context")
    graph.add_edge("collect_on_live_context", "on_live_planner")
    graph.add_edge("on_live_planner", "route_by_decision")
    graph.add_conditional_edges(
        "route_by_decision",
        _route_decider,
        {
            "execute_tools": "execute_tools",
            END: END,
        },
    )
    graph.add_edge("execute_tools", "observe_result")
    graph.add_edge("observe_result", "write_audit")
    graph.add_edge("write_audit", END)

    return graph.compile(checkpointer=checkpointer)


def _collect_context_node(state: OnLiveAgentGraphState) -> dict[str, Any]:
    """收集播中上下文：弹幕摘要、库存告警、当前商品。"""
    try:
        danmaku = state.get("danmaku_summary", [])
        alerts = state.get("inventory_alerts", [])
        context_summary = ""

        # 统计弹幕热点分类
        if danmaku:
            categories = {}
            for d in danmaku:
                cat = d.get("category", "unknown")
                categories[cat] = categories.get(cat, 0) + d.get("count", 0)
            top = sorted(categories.items(), key=lambda x: -x[1])[:3]
            if top:
                context_summary += "弹幕热点：" + ", ".join(f"{k}({v}条)" for k, v in top) + "。"

        # 统计库存告警
        if alerts:
            context_summary += f"库存告警：{len(alerts)}个商品异常。"
            for a in alerts[:3]:
                context_summary += f"{a.get('product_name', a.get('product_id', '未知商品'))}；"

        return {
            "completed_nodes": _append_node(state, "collect_on_live_context"),
            "product_count": len(danmaku),
        }
    except Exception as exc:
        return {
            "error": f"collect_on_live_context failed: {exc}",
            "completed_nodes": _append_node(state, "collect_on_live_context"),
        }


def _planner_node(state: OnLiveAgentGraphState, planner: Any) -> dict[str, Any]:
    """播中 planner 节点：根据弹幕和告警决策本轮目标。

    如果 planner 是 OnLiveLLMPlanner（有 plan 方法且不是 _DefaultPlanner），
    优先用 LLM 决策；否则走旧确定性规则。
    LLM 不可用或失败时自动降级到规则，不中断流程。
    """
    try:
        danmaku = state.get("danmaku_summary", [])
        alerts = state.get("inventory_alerts", [])
        trust_score = state.get("trust_score", 0.7)
        memory_hints = state.get("memory_summary", None)

        # 判断 planner 类型：OnLiveLLMPlanner 有 plan 方法且不是 _DefaultPlanner
        use_llm = hasattr(planner, "plan") and not isinstance(planner, _DefaultPlanner)

        if use_llm:
            try:
                # OnLiveLLMPlanner.plan() 接受的参数
                kwargs = {
                    "danmaku_summary": danmaku,
                    "inventory_alerts": alerts,
                    "trust_score": trust_score,
                }
                if memory_hints:
                    kwargs["memory_hints"] = [(memory_hints, 0.5)]

                decision = planner.plan(**kwargs)

                route = AgentReplanRoute.FALLBACK.value
                if decision.get("route") == "direct_plan":
                    route = AgentReplanRoute.DIRECT_PLAN.value
                elif decision.get("route") == "finish":
                    route = AgentReplanRoute.FINISH.value

                return {
                    "planner_route": route,
                    "goal": decision.get("goal", "LLM 决策"),
                    "suggestion": decision.get("suggestion"),
                    "completed_nodes": _append_node(state, "on_live_planner"),
                }
            except Exception:
                # LLM 失败，降级到规则
                pass

        # 确定性规则（_DefaultPlanner 或 LLM 降级）
        has_high_frequency = any(d.get("count", 0) >= 10 for d in danmaku)
        has_alerts = len(alerts) > 0

        if not danmaku and not alerts:
            return {
                "planner_route": AgentReplanRoute.FINISH.value,
                "goal": "无事件，不干预",
                "suggestion": None,
                "completed_nodes": _append_node(state, "on_live_planner"),
            }

        if has_alerts:
            route = AgentReplanRoute.DIRECT_PLAN.value
            goal = "处理库存告警"
            suggestion = "检测到 " + str(len(alerts)) + " 个库存异常，建议检查备选商品并准备切换。"
        elif has_high_frequency:
            route = AgentReplanRoute.DIRECT_PLAN.value
            goal = "处理高频弹幕"
            top = max(danmaku, key=lambda d: d.get("count", 0))
            summary = top.get("summary", top.get("category", "未知"))
            suggestion = "弹幕高频问题：" + str(summary) + "，建议主播重点回应。"
        else:
            route = AgentReplanRoute.FINISH.value
            goal = "低频事件，不干预"
            suggestion = None

        return {
            "planner_route": route,
            "goal": goal,
            "suggestion": suggestion,
            "completed_nodes": _append_node(state, "on_live_planner"),
        }
    except Exception as exc:
        return {
            "planner_route": AgentReplanRoute.FALLBACK.value,
            "goal": "planner 失败，降级",
            "error": "on_live_planner failed: " + str(exc),
            "completed_nodes": _append_node(state, "on_live_planner"),
        }


def _route_by_decision_node(state: OnLiveAgentGraphState) -> dict[str, Any]:
    """路由决策节点——仅作为 conditional edge 锚点。"""
    return {"completed_nodes": _append_node(state, "route_by_decision")}


def _route_decider(state: OnLiveAgentGraphState) -> str:
    """条件路由：finish 时直接 END，否则走 execute_tools。"""
    route = state.get("planner_route", "")
    if route == AgentReplanRoute.FINISH.value:
        return END
    return "execute_tools"


def _execute_tools_node(state: OnLiveAgentGraphState, executor: Any) -> dict[str, Any]:
    """执行工具节点：根据决策执行对应播中工具。"""
    try:
        tools_executed = []
        route = state.get("planner_route", "")

        if route == AgentReplanRoute.DIRECT_PLAN.value:
            # 执行播中建议工具
            goal = state.get("goal", "")
            if "库存" in goal or "告警" in goal:
                tools_executed.append({
                    "tool_name": "recommend_backup",
                    "status": "simulated",
                    "summary": "建议切换备选商品",
                })
            elif "弹幕" in goal:
                tools_executed.append({
                    "tool_name": "generate_on_live_prompt",
                    "status": "simulated",
                    "summary": "生成弹幕回复建议",
                })

        return {
            "executed_tools": tools_executed,
            "completed_nodes": _append_node(state, "execute_tools"),
        }
    except Exception as exc:
        return {
            "error": f"execute_tools failed: {exc}",
            "completed_nodes": _append_node(state, "execute_tools"),
        }


def _observe_result_node(state: OnLiveAgentGraphState) -> dict[str, Any]:
    """观察节点：收集工具执行结果。"""
    tools = state.get("executed_tools", [])
    suggestion = state.get("suggestion")

    # 如果没有建议但有工具执行，生成观察摘要
    if not suggestion and tools:
        tool_summaries = [t.get("summary", "") for t in tools if t.get("status") == "simulated"]
        if tool_summaries:
            suggestion = "播中 Agent 观察：已执行 " + "；".join(tool_summaries)

    return {
        "observations": [{"tools_executed": len(tools), "has_suggestion": suggestion is not None}],
        "suggestion": suggestion,
        "completed_nodes": _append_node(state, "observe_result"),
    }


def _write_audit_node(state: OnLiveAgentGraphState) -> dict[str, Any]:
    """写入审计节点。"""
    return {
        "setup_status": "observed",
        "completed_nodes": _append_node(state, "write_audit"),
    }


def _append_node(state: OnLiveAgentGraphState, node_name: str) -> list[str]:
    """向 completed_nodes 追加节点名。"""
    return [*state.get("completed_nodes", []), node_name]


class _DefaultPlanner:
    """默认播中 planner——用于测试和快速验证。"""

    def plan(self, room_id: str, trace_id: str, **kwargs) -> Any:
        from src.core.agent_decision import AgentPlannerDecision
        return AgentPlannerDecision(
            trace_id=trace_id,
            room_id=room_id,
            goal="默认播中决策",
            route=AgentReplanRoute.FINISH,
            reason="无事件",
            tool_calls=[],
        )




class _LocalServiceExecutor:
    """本地服务执行器——调用真实 OnLiveFlowService / DanmakuFlowService。

    替代 _DefaultExecutor 的模拟行为，让播中 Agent 真正调起本地业务服务：
    - OnLiveFlowService.handle_sold_out_event: 处理售罄事件
    - recommend_backup_product: 推荐备用商品
    - generate_sold_out_prompt: 生成主播提示
    - DanmakuFlowService.handle_danmaku_batch: 弹幕聚合
    """

    def __init__(self, on_live_service=None, danmaku_service=None) -> None:
        self._on_live_service = on_live_service
        self._danmaku_service = danmaku_service

    def execute(
        self,
        tool_name: str,
        arguments: dict,
        room_id: str,
        trace_id: str,
        state=None,
        sold_out_product=None,
    ) -> dict:
        """根据 tool_name 路由到对应的本地服务。

        返回 dict:
            - status: success / error / pending
            - summary: 执行摘要
            - audit_ids: 审计 ID 列表（如果有）
            - backup_product_id: 备用商品 ID（如果是推荐备用）
            - message: 提示文案（如果是生成主播提示）
            - group_count: 聚合组数（如果是弹幕聚合）
        """
        try:
            if tool_name == "handle_sold_out_event":
                return self._handle_sold_out(arguments, room_id, trace_id, state)

            elif tool_name in ("recommend_backup", "recommend_backup_product"):
                return self._recommend_backup(arguments, room_id, trace_id, state)

            elif tool_name == "generate_on_live_prompt":
                return self._generate_prompt(arguments, room_id, trace_id, sold_out_product)

            elif tool_name == "aggregate_danmaku_questions":
                return self._aggregate_danmaku(arguments, room_id, trace_id, state)

            else:
                return {
                    "tool_name": tool_name,
                    "status": "error",
                    "summary": f"tool {tool_name} not supported in _LocalServiceExecutor",
                }
        except Exception as exc:
            return {
                "tool_name": tool_name,
                "status": "error",
                "summary": f"execution failed: {exc}",
            }

    def _handle_sold_out(self, arguments, room_id, trace_id, state):
        """调用 OnLiveFlowService 处理售罄事件。"""
        if self._on_live_service is None:
            return {"status": "error", "summary": "on_live_service not configured"}

        from src.skills.on_live_events import InventoryEvent, OnLiveEventType
        product_id = arguments.get("product_id", "")
        event = InventoryEvent(
            room_id=room_id,
            product_id=product_id,
            event_type=OnLiveEventType.SOLD_OUT,
            trace_id=trace_id,
        )
        if state is None:
            return {"status": "error", "summary": "state required for handle_sold_out_event"}

        result = self._on_live_service.handle_sold_out_event(state, event)
        return {
            "tool_name": "handle_sold_out_event",
            "status": "success",
            "summary": f"sold_out handled for {product_id}",
            "audit_ids": result.audit_ids,
            "backup_product_id": result.backup_product.product_id if result.backup_product else None,
            "message": result.prompt.message if result.prompt else "",
        }

    def _recommend_backup(self, arguments, room_id, trace_id, state):
        """调用本地 recommend_backup_product。"""
        from src.skills.backup_product_recommender import (
            recommend_backup_product,
            BackupProductNotFoundError,
        )

        if state is None:
            return {"status": "error", "summary": "state required for recommend_backup"}

        sold_out_id = arguments.get("sold_out_product_id", "")
        try:
            backup = recommend_backup_product(state, sold_out_product_id=sold_out_id)
            return {
                "tool_name": "recommend_backup",
                "status": "success",
                "summary": f"recommended backup {backup.product_id}",
                "backup_product_id": backup.product_id,
            }
        except BackupProductNotFoundError as exc:
            return {
                "tool_name": "recommend_backup",
                "status": "success",
                "summary": f"no backup found: {exc}",
                "backup_product_id": None,
            }

    def _generate_prompt(self, arguments, room_id, trace_id, sold_out_product):
        """调用 generate_sold_out_prompt 生成主播提示。"""
        from src.skills.on_live_prompt import generate_sold_out_prompt

        if sold_out_product is None:
            return {"status": "error", "summary": "sold_out_product required for generate_on_live_prompt"}

        backup_id = arguments.get("backup_product_id")
        backup_product = None
        if backup_id:
            try:
                backup_product = sold_out_product.model_copy(update={"product_id": backup_id})
            except Exception:
                backup_product = None

        prompt = generate_sold_out_prompt(
            sold_out_product=sold_out_product,
            backup_product=backup_product,
        )
        return {
            "tool_name": "generate_on_live_prompt",
            "status": "success",
            "summary": f"prompt severity: {prompt.severity}",
            "message": prompt.message,
            "severity": prompt.severity,
        }

    def _aggregate_danmaku(self, arguments, room_id, trace_id, state):
        """调用 DanmakuFlowService.handle_danmaku_batch。"""
        if self._danmaku_service is None:
            return {"status": "error", "summary": "danmaku_service not configured"}

        from src.skills.danmaku_events import DanmakuEvent
        from datetime import datetime, timezone
        events_data = arguments.get("events", [])
        events = []
        for ev in events_data:
            if isinstance(ev, DanmakuEvent):
                events.append(ev)
            else:
                # dict -> DanmakuEvent
                ev_time = ev.get("event_time")
                if ev_time is None:
                    ev_time = datetime(2026, 7, 10, tzinfo=timezone.utc)
                elif isinstance(ev_time, str):
                    try:
                        ev_time = datetime.fromisoformat(ev_time.replace("Z", "+00:00"))
                    except Exception:
                        ev_time = datetime(2026, 7, 10, tzinfo=timezone.utc)
                events.append(DanmakuEvent(
                    room_id=ev.get("room_id", room_id),
                    viewer_id=ev.get("viewer_id", "anonymous"),
                    content=ev.get("content", ""),
                    event_time=ev_time,
                    trace_id=ev.get("trace_id", trace_id),
                ))

        if state is None:
            return {"status": "error", "summary": "state required for aggregate_danmaku_questions"}

        result = self._danmaku_service.handle_danmaku_batch(state, events)
        return {
            "tool_name": "aggregate_danmaku_questions",
            "status": "success",
            "summary": f"aggregated {len(result.groups)} groups",
            "group_count": len(result.groups),
            "audit_ids": result.audit_ids,
        }


class RuntimeOnLiveExecutor:
    """播中 Runtime 执行器，保持旧 Graph executor 同步外观。

    Graph 和 Harness 仍调用 execute(tool_name, arguments, room_id, trace_id, ...)，
    本类只在边界内构造 ON_LIVE SkillCall 并委托 SyncSkillExecutorAdapter。它不做
    重试、不 fallback 到 _LocalServiceExecutor，也不修改 LangGraph state 结构。
    """

    def __init__(self, skill_executor: SyncSkillExecutorAdapter) -> None:
        self._skill_executor = skill_executor
        # Catalog 是 Skill 精确版本的唯一事实源。播中兼容入口在装配时冻结快照，
        # 避免已开始的 Graph 调用因后续 Catalog 重装配而悄然切换版本。
        self._skill_versions = {
            manifest.skill_id: manifest.version
            for manifest in get_default_skill_catalog()
        }

    def execute(
        self,
        tool_name: str,
        arguments: dict,
        room_id: str,
        trace_id: str,
        state: Any = None,
        **kwargs,
    ) -> dict[str, Any]:
        """执行播中 Skill，并返回旧节点可消费的 dict observation。

        state 保持为显式兼容参数，使 Harness Graph Protocol、反射检查和后续类型
        标注都能识别审批上下文；额外 kwargs 仅用于兼容历史调用，不参与信任判断。
        """
        try:
            result = self._skill_executor.execute(
                SkillCall(
                    skill_id=tool_name,
                    # 未登记工具仍保留旧版本占位，让 Executor 返回稳定 SKILL_NOT_FOUND，
                    # 而不是让兼容层抛 KeyError 后丢失可审计的失败事实。
                    version=self._skill_versions.get(tool_name, "1.0.0"),
                    context=SkillExecutionContext(
                        room_id=room_id,
                        trace_id=trace_id,
                        lifecycle=LifecycleStage.ON_LIVE,
                        execution_route=SkillExecutionRoute.SKILL_RUNTIME,
                        idempotency_key=self._idempotency_key(tool_name, arguments, trace_id),
                        approval=self._approval_from_state(state),
                    ),
                    arguments=self._arguments(tool_name, arguments, room_id, trace_id),
                )
            )
            return self._result_to_dict(tool_name, result)
        except Exception:
            # Runtime 边界异常必须脱敏为旧 executor 形状，避免 Graph 节点抛出后丢失
            # checkpoint 观察事实；这里也绝不调用 legacy 作为 fallback。
            return {
                "tool_name": tool_name,
                "status": "error",
                "summary": "HANDLER_FAILED: on-live runtime execution failed",
            }

    @staticmethod
    def _idempotency_key(tool_name: str, arguments: dict, trace_id: str) -> str | None:
        """从旧参数读取幂等键；售罄若缺失则用 trace 生成稳定兼容键。"""
        value = arguments.get("idempotency_key")
        if isinstance(value, str) and value:
            return value
        if tool_name == "handle_sold_out_event":
            return f"{trace_id}:handle_sold_out_event"
        return None

    @staticmethod
    def _arguments(tool_name: str, arguments: dict, room_id: str, trace_id: str) -> dict:
        """只为尚未迁移的读/建议 Skill 补齐旧业务字段。

        ``handle_sold_out_event@2.0.0`` 的业务 Schema 只有 ``product_id`` 与
        ``expected_version``。room、trace 和幂等键均属于执行 Context，旧 Harness
        不得把它们重新塞回业务参数以绕过严格 Schema 或伪造事件授权。
        """
        normalized = dict(arguments)
        if tool_name in {
            "recommend_backup_product",
            "generate_on_live_prompt",
        }:
            normalized.setdefault("room_id", room_id)
        if tool_name in {
            "aggregate_danmaku_questions",
            "generate_danmaku_reply",
            "on_live_context_collect",
        }:
            normalized.setdefault("room_id", room_id)
            normalized.setdefault("trace_id", trace_id)
        if tool_name == "handle_sold_out_event":
            normalized.pop("room_id", None)
            normalized.pop("trace_id", None)
            normalized.pop("idempotency_key", None)
        return normalized

    @staticmethod
    def _approval_from_state(state: Any) -> ApprovalContext | None:
        """从 Harness state 恢复可信审批证据；无审批时返回 None。

        当前批次二的 handle_sold_out_event 是 AUTO gate，该逻辑主要保证后续高风险
        播中写能力接入时不会把普通业务参数伪造成审批。
        """
        if not isinstance(state, dict):
            return None
        if state.get("approval_decision") != "approved":
            return None
        operator_id = state.get("approval_operator_id")
        audit_id = state.get("approval_resume_audit_id")
        if not isinstance(operator_id, str) or not isinstance(audit_id, str):
            return None
        return _build_human_interrupt_approval(
            decision="APPROVED",
            operator_id=operator_id,
            approval_audit_id=audit_id,
        )

    @staticmethod
    def _result_to_dict(tool_name: str, result: SkillExecutionResult) -> dict[str, Any]:
        """把 Runtime 结果压缩为旧播中 executor 的 dict 契约。"""
        observation = observation_from_skill_result(tool_name, result)
        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "status": observation.status,
            "summary": observation.summary,
            "audit_ids": [observation.audit_id] if observation.audit_id else [],
            "attempt_id": result.attempt_id,
        }
        if result.status != SkillExecutionStatus.SUCCESS or result.output is None:
            if result.failure is not None:
                payload["failure_category"] = result.failure.category.value
            return payload

        output = result.output
        if "backup_product" in output and output["backup_product"] is not None:
            payload["backup_product_id"] = output["backup_product"].get("product_id")
        if "prompt" in output:
            payload["message"] = output["prompt"].get("message", "")
            payload["severity"] = output["prompt"].get("severity")
        if "reply" in output:
            payload["message"] = output["reply"].get("message", "")
        if "groups" in output:
            payload["group_count"] = len(output["groups"])
        if "suggestion" in output:
            payload["suggestion"] = output["suggestion"]
        payload["output"] = output
        return payload


class _DefaultExecutor:
    """默认播中执行器——用于测试和快速验证。"""

    def execute(self, tool_name: str, **kwargs) -> dict[str, Any]:
        return {"tool_name": tool_name, "status": "simulated"}
