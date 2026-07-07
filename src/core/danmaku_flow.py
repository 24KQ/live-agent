"""Phase 2C 播中弹幕聚合与参考回复服务。

该服务串联弹幕事件校验、工具注册、安全 Hook、确定性聚合、模板回复和
PostgreSQL 审计写入。它不调用 Reducer，因为本阶段的弹幕处理不改变商品状态；
后续如果弹幕触发切品、改价或发券，再必须重新经过 Reducer 和更高风险门禁。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.audit.tool_call_audit import AuditEvent, ToolCallAuditStore
from src.config.tool_registry import get_default_tool_registry
from src.core.security_hooks import evaluate_tool_gate
from src.skills.danmaku_aggregator import DanmakuQuestionGroup, aggregate_danmaku_questions
from src.skills.danmaku_events import DanmakuEvent
from src.skills.danmaku_reply_generator import DanmakuReply, generate_danmaku_reply
from src.state.models import ActionType, LifecycleStage, LiveRoomState


@dataclass(frozen=True)
class DanmakuFlowResult:
    """弹幕聚合闭环的结构化结果。"""

    updated_state: LiveRoomState
    groups: list[DanmakuQuestionGroup]
    replies: list[DanmakuReply]
    audit_ids: list[str] = field(default_factory=list)
    trace_id: str = ""


class DanmakuFlowService:
    """播中弹幕应用服务。"""

    def __init__(self, audit_store: ToolCallAuditStore) -> None:
        self._audit_store = audit_store
        self._registry = get_default_tool_registry()

    def handle_danmaku_batch(self, state: LiveRoomState, events: list[DanmakuEvent]) -> DanmakuFlowResult:
        """处理一批本地模拟弹幕事件。

        流程固定为：生命周期校验 -> 同房间校验 -> 5 秒窗口聚合 -> 生成参考回复
        -> 写入审计。当前阶段只生成参考回复，不自动发送，也不修改状态。
        """

        if state.lifecycle != LifecycleStage.ON_LIVE:
            raise ValueError(f"danmaku batch can only be handled in ON_LIVE, current lifecycle is {state.lifecycle}")
        if not events:
            raise ValueError("danmaku events cannot be empty")
        if any(event.room_id != state.room_id for event in events):
            raise ValueError("danmaku event room_id must match state room_id")

        trace_id = events[0].trace_id
        audit_ids: list[str] = []
        groups = self._aggregate_questions(state=state, events=events, audit_ids=audit_ids)
        replies = self._generate_replies(state=state, groups=groups, audit_ids=audit_ids)

        return DanmakuFlowResult(
            updated_state=state,
            groups=groups,
            replies=replies,
            audit_ids=audit_ids,
            trace_id=trace_id,
        )

    def _aggregate_questions(
        self,
        state: LiveRoomState,
        events: list[DanmakuEvent],
        audit_ids: list[str],
    ) -> list[DanmakuQuestionGroup]:
        """调用聚合工具并写入聚合摘要审计。"""

        tool = self._require_on_live_tool("aggregate_danmaku_questions")
        gate = evaluate_tool_gate(tool, confirmed=True)
        groups = aggregate_danmaku_questions(events, window_seconds=5)
        trace_id = events[0].trace_id
        audit_ids.append(
            self._audit_store.record_event(
                AuditEvent(
                    trace_id=trace_id,
                    room_id=state.room_id,
                    tool_name=tool.name,
                    action_type=ActionType.AGGREGATE_DANMAKU_QUESTIONS,
                    risk_level=tool.risk_level,
                    gate_decision=gate.decision,
                    operator_decision="approved",
                    request_payload={"event_count": len(events), "window_seconds": 5},
                    result_payload={
                        "group_count": len(groups),
                        "groups": [_group_to_audit_payload(group) for group in groups],
                    },
                )
            )
        )
        return groups

    def _generate_replies(
        self,
        state: LiveRoomState,
        groups: list[DanmakuQuestionGroup],
        audit_ids: list[str],
    ) -> list[DanmakuReply]:
        """为每个聚合问题生成参考回复并写入审计。"""

        tool = self._require_on_live_tool("generate_danmaku_reply")
        gate = evaluate_tool_gate(tool, confirmed=True)
        replies: list[DanmakuReply] = []
        for group in groups:
            reply = generate_danmaku_reply(group)
            replies.append(reply)
            audit_ids.append(
                self._audit_store.record_event(
                    AuditEvent(
                        trace_id=group.trace_id,
                        room_id=state.room_id,
                        tool_name=tool.name,
                        action_type=ActionType.GENERATE_DANMAKU_REPLY,
                        risk_level=tool.risk_level,
                        gate_decision=gate.decision,
                        operator_decision="approved",
                        request_payload=_group_to_audit_payload(group),
                        result_payload=_reply_to_audit_payload(reply),
                    )
                )
            )
        return replies

    def _require_on_live_tool(self, tool_name: str):
        """读取播中工具元数据，并确保工具只在 ON_LIVE 阶段开放。"""

        tool = self._registry.get(tool_name)
        if not self._registry.is_available(tool.name, LifecycleStage.ON_LIVE):
            raise ValueError(f"tool {tool.name} is not available in ON_LIVE")
        return tool


def _group_to_audit_payload(group: DanmakuQuestionGroup) -> dict[str, object]:
    """把聚合结果转换为 JSONB 可写入的审计 payload。"""

    return {
        "category": group.category.value,
        "summary": group.summary,
        "count": group.count,
        "sample_contents": group.sample_contents,
        "window_start": group.window_start.isoformat(),
        "window_end": group.window_end.isoformat(),
    }


def _reply_to_audit_payload(reply: DanmakuReply) -> dict[str, object]:
    """把参考回复转换为 JSONB 可写入的审计 payload。"""

    return {
        "category": reply.category.value,
        "summary": reply.summary,
        "reply_text": reply.reply_text,
        "risk_tips": reply.risk_tips,
        "confidence": reply.confidence,
        "requires_human_review": reply.requires_human_review,
    }
