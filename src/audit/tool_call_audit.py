"""工具调用审计写入。

Phase 1 只把工具调用和状态变更写入 PostgreSQL 审计表，不持久化商品状态。
审计写入是高风险动作闭环的一部分：如果审计失败，调用方不能宣称业务动作
已经安全完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.config.settings import Settings
from src.core.security_hooks import GateDecision
from src.state.models import ActionType, RiskLevel


class AuditWriteError(RuntimeError):
    """审计写入失败。"""


@dataclass(frozen=True)
class AuditEvent:
    """待写入数据库的审计事件。"""

    trace_id: str
    room_id: str
    tool_name: str
    action_type: ActionType
    risk_level: RiskLevel
    gate_decision: GateDecision
    operator_decision: str | None
    request_payload: dict[str, Any] = field(default_factory=dict)
    result_payload: dict[str, Any] = field(default_factory=dict)


class ToolCallAuditStore:
    """PostgreSQL 工具调用审计 Store。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def record_event(self, event: AuditEvent) -> str:
        """写入一条审计事件并返回 audit_id。

        使用 psycopg 参数绑定写入 JSONB，避免手动拼接 SQL 带来的转义和注入风险。
        捕获所有数据库异常并转换成领域错误，让上层流程可以明确失败。
        """

        sql = """
            INSERT INTO tool_call_audit (
                trace_id,
                room_id,
                tool_name,
                action_type,
                risk_level,
                gate_decision,
                operator_decision,
                request_payload,
                result_payload
            )
            VALUES (
                %(trace_id)s,
                %(room_id)s,
                %(tool_name)s,
                %(action_type)s,
                %(risk_level)s,
                %(gate_decision)s,
                %(operator_decision)s,
                %(request_payload)s,
                %(result_payload)s
            )
            RETURNING audit_id;
        """
        try:
            with psycopg.connect(**self._settings.postgres_connection_kwargs) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql,
                        {
                            "trace_id": event.trace_id,
                            "room_id": event.room_id,
                            "tool_name": event.tool_name,
                            "action_type": event.action_type.value,
                            "risk_level": event.risk_level.value,
                            "gate_decision": event.gate_decision.value,
                            "operator_decision": event.operator_decision,
                            "request_payload": psycopg.types.json.Jsonb(event.request_payload),
                            "result_payload": psycopg.types.json.Jsonb(event.result_payload),
                        },
                    )
                    audit_id = cursor.fetchone()[0]
                connection.commit()
            return str(audit_id)
        except Exception as exc:  # noqa: BLE001 - 审计失败必须被统一转换为业务可读错误。
            raise AuditWriteError(f"failed to write tool call audit: {exc}") from exc

    def get_event_by_trace_id(self, trace_id: str) -> dict[str, Any] | None:
        """按 trace_id 读取最近一条审计事件，供测试和排障使用。"""

        sql = """
            SELECT
                audit_id::text,
                trace_id,
                room_id,
                tool_name,
                action_type,
                risk_level,
                gate_decision,
                operator_decision,
                request_payload,
                result_payload,
                created_at
            FROM tool_call_audit
            WHERE trace_id = %(trace_id)s
            ORDER BY created_at DESC
            LIMIT 1;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, {"trace_id": trace_id})
                row = cursor.fetchone()
        return dict(row) if row is not None else None
