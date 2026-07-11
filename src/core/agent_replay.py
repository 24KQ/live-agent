# -*- coding: utf-8 -*-
"""Phase 7A Agent 回放模型与服务。

本模块把一次 Harness Agent 执行整理成稳定的时间线。优先从 LangGraph
checkpoint 历史读取；如果 checkpoint 不可用，则从 Phase 6C 的
Harness session、ToolCallAudit 和 DecisionTrace 降级重建。降级回放不会假装
拥有完整节点状态，而是显式标记 `replay_fidelity="degraded"`，方便后续评估
区分证据强弱。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


ReplayFidelity = Literal["checkpoint", "degraded"]

_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "password", "passwd", "secret", "token")
_SENSITIVE_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\[^,\s]*?\\.env", re.IGNORECASE),
)


class ReplayTimelineItem(BaseModel):
    """标准化回放时间线中的单个节点或证据事件。"""

    sequence: int
    node_name: str
    phase: str = "on_live"
    status: str = "completed"
    timestamp: datetime | None = None
    state_delta: dict[str, Any] = Field(default_factory=dict)
    tool_call: dict[str, Any] = Field(default_factory=dict)
    approval: dict[str, Any] = Field(default_factory=dict)
    observation: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("state_delta", "tool_call", "approval", "observation", mode="before")
    @classmethod
    def _sanitize_payload(cls, value: Any) -> Any:
        return sanitize_replay_payload(value or {})


class AgentReplay(BaseModel):
    """一次 Agent 执行的可评估回放快照。"""

    trace_id: str
    graph_version: str = "harness-v1"
    replay_fidelity: ReplayFidelity
    timeline: list[ReplayTimelineItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentReplayService:
    """构建 Agent 回放快照。

    `graph` 可选注入已编译 LangGraph，用于读取 `get_state_history()`；单元测试
    和数据库不可用场景可以只注入 session/audit store，让服务走降级回放。
    """

    def __init__(
        self,
        *,
        session_store: Any | None = None,
        audit_store: Any | None = None,
        decision_trace_store: Any | None = None,
        graph: Any | None = None,
        checkpointer: Any | None = None,
        graph_version: str = "harness-v1",
    ) -> None:
        self._session_store = session_store
        self._audit_store = audit_store
        self._decision_trace_store = decision_trace_store
        self._graph = graph
        self._checkpointer = checkpointer
        self._graph_version = graph_version

    def build_replay(self, trace_id: str) -> AgentReplay:
        """按优先级构建回放：checkpoint 完整回放 -> 业务表降级回放。"""

        checkpoint_replay = self._try_checkpoint_replay(trace_id)
        if checkpoint_replay is not None:
            return checkpoint_replay
        return self._build_degraded_replay(trace_id)

    def _try_checkpoint_replay(self, trace_id: str) -> AgentReplay | None:
        """尝试从 LangGraph 历史状态构建完整回放。

        不同 LangGraph 版本返回的 snapshot 结构略有差异，所以这里只提取最稳定
        的 `metadata/source`、`values` 和 `created_at`。提取失败时返回 None，
        交给降级链路处理。
        """

        history_reader = self._graph or self._checkpointer
        if history_reader is None or not hasattr(history_reader, "get_state_history"):
            return None

        try:
            history = list(
                history_reader.get_state_history(
                    {"configurable": {"thread_id": trace_id}}
                )
            )
        except Exception:
            return None

        items: list[ReplayTimelineItem] = []
        for sequence, snapshot in enumerate(reversed(history), start=1):
            values = dict(getattr(snapshot, "values", {}) or {})
            metadata = dict(getattr(snapshot, "metadata", {}) or {})
            node_name = (
                metadata.get("source")
                or metadata.get("writes")
                or values.get("last_node")
                or f"checkpoint_{sequence}"
            )
            items.append(
                ReplayTimelineItem(
                    sequence=sequence,
                    node_name=str(node_name),
                    phase="on_live",
                    status=str(values.get("agent_status") or "completed"),
                    timestamp=getattr(snapshot, "created_at", None),
                    state_delta=values,
                    tool_call=(values.get("pending_tool_call") or {}),
                    approval=_approval_from_state(values),
                    observation=_last_dict(values.get("observations")),
                    evidence_ids=list(values.get("audit_ids") or []) + list(values.get("decision_trace_ids") or []),
                )
            )

        if not items:
            return None
        return AgentReplay(
            trace_id=trace_id,
            graph_version=self._graph_version,
            replay_fidelity="checkpoint",
            timeline=items,
        )

    def _build_degraded_replay(self, trace_id: str) -> AgentReplay:
        """从 Harness session 和审计记录降级重建回放。"""

        if self._session_store is None:
            return AgentReplay(trace_id=trace_id, graph_version=self._graph_version, replay_fidelity="degraded")

        record = self._session_store.get(trace_id)
        state = dict(getattr(record, "latest_state", {}) or {})
        audit_ids = list(getattr(record, "audit_ids", []) or [])
        decision_trace_ids = list(getattr(record, "decision_trace_ids", []) or [])
        evidence_ids = audit_ids + decision_trace_ids
        timestamp = getattr(record, "updated_at", None)

        items: list[ReplayTimelineItem] = []
        for sequence, node_name in enumerate(state.get("completed_nodes", []) or [], start=1):
            items.append(
                ReplayTimelineItem(
                    sequence=sequence,
                    node_name=str(node_name),
                    phase="on_live",
                    status=str(state.get("agent_status") or getattr(record, "status", "completed")),
                    timestamp=timestamp,
                    state_delta={"node_name": node_name},
                    evidence_ids=evidence_ids,
                )
            )

        next_sequence = len(items) + 1
        for tool in state.get("executed_tools", []) or []:
            items.append(
                ReplayTimelineItem(
                    sequence=next_sequence,
                    node_name="execute_tool",
                    phase="on_live",
                    status=str(tool.get("status") or "completed"),
                    timestamp=timestamp,
                    tool_call=dict(tool),
                    approval=_approval_from_state(state),
                    observation=_matching_observation(state.get("observations", []), tool),
                    evidence_ids=evidence_ids,
                )
            )
            next_sequence += 1

        audit_items = self._audit_items(trace_id, start_sequence=next_sequence, fallback_evidence_ids=evidence_ids)
        items.extend(audit_items)

        return AgentReplay(
            trace_id=trace_id,
            graph_version=getattr(record, "graph_version", self._graph_version),
            replay_fidelity="degraded",
            timeline=items,
        )

    def _audit_items(
        self,
        trace_id: str,
        *,
        start_sequence: int,
        fallback_evidence_ids: list[str],
    ) -> list[ReplayTimelineItem]:
        """把 ToolCallAudit 作为降级回放的补充证据。"""

        if self._audit_store is None or not hasattr(self._audit_store, "list_events_by_trace_id"):
            return []
        try:
            events = self._audit_store.list_events_by_trace_id(trace_id)
        except Exception:
            return []

        items: list[ReplayTimelineItem] = []
        for offset, event in enumerate(events):
            items.append(
                ReplayTimelineItem(
                    sequence=start_sequence + offset,
                    node_name="audit_event",
                    phase="on_live",
                    status="recorded",
                    timestamp=event.get("created_at"),
                    tool_call={
                        "tool_name": event.get("tool_name"),
                        "risk_level": event.get("risk_level"),
                    },
                    approval={
                        "decision": _approval_decision_from_operator(event.get("operator_decision")),
                        "gate_decision": event.get("gate_decision"),
                        "operator_decision": event.get("operator_decision"),
                    },
                    evidence_ids=(
                        [str(event.get("audit_id"))] + [item for item in fallback_evidence_ids if item != str(event.get("audit_id"))]
                        if event.get("audit_id")
                        else fallback_evidence_ids
                    ),
                )
            )
        return items


def sanitize_replay_payload(value: Any) -> Any:
    """递归脱敏回放快照，避免保存密钥、本机私密路径或 `.env` 片段。"""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in _SENSITIVE_KEY_PARTS):
                cleaned[key_text] = "<redacted>"
            else:
                cleaned[key_text] = sanitize_replay_payload(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_replay_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_replay_payload(item) for item in value)
    if isinstance(value, str):
        sanitized = value.replace(".env", "<redacted-env>")
        for pattern in _SENSITIVE_PATH_PATTERNS:
            sanitized = pattern.sub("<redacted-path>", sanitized)
        return sanitized
    return value


def _approval_from_state(state: dict[str, Any]) -> dict[str, Any]:
    decision = state.get("approval_decision")
    if not decision:
        return {}
    return {
        "decision": decision,
        "operator_id": state.get("approval_operator_id") or state.get("operator_id"),
        "reason": state.get("approval_reason") or state.get("reason"),
    }


def _last_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[-1], dict):
        return dict(value[-1])
    if isinstance(value, dict):
        return dict(value)
    return {}


def _matching_observation(observations: Any, tool: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(observations, list):
        return {}
    tool_name = tool.get("tool_name")
    for observation in reversed(observations):
        if isinstance(observation, dict) and observation.get("tool_name") == tool_name:
            return dict(observation)
    return _last_dict(observations)


def _approval_decision_from_operator(operator_decision: Any) -> str | None:
    """把审计里的 operator_decision 映射成评估器使用的人审 decision。"""

    raw = str(operator_decision or "").lower()
    if raw in {"approved", "approve"}:
        return "approved"
    if raw in {"rejected", "reject"}:
        return "rejected"
    return None
