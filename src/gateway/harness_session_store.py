# -*- coding: utf-8 -*-
"""Phase 6C Harness Agent Web 会话持久化。

本模块只负责保存“副屏能看到的会话状态”，不保存 LangGraph checkpoint。
checkpoint 仍由官方 PostgresSaver 管理；这里的业务表用于 Web 查询 pending 审批、
节点路径、最终建议和审计结果。这样职责边界更清楚，也便于后续做回放和筛选。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import Settings

HarnessSessionStatus = Literal["pending_human", "approved", "rejected", "completed", "error", "expired", "locked"]


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
    # Phase 7B ???????????????????????? key ?????
    approval_expires_at: datetime | None = None
    locked_by: str | None = None
    lock_until: datetime | None = None
    idempotency_key: str | None = None
    approval_attempts: int = 0
    expired_reason: str | None = None
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
            # Phase 7B ??????
            "approval_expires_at": self.approval_expires_at.isoformat() if self.approval_expires_at else None,
            "locked_by": self.locked_by,
            "lock_until": self.lock_until.isoformat() if self.lock_until else None,
            "idempotency_key": self.idempotency_key,
            "approval_attempts": self.approval_attempts,
            "expired_reason": self.expired_reason,
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

    def save_terminal(self, record: HarnessSessionRecord) -> HarnessSessionRecord:
        """一次写入无 interrupt 的终态会话，并保持 trace 级幂等。

        该入口专门服务于默认关闭、明确失败等直接终止的 Graph 结果。它不允许先
        伪造 ``pending_human`` 再更新终态，否则进程在两次写之间崩溃会暴露一个
        实际不存在的审批请求。重复 trace 返回首个事实，避免重放覆盖审计证据。
        """

        if record.status == "pending_human":
            raise ValueError("terminal session cannot use pending_human status")
        current = self._records.get(record.trace_id)
        if current is not None:
            return current
        saved = replace(record, updated_at=datetime.now(timezone.utc))
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
        if current.status in {"completed", "rejected", "expired", "locked"}:
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



    # ----- Phase 7B ?????? -----

    def acquire_lock(self, trace_id: str, operator_id: str, lock_duration_seconds: int = 60) -> tuple[bool, str | HarnessSessionRecord]:
        """?????????????????????????

        Args:
            trace_id: ?????
            operator_id: ??????? ID?
            lock_duration_seconds: ???????? 60 ??

        Returns:
            (True, record) ?????? (False, "locked by xxx") ???????
        """
        now = datetime.now(timezone.utc)
        # ??????? operator ???????????????
        current = self._records.get(trace_id)
        if current:
            if current.locked_by == operator_id and current.lock_until and current.lock_until > now:
                return True, current
            if current.locked_by and current.locked_by != operator_id and current.lock_until and current.lock_until > now:
                return False, f"locked by {current.locked_by}"
        # ?????????
        released = replace(
            current or self._records[trace_id],
            locked_by=operator_id,
            lock_until=now + timedelta(seconds=lock_duration_seconds),
            updated_at=now,
        ) if trace_id in self._records else None
        if released:
            self._records[trace_id] = released
            return True, released
        raise HarnessSessionNotFoundError(trace_id)

    def renew_lock(self, trace_id: str, operator_id: str, lock_duration_seconds: int = 60) -> tuple[bool, str | HarnessSessionRecord]:
        """??????????????????????

        Returns:
            (True, record) ?????? (False, "not locked by {operator_id}")?
        """
        now = datetime.now(timezone.utc)
        current = self._records.get(trace_id)
        if current is None:
            raise HarnessSessionNotFoundError(trace_id)
        if current.locked_by != operator_id or not current.lock_until or current.lock_until <= now:
            return False, f"not locked by {operator_id}"
        renewed = replace(current, lock_until=now + timedelta(seconds=lock_duration_seconds), updated_at=now)
        self._records[trace_id] = renewed
        return True, renewed

    def expire_stale_pending(self, max_pending_minutes: int = 10) -> list[str]:
        """??????? pending ????? expired?

        Returns:
            ???? expired ? trace_id ???
        """
        now = datetime.now(timezone.utc)
        expired_ids: list[str] = []
        for trace_id, record in self._records.items():
            if record.status == "pending_human" and (now - record.created_at).total_seconds() > max_pending_minutes * 60:
                expired = replace(record, status="expired", expired_reason="approval_timeout", updated_at=now)
                self._records[trace_id] = expired
                expired_ids.append(trace_id)
        return expired_ids

    def submit_approval_with_idempotency(
        self,
        trace_id: str,
        decision: str,
        operator_id: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> HarnessSessionRecord:
        """??????????? key?????????????????

        Args:
            trace_id: ?????
            decision: ?????approved ? rejected?
            operator_id: ????? ID?
            reason: ?????
            idempotency_key: ???? key??? key ???????

        Raises:
            HarnessSessionNotFoundError: trace_id ????
            ValueError: ??? expired?locked ???????
            PermissionError: ???? operator ??????
        """
        now = datetime.now(timezone.utc)
        current = self._records.get(trace_id)
        if current is None:
            raise HarnessSessionNotFoundError(trace_id)

        # ????? key ????????
        if idempotency_key and current.idempotency_key == idempotency_key and current.status in {"approved", "rejected"}:
            return current

        # ????
        if current.status == "expired":
            raise ValueError("approval session expired")
        if current.status == "locked":
            raise ValueError("approval session locked")
        if current.status in {"approved", "rejected", "completed"}:
            return current
        if current.status != "pending_human":
            raise ValueError(f"approval session status {current.status} not submittable")

        # ????????????????? operator
        if current.locked_by and current.locked_by != operator_id and current.lock_until and current.lock_until > now:
            raise PermissionError(f"locked by {current.locked_by}")

        submitted = replace(
            current,
            status=decision,
            approval_decision=decision,
            operator_id=operator_id,
            reason=reason,
            idempotency_key=idempotency_key or current.idempotency_key,
            approval_attempts=current.approval_attempts + 1,
            updated_at=now,
        )
        self._records[trace_id] = submitted
        return submitted

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

    def save_terminal(self, record: HarnessSessionRecord) -> HarnessSessionRecord:
        """用单条 INSERT 原子创建终态会话，绝不经过旧审批状态。

        ``ON CONFLICT DO NOTHING`` 让相同 trace 的重放复用数据库中的首个权威事实。
        冲突时的只读查询不会产生中间状态；首次创建则由一个事务一次提交完整终态。
        """

        if record.status == "pending_human":
            raise ValueError("terminal session cannot use pending_human status")
        sql = """
            INSERT INTO live_agent_harness_sessions (
                trace_id, room_id, anchor_id, status, approval_request,
                interrupt_payload, latest_state, approval_decision, operator_id,
                reason, audit_status, audit_ids, decision_trace_ids
            )
            VALUES (
                %(trace_id)s, %(room_id)s, %(anchor_id)s, %(status)s,
                %(approval_request)s, %(interrupt_payload)s, %(latest_state)s,
                %(approval_decision)s, %(operator_id)s, %(reason)s,
                %(audit_status)s, %(audit_ids)s, %(decision_trace_ids)s
            )
            ON CONFLICT (trace_id) DO NOTHING
            RETURNING *;
        """
        params = {
            "trace_id": record.trace_id,
            "room_id": record.room_id,
            "anchor_id": record.anchor_id,
            "status": record.status,
            "approval_request": Jsonb(record.approval_request),
            "interrupt_payload": Jsonb(record.interrupt_payload),
            "latest_state": Jsonb(record.latest_state),
            "approval_decision": record.approval_decision,
            "operator_id": record.operator_id,
            "reason": record.reason,
            "audit_status": record.audit_status,
            "audit_ids": Jsonb(record.audit_ids),
            "decision_trace_ids": Jsonb(record.decision_trace_ids),
        }
        with psycopg.connect(
            **self._settings.postgres_connection_kwargs,
            row_factory=dict_row,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
        if row is None:
            return self.get(record.trace_id)
        return self._row_to_record(dict(row))

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
        if current.status in {"completed", "rejected", "expired", "locked"}:
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
            # Phase 7B ??????
            approval_expires_at=row.get("approval_expires_at"),
            locked_by=row.get("locked_by"),
            lock_until=row.get("lock_until"),
            idempotency_key=row.get("idempotency_key"),
            approval_attempts=int(row.get("approval_attempts") or 0),
            expired_reason=row.get("expired_reason"),
            updated_at=row["updated_at"],
        )



    # ----- Phase 7B ?????? -----

    def acquire_lock(self, trace_id: str, operator_id: str, lock_duration_seconds: int = 60) -> tuple[bool, str | HarnessSessionRecord]:
        """?????????? PostgreSQL ??????????

        Args:
            trace_id: ?????
            operator_id: ??????? ID?
            lock_duration_seconds: ???????? 60 ??

        Returns:
            (True, record) ?????? (False, "locked by xxx") ???????
        """
        sql = """
            UPDATE live_agent_harness_sessions
            SET locked_by = %(operator)s,
                lock_until = NOW() + make_interval(secs => %(duration)s),
                updated_at = NOW()
            WHERE trace_id = %(trace_id)s
              AND (locked_by IS NULL OR lock_until <= NOW())
            RETURNING *;
        """
        params = {"trace_id": trace_id, "operator": operator_id, "duration": lock_duration_seconds}
        try:
            record = self._fetch_one(sql, params)
            return True, record
        except HarnessSessionNotFoundError:
            # ??????????????
            current = self.get(trace_id)
            if current.locked_by and current.lock_until and current.lock_until > datetime.now(timezone.utc):
                return False, f"locked by {current.locked_by}"
            return False, "lock acquisition failed"

    def renew_lock(self, trace_id: str, operator_id: str, lock_duration_seconds: int = 60) -> tuple[bool, str | HarnessSessionRecord]:
        """??????????????????????

        Returns:
            (True, record) ?????? (False, "not locked by {operator_id}")?
        """
        sql = """
            UPDATE live_agent_harness_sessions
            SET lock_until = NOW() + make_interval(secs => %(duration)s),
                updated_at = NOW()
            WHERE trace_id = %(trace_id)s
              AND locked_by = %(operator)s
              AND lock_until > NOW()
            RETURNING *;
        """
        params = {"trace_id": trace_id, "operator": operator_id, "duration": lock_duration_seconds}
        try:
            record = self._fetch_one(sql, params)
            return True, record
        except HarnessSessionNotFoundError:
            return False, f"not locked by {operator_id}"

    def expire_stale_pending(self, max_pending_minutes: int = 10) -> list[str]:
        """??????? pending ????? expired?

        Returns:
            ???? expired ? trace_id ???
        """
        sql = """
            UPDATE live_agent_harness_sessions
            SET status = 'expired',
                expired_reason = 'approval_timeout',
                updated_at = NOW()
            WHERE status = 'pending_human'
              AND created_at < NOW() - make_interval(mins => %(max_min)s)
            RETURNING trace_id;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"max_min": max_pending_minutes})
                rows = cur.fetchall()
            conn.commit()
        return [dict(row)["trace_id"] for row in rows]

    def submit_approval_with_idempotency(
        self,
        trace_id: str,
        decision: str,
        operator_id: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> HarnessSessionRecord:
        """??????????? key?????????????????

        Args:
            trace_id: ?????
            decision: ?????approved ? rejected?
            operator_id: ????? ID?
            reason: ?????
            idempotency_key: ???? key??? key ???????

        Raises:
            ValueError: ??? expired ? locked ????
            PermissionError: ???? operator ??????
        """
        current = self.get(trace_id)

        # ????? key ????????
        if idempotency_key and current.idempotency_key == idempotency_key and current.status in {"approved", "rejected"}:
            return current

        # ????
        if current.status == "expired":
            raise ValueError("approval session expired")
        if current.status == "locked":
            raise ValueError("approval session locked")
        if current.status in {"approved", "rejected", "completed"}:
            return current
        if current.status != "pending_human":
            raise ValueError(f"approval session status {current.status} not submittable")

        # ???
        if current.locked_by and current.locked_by != operator_id and current.lock_until and current.lock_until > datetime.now(timezone.utc):
            raise PermissionError(f"locked by {current.locked_by}")

        sql = """
            UPDATE live_agent_harness_sessions
            SET status = %(status)s,
                approval_decision = %(decision)s,
                operator_id = %(operator_id)s,
                reason = %(reason)s,
                idempotency_key = COALESCE(%(idempotency_key)s, idempotency_key),
                approval_attempts = approval_attempts + 1,
                updated_at = NOW()
            WHERE trace_id = %(trace_id)s
              AND status = 'pending_human'
            RETURNING *;
        """
        params = {
            "trace_id": trace_id,
            "status": decision,
            "decision": decision,
            "operator_id": operator_id,
            "reason": reason,
            "idempotency_key": idempotency_key,
        }
        return self._fetch_one(sql, params)

def initialize_harness_session_schema(settings: Settings) -> None:
    """初始化 Phase 6C Harness 会话表。"""

    project_root = Path(__file__).resolve().parents[2]
    sql = (project_root / "docker" / "init_phase6c_harness_sessions.sql").read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
