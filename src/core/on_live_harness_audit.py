"""Phase 5H 播中 Harness Agent 审计与 DecisionTrace 写入。

这个模块把 LangGraph Harness Agent 的最终 state 转换成两类可回放证据：
1. ToolCallAuditStore 可写入的工具调用审计事件；
2. DecisionTraceStore 可写入的建议证据记录。

默认不强依赖数据库。没有注入 store 时返回 dry-run payload，CLI、单元测试和本地演示仍能看到完整审计
结构；生产或集成环境只需要通过依赖注入接入真实 store 即可落库。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from src.audit.tool_call_audit import AuditEvent, ToolCallAuditStore
from src.core.security_hooks import GateDecision
from src.memory.decision_trace_store import DecisionTraceStore
from src.memory.models import AnchorAction, BusinessResult, DecisionTraceRecord
from src.state.models import ActionType, RiskLevel


_ACTION_TYPE_BY_TOOL: dict[str, ActionType] = {
    "handle_sold_out_event": ActionType.HANDLE_SOLD_OUT_EVENT,
    "recommend_backup_product": ActionType.RECOMMEND_BACKUP_PRODUCT,
    "recommend_backup": ActionType.RECOMMEND_BACKUP_PRODUCT,
    "generate_on_live_prompt": ActionType.GENERATE_ON_LIVE_PROMPT,
    "aggregate_danmaku_questions": ActionType.AGGREGATE_DANMAKU_QUESTIONS,
    "generate_danmaku_reply": ActionType.GENERATE_DANMAKU_REPLY,
}

_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "password", "passwd", "secret", "token")
_SENSITIVE_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\[^,\s]*?\\.env", re.IGNORECASE),
)


class OnLiveHarnessAuditWriter:
    """把 Harness Agent state 写成审计事件和 DecisionTrace。

    audit_store / decision_trace_store 均为可选依赖。没有真实 store 时不抛错，而是返回 dry-run 结构，
    这样播中 Agent 的可观测性不会被本地数据库状态卡住。
    """

    def __init__(
        self,
        audit_store: ToolCallAuditStore | None = None,
        decision_trace_store: DecisionTraceStore | None = None,
    ) -> None:
        self._audit_store = audit_store
        self._decision_trace_store = decision_trace_store

    def write(self, state: dict[str, Any]) -> dict[str, Any]:
        """写入审计并返回可合并回 LangGraph state 的字段。

        返回值固定包含 audit_status、audit_ids、decision_trace_ids 和 audit_payload。Graph 节点会把这些
        字段直接 merge 回最终 state，供 CLI、副屏和测试观察。
        """

        request_payload = self._build_request_payload(state)
        result_payload = self._build_result_payload(state)
        audit_payload: dict[str, Any] = {
            "request_payload": request_payload,
            "result_payload": result_payload,
        }

        audit_ids = self._write_or_preview_audit_events(state, request_payload, result_payload)
        decision_trace_ids = self._write_or_preview_decision_trace(state, audit_payload)
        has_real_store = self._audit_store is not None or (
            self._decision_trace_store is not None and state.get("anchor_id")
        )

        return {
            "audit_status": "recorded" if has_real_store else "dry_run",
            "audit_ids": audit_ids,
            "decision_trace_ids": decision_trace_ids,
            "audit_payload": _sanitize(audit_payload),
        }

    def _build_request_payload(self, state: dict[str, Any]) -> dict[str, Any]:
        """只保留复盘需要的请求侧摘要，避免把完整 prompt 或 system_context 写入审计。"""

        return _sanitize(
            {
                "iteration": state.get("iteration", 0),
                "completed_nodes": state.get("completed_nodes", []),
                "pending_tool_call": state.get("pending_tool_call"),
                "tool_policy": state.get("tool_policy"),
                "context_summary": state.get("context_summary"),
            }
        )

    def _build_result_payload(self, state: dict[str, Any]) -> dict[str, Any]:
        """记录 Agent 的可复盘结果，包括最终建议、observation 和工具执行摘要。"""

        return _sanitize(
            {
                "agent_status": state.get("agent_status"),
                "final_suggestion": state.get("final_suggestion"),
                "observations": state.get("observations", []),
                "executed_tools": state.get("executed_tools", []),
                "error": state.get("error"),
            }
        )

    def _write_or_preview_audit_events(
        self,
        state: dict[str, Any],
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> list[str]:
        """逐个工具调用生成审计；无工具调用时也生成一条 Harness 决策审计。"""

        events = self._build_audit_events(state, request_payload, result_payload)
        if self._audit_store is None:
            return [f"dry-run:{event.trace_id}:{event.tool_name}:{index}" for index, event in enumerate(events, start=1)]
        return [str(self._audit_store.record_event(event)) for event in events]

    def _build_audit_events(
        self,
        state: dict[str, Any],
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> list[AuditEvent]:
        """根据工具结果、pending 工具或最终建议生成 AuditEvent 列表。"""

        executed_tools = state.get("executed_tools") or []
        if executed_tools:
            return [
                self._event_for_tool(state, tool, request_payload, result_payload, index)
                for index, tool in enumerate(executed_tools, start=1)
            ]

        pending_tool = state.get("pending_tool_call") or {}
        if pending_tool:
            return [self._event_for_tool(state, pending_tool, request_payload, result_payload, 1)]

        return [self._event_for_tool(state, {"tool_name": "harness_agent"}, request_payload, result_payload, 1)]

    def _event_for_tool(
        self,
        state: dict[str, Any],
        tool: dict[str, Any],
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
        index: int,
    ) -> AuditEvent:
        """把单个工具或最终 Harness 决策映射成审计事件。"""

        tool_name = str(tool.get("tool_name") or "harness_agent")
        status = str(state.get("agent_status") or "")
        return AuditEvent(
            trace_id=str(state.get("trace_id") or ""),
            room_id=str(state.get("room_id") or ""),
            tool_name=tool_name,
            action_type=_action_type_for(tool_name, status),
            risk_level=_risk_level_for(tool, status),
            gate_decision=_gate_decision_for(status),
            operator_decision=_operator_decision_for(status),
            request_payload={
                **request_payload,
                "audit_event_index": index,
                "audited_tool_name": tool_name,
            },
            result_payload=result_payload,
        )

    def _write_or_preview_decision_trace(self, state: dict[str, Any], audit_payload: dict[str, Any]) -> list[str]:
        """写入或预览 DecisionTrace。

        本阶段先记录 Agent 建议证据，主播是否采纳、业务结果和 trust_delta 仍由播后复盘阶段更新。
        """

        record = self._build_decision_trace_record(state)
        if record is None:
            audit_payload["decision_trace_dry_run"] = None
            return []

        if self._decision_trace_store is None or not state.get("anchor_id"):
            audit_payload["decision_trace_dry_run"] = record.model_dump(mode="json")
            return [f"dry-run:{record.trace_id}:decision-trace"]

        decision_trace_id = str(self._decision_trace_store.record_trace(record))
        audit_payload["decision_trace_id"] = decision_trace_id
        return [decision_trace_id]

    def _build_decision_trace_record(self, state: dict[str, Any]) -> DecisionTraceRecord | None:
        """从最终建议构造 DecisionTraceRecord；没有任何建议时不强行生成。"""

        final_suggestion = state.get("final_suggestion")
        if not final_suggestion and state.get("agent_status") not in {"pending_human", "blocked", "max_iterations"}:
            return None

        recommendation = _sanitize(
            {
                "final_suggestion": final_suggestion,
                "agent_status": state.get("agent_status"),
                "tool_policy": state.get("tool_policy"),
                "observations": state.get("observations", []),
                "executed_tools": state.get("executed_tools", []),
                "completed_nodes": state.get("completed_nodes", []),
            }
        )
        return DecisionTraceRecord(
            trace_id=str(state.get("trace_id") or ""),
            anchor_id=str(state.get("anchor_id") or "dry-run-anchor"),
            room_id=str(state.get("room_id") or ""),
            recommendation=recommendation,
            anchor_action=AnchorAction.REJECTED,
            business_result=BusinessResult.AGENT_RIGHT,
            lift=Decimal("0.00"),
            trust_delta=Decimal("0.00"),
            final_trust_score=Decimal(str(state.get("trust_score", 0.7))),
        )


def _action_type_for(tool_name: str, status: str) -> ActionType:
    """根据工具名和 Agent 状态选择 ActionType。"""

    if status == "pending_human":
        return ActionType.HUMAN_APPROVAL_PENDING
    return _ACTION_TYPE_BY_TOOL.get(tool_name, ActionType.GENERATE_ON_LIVE_PROMPT)


def _risk_level_for(tool: dict[str, Any], status: str) -> RiskLevel:
    """从工具调用中解析风险等级，解析失败时按 LOW 记录，避免审计写入被脏值阻断。"""

    if status in {"blocked", "max_iterations"}:
        return RiskLevel.HIGH
    raw = str(tool.get("risk_level") or "LOW").upper()
    try:
        return RiskLevel(raw)
    except ValueError:
        return RiskLevel.LOW


def _gate_decision_for(status: str) -> GateDecision:
    """把 Harness 状态转换为审计 gate_decision。"""

    if status == "pending_human":
        return GateDecision.HARD_GATE
    if status in {"blocked", "max_iterations"}:
        return GateDecision.BLOCK
    return GateDecision.AUTO


def _operator_decision_for(status: str) -> str:
    """记录当前动作是自动执行、等待人审还是被系统阻断。"""

    if status == "pending_human":
        return "pending"
    if status in {"blocked", "max_iterations"}:
        return "blocked"
    return "auto"


def _sanitize(value: Any) -> Any:
    """递归脱敏审计 payload。

    审计只需要可复盘证据，不应该保存 API key、token、password、.env 路径或用户本机私密路径。
    """

    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                sanitized[key_text] = "<redacted>"
            else:
                sanitized[key_text] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(item) for item in value)
    if isinstance(value, str):
        return _sanitize_text(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    """按字段名判断是否需要脱敏。"""

    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _sanitize_text(text: str) -> str:
    """按字符串内容脱敏本机路径和 .env 片段。"""

    sanitized = text.replace(".env", "<redacted-env>")
    for pattern in _SENSITIVE_PATH_PATTERNS:
        sanitized = pattern.sub("<redacted-path>", sanitized)
    return sanitized
