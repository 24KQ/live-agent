# -*- coding: utf-8 -*-
"""Phase 6C Harness Agent Web 会话持久化。

本模块只负责保存“副屏能看到的会话状态”，不保存 LangGraph checkpoint。
checkpoint 仍由官方 PostgresSaver 管理；这里的业务表用于 Web 查询 pending 审批、
节点路径、最终建议和审计结果。这样职责边界更清楚，也便于后续做回放和筛选。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import Settings

HarnessSessionStatus = Literal["pending_human", "approved", "rejected", "completed", "error"]


class HarnessSessionNotFoundError(KeyError):
    """按 trace_id 找不到 Harness Web 会话。"""


@dataclass(frozen=True)
class HarnessSessionRecord:
    """副屏展示用的 Harness 会话快照。

    这里所有复杂字段都保持为 dict/list，保证能直接写入 JSONB，也能直接返回给
    FastAPI。`updated_at` 用于副屏按房间读取最近会话。
    """

    trace_id: str
    room_id: str
    anchor_id: str | None = None
    status: HarnessSessionStatus = "pending_human"
    approval_request: dict[str, Any] = field(default_factory=dict)
    interrupt_payload: dict[str, Any] = field(default_factory=dict)
    latest_state: dict[str, Any] = field(default_factory=dict)
    approval_decision: str | None = None
    operator_id: str | None = None
    reason: str | None = None
    audit_status: str | None = None
    audit_ids: list[str] = field(default_factory=list)
    decision_trace_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_status_payload(self) -> dict[str, Any]:
        """转换为 API/前端统一使用的状态 payload。"""

        state = self.latest_state or {}
        return {
            "trace_id": self.trace_id,
            "room_id": self.room_id,
            "anchor_id": self.anchor_id,
            "status": self.status,
            "agent_status": state.get("agent_status") or self.status,
            "pending_approval": self.status == "pending_human",
            "approval_request": self.approval_request,
            "interrupt_payload": self.interrupt_payload,
            "approval_decision": self.approval_decision,
            "operator_id": self.operator_id,
            "reason": self.reason,
            "completed_nodes": state.get("completed_nodes", []),
            "executed_tools": state.get("executed_tools", []),
            "observations": state.get("observations", []),
            "final_suggestion": state.get("final_suggestion"),
            "audit_status": self.audit_status or state.get("audit_status"),
            "audit_ids": self.audit_ids or state.get("audit_ids", []),
            "decision_trace_ids": self.decision_trace_ids or state.get("decision_trace_ids", []),
            "error": state.get("error"),
            "updated_at": self.updated_at.isoformat(),
        }


class InMemoryHarnessSessionStore:
    """单元测试和无数据库演示用的内存 Store。

    生产入口应使用 `PostgresHarnessSessionStore`；内存 Store 只用于快速验证服务行为，
    避免单元测试依赖本机 PostgreSQL。
    """

    def __init__(self) -> None:
        self._records: dict[str, HarnessSessionRecord] = {}
        self._order: list[str] = []

    def save_pending(self, record: HarnessSessionRecord) -> HarnessSessionRecord:
        saved = replace(record, status="pending_human", updated_at=datetime.now(timezone.utc))
        if saved.trace_id not in self._records:
            self._order.append(saved.trace_id)
        self._records[saved.trace_id] = saved
        return saved

    def get(self, trace_id: str) -> HarnessSessionRecord:
        try:
            return self._records[trace_id]
        except KeyError as exc:
            raise HarnessSessionNotFoundError(trace_id) from exc

    def latest_for_room(self, room_id: str, limit: int = 5) -> list[HarnessSessionRecord]:
        records = [self._records[trace_id] for trace_id in reversed(self._order)]
        return [record for record in records if record.room_id == room_id][:limit]

    def save_final_state(
        self,
        *,
        trace_id: str,
        status: HarnessSessionStatus,
        latest_state: dict[str, Any],
        approval_decision: str | None,
        operator_id: str | None,
        reason: str | None,
        audit_status: str | None,
        audit_ids: list[str] | None = None,
        decision_trace_ids: list[str] | None = None,
    ) -> HarnessSessionRecord:
        current = self.get(trace_id)
        if current.status in {"completed", "rejected"}:
            return current
        saved = replace(
            current,
            status=status,
            latest_state=latest_state,
            approval_decision=approval_decision,
            operator_id=operator_id,
            reason=reason,
            audit_status=audit_status,
            audit_ids=audit_ids or [],
            decision_trace_ids=decision_trace_ids or [],
            updated_at=datetime.now(timezone.utc),
        )
        self._records[trace_id] = saved
        return saved


class PostgresHarnessSessionStore:
    """PostgreSQL 版本 Harness 会话 Store。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def initialize_schema(self) -> None:
        initialize_harness_session_schema(self._settings)

    def save_pending(self, record: HarnessSessionRecord) -> HarnessSessionRecord:
        sql = """
            INSERT INTO live_agent_harness_sessions (
                trace_id, room_id, anchor_id, status, approval_request,
                interrupt_payload, latest_state, audit_ids, decision_trace_ids
            )
            VALUES (
                %(trace_id)s, %(room_id)s, %(anchor_id)s, 'pending_human',
                %(approval_request)s, %(interrupt_payload)s, %(latest_state)s,
                %(audit_ids)s, %(decision_trace_ids)s
            )
            ON CONFLICT (trace_id) DO UPDATE SET
                room_id = EXCLUDED.room_id,
                anchor_id = EXCLUDED.anchor_id,
                status = EXCLUDED.status,
                approval_request = EXCLUDED.approval_request,
                interrupt_payload = EXCLUDED.interrupt_payload,
                latest_state = EXCLUDED.latest_state,
                updated_at = NOW()
            RETURNING *;
        """
        return self._fetch_one(
            sql,
            {
                "trace_id": record.trace_id,
                "room_id": record.room_id,
                "anchor_id": record.anchor_id,
                "approval_request": Jsonb(record.approval_request),
                "interrupt_payload": Jsonb(record.interrupt_payload),
                "latest_state": Jsonb(record.latest_state),
                "audit_ids": Jsonb(record.audit_ids),
                "decision_trace_ids": Jsonb(record.decision_trace_ids),
            },
        )

    def get(self, trace_id: str) -> HarnessSessionRecord:
        sql = "SELECT * FROM live_agent_harness_sessions WHERE trace_id = %(trace_id)s;"
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"trace_id": trace_id})
                row = cur.fetchone()
        if row is None:
            raise HarnessSessionNotFoundError(trace_id)
        return self._row_to_record(dict(row))

    def latest_for_room(self, room_id: str, limit: int = 5) -> list[HarnessSessionRecord]:
        sql = """
            SELECT * FROM live_agent_harness_sessions
            WHERE room_id = %(room_id)s
            ORDER BY updated_at DESC
            LIMIT %(limit)s;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"room_id": room_id, "limit": limit})
                rows = cur.fetchall()
        return [self._row_to_record(dict(row)) for row in rows]

    def save_final_state(
        self,
        *,
        trace_id: str,
        status: HarnessSessionStatus,
        latest_state: dict[str, Any],
        approval_decision: str | None,
        operator_id: str | None,
        reason: str | None,
        audit_status: str | None,
        audit_ids: list[str] | None = None,
        decision_trace_ids: list[str] | None = None,
    ) -> HarnessSessionRecord:
        current = self.get(trace_id)
        if current.status in {"completed", "rejected"}:
            return current
        sql = """
            UPDATE live_agent_harness_sessions
            SET status = %(status)s,
                latest_state = %(latest_state)s,
                approval_decision = %(approval_decision)s,
                operator_id = %(operator_id)s,
                reason = %(reason)s,
                audit_status = %(audit_status)s,
                audit_ids = %(audit_ids)s,
                decision_trace_ids = %(decision_trace_ids)s,
                updated_at = NOW()
            WHERE trace_id = %(trace_id)s
            RETURNING *;
        """
        return self._fetch_one(
            sql,
            {
                "trace_id": trace_id,
                "status": status,
                "latest_state": Jsonb(latest_state),
                "approval_decision": approval_decision,
                "operator_id": operator_id,
                "reason": reason,
                "audit_status": audit_status,
                "audit_ids": Jsonb(audit_ids or []),
                "decision_trace_ids": Jsonb(decision_trace_ids or []),
            },
        )

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> HarnessSessionRecord:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise HarnessSessionNotFoundError(params.get("trace_id", ""))
        return self._row_to_record(dict(row))

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> HarnessSessionRecord:
        return HarnessSessionRecord(
            trace_id=row["trace_id"],
            room_id=row["room_id"],
            anchor_id=row.get("anchor_id"),
            status=row["status"],
            approval_request=dict(row.get("approval_request") or {}),
            interrupt_payload=dict(row.get("interrupt_payload") or {}),
            latest_state=dict(row.get("latest_state") or {}),
            approval_decision=row.get("approval_decision"),
            operator_id=row.get("operator_id"),
            reason=row.get("reason"),
            audit_status=row.get("audit_status"),
            audit_ids=list(row.get("audit_ids") or []),
            decision_trace_ids=list(row.get("decision_trace_ids") or []),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def initialize_harness_session_schema(settings: Settings) -> None:
    """初始化 Phase 6C Harness 会话表。"""

    project_root = Path(__file__).resolve().parents[2]
    sql = (project_root / "docker" / "init_phase6c_harness_sessions.sql").read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
