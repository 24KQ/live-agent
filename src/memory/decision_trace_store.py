"""Phase 3A Decision Trace PostgreSQL Store。

Decision Trace 记录“建议 -> 主播反馈 -> 业务结果 -> trust_score 变化”的闭环证据，
用于后续复盘 Agent 的建议是否真的帮助主播，而不是只看单次工具调用是否成功。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import Settings
from src.memory.models import AnchorAction, BusinessResult, DecisionTraceRecord


class DecisionTraceStore:
    """Decision Trace 数据库仓储。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def record_trace(self, record: DecisionTraceRecord) -> str:
        """写入或更新一条决策轨迹，并返回 decision_trace_id。

        trace_id 使用唯一约束，方便 CLI 和集成测试重复执行同一个演示而不产生重复记录。
        如果同一 trace_id 已存在且内容完全一致，返回原记录 ID；如果内容不同，则拒绝覆盖，
        避免 Decision Trace 被后续运行悄悄改写。
        """

        validated = DecisionTraceRecord.model_validate(record.model_dump(mode="python"))
        self._ensure_room_belongs_to_anchor(validated.anchor_id, validated.room_id)
        existing = self._get_existing_trace(validated.trace_id)
        if existing is not None:
            if self._is_same_trace(existing, validated):
                return str(existing.decision_trace_id)
            raise ValueError("trace_id already exists with different decision trace content")

        sql = """
            INSERT INTO live_agent_decision_trace (
                trace_id,
                anchor_id,
                room_id,
                recommendation,
                anchor_action,
                business_result,
                lift,
                trust_delta,
                final_trust_score
            )
            VALUES (
                %(trace_id)s,
                %(anchor_id)s,
                %(room_id)s,
                %(recommendation)s,
                %(anchor_action)s,
                %(business_result)s,
                %(lift)s,
                %(trust_delta)s,
                %(final_trust_score)s
            )
            RETURNING decision_trace_id::text;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql,
                    {
                        "trace_id": validated.trace_id,
                        "anchor_id": validated.anchor_id,
                        "room_id": validated.room_id,
                        "recommendation": Jsonb(validated.recommendation),
                        "anchor_action": validated.anchor_action.value,
                        "business_result": validated.business_result.value,
                        "lift": validated.lift,
                        "trust_delta": validated.trust_delta,
                        "final_trust_score": validated.final_trust_score,
                    },
                )
                decision_trace_id = cursor.fetchone()[0]
            connection.commit()
        return str(decision_trace_id)

    def list_traces(self, trace_id: str) -> list[DecisionTraceRecord]:
        """按 trace_id 读取决策轨迹，供测试、CLI 回放和后续复盘使用。"""

        if not trace_id or not trace_id.strip():
            raise ValueError("trace_id must not be empty")
        sql = """
            SELECT
                decision_trace_id::text,
                trace_id,
                anchor_id,
                room_id,
                recommendation,
                anchor_action,
                business_result,
                lift,
                trust_delta,
                final_trust_score,
                created_at
            FROM live_agent_decision_trace
            WHERE trace_id = %(trace_id)s
            ORDER BY created_at ASC, decision_trace_id ASC;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, {"trace_id": trace_id})
                rows = cursor.fetchall()
        return [self._row_to_trace(row) for row in rows]

    def _get_existing_trace(self, trace_id: str) -> DecisionTraceRecord | None:
        """读取同一 trace_id 的现有记录，用于幂等判断。"""

        traces = self.list_traces(trace_id)
        return traces[0] if traces else None

    def _ensure_room_belongs_to_anchor(self, anchor_id: str, room_id: str) -> None:
        """校验 Decision Trace 中的直播间和主播归属一致。"""

        sql = """
            SELECT 1
            FROM live_agent_live_rooms
            WHERE room_id = %(room_id)s AND anchor_id = %(anchor_id)s;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, {"room_id": room_id, "anchor_id": anchor_id})
                row = cursor.fetchone()
        if row is None:
            raise ValueError("room_id does not belong to anchor_id")

    @staticmethod
    def _is_same_trace(existing: DecisionTraceRecord, incoming: DecisionTraceRecord) -> bool:
        """判断重复写入是否为同一条决策轨迹。

        created_at 和数据库生成的 decision_trace_id 不参与比较；其余业务字段必须一致，才允许
        视为幂等重放。
        """

        return (
            existing.trace_id == incoming.trace_id
            and existing.anchor_id == incoming.anchor_id
            and existing.room_id == incoming.room_id
            and existing.recommendation == incoming.recommendation
            and existing.anchor_action == incoming.anchor_action
            and existing.business_result == incoming.business_result
            and existing.lift == incoming.lift
            and existing.trust_delta == incoming.trust_delta
            and existing.final_trust_score == incoming.final_trust_score
        )

    @staticmethod
    def _row_to_trace(row: dict[str, Any]) -> DecisionTraceRecord:
        """把数据库行转换为 DecisionTraceRecord。"""

        return DecisionTraceRecord(
            decision_trace_id=row["decision_trace_id"],
            trace_id=row["trace_id"],
            anchor_id=row["anchor_id"],
            room_id=row["room_id"],
            recommendation=dict(row["recommendation"] or {}),
            anchor_action=AnchorAction(row["anchor_action"]),
            business_result=BusinessResult(row["business_result"]),
            lift=Decimal(str(row["lift"])),
            trust_delta=Decimal(str(row["trust_delta"])),
            final_trust_score=Decimal(str(row["final_trust_score"])),
            created_at=row["created_at"],
        )
