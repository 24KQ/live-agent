"""Phase 11B 执行尝试事实存储。

本模块把“是否已经向外部平台发起过一次操作”从 ToolCallAudit 中分离出来。
Operation 用业务幂等键标识一次用户可见动作，Attempt 记录该动作唯一的外部
尝试。副作用未知也必须成为可重放的终态，禁止调用方借由重复请求再次写平台。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from pydantic import BaseModel, Field, field_validator
from psycopg.rows import dict_row

from src.skill_runtime.models import FailureFact


class AttemptInvariantError(RuntimeError):
    """Operation/Attempt 状态违背不可变事实或合法迁移时抛出。"""


class AttemptState(StrEnum):
    """Phase 11B 单次外部尝试允许的持久化状态。"""

    INTENT_RECORDED = "INTENT_RECORDED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SIDE_EFFECT_UNKNOWN = "SIDE_EFFECT_UNKNOWN"


class OperationRequest(BaseModel, frozen=True):
    """创建或重放 Operation 所需的不可变业务事实。

    这里不接收 trace_id 作为唯一键，因为同一业务操作可能由网络重试、Graph
    恢复或不同 trace 重放。skill/version/room/idempotency_key 才是 D-058
    固定的幂等身份；意图载荷摘要用于拒绝同键不同事实。
    """

    skill_id: str = Field(..., min_length=1)
    skill_version: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    deadline_at: datetime
    intent_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("deadline_at")
    @classmethod
    def _deadline_must_be_aware(cls, value: datetime) -> datetime:
        """Store 只持久化 UTC deadline，避免数据库与进程时区分歧。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must include timezone information")
        return value.astimezone(timezone.utc)

    @property
    def operation_id(self) -> str:
        """从固定业务身份生成稳定 UUID，便于跨进程按同一 Operation 定位。"""
        identity = "\x1f".join(
            (self.skill_id, self.skill_version, self.room_id, self.idempotency_key)
        )
        return str(uuid5(NAMESPACE_URL, identity))

    @property
    def intent_digest(self) -> str:
        """生成顺序无关的 JSON 业务事实摘要，拒绝同键不同请求。"""
        import json

        encoded = json.dumps(
            self.intent_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AttemptRecord:
    """一个 Operation 的唯一执行尝试及其不可变终态。"""

    operation_id: str
    attempt_id: str
    request: OperationRequest
    state: AttemptState
    created_at: datetime
    terminal_payload: dict[str, Any] | None = None
    failure: FailureFact | None = None


@dataclass(frozen=True)
class ClaimResult:
    """claim_or_replay 的结果，created 区分首次执行与安全重放。"""

    record: AttemptRecord
    created: bool


class AttemptStore(Protocol):
    """Executor 使用的最小 Store 契约，不把数据库实现泄漏到 Handler。"""

    def claim_or_replay(self, request: OperationRequest) -> ClaimResult:
        """原子创建意图或返回已有 Operation 的唯一 Attempt。"""

    def complete_success(self, attempt_id: str, payload: dict[str, Any]) -> AttemptRecord:
        """把等待终态的 Attempt 闭合为确认成功。"""

    def complete_failure(self, attempt_id: str, failure: FailureFact) -> AttemptRecord:
        """把等待终态的 Attempt 闭合为确定失败或副作用未知。"""


class InMemoryAttemptStore:
    """测试和无外部依赖 Demo 使用的线程安全 Attempt Store。

    该实现的锁只用于保证单进程测试中的原子 claim；生产多进程语义由后续
    PostgreSQLAttemptStore 使用唯一约束实现，不能把本锁误当成分布式互斥。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._records_by_operation: dict[str, AttemptRecord] = {}
        self._operation_digest: dict[str, str] = {}
        self._records_by_attempt: dict[str, AttemptRecord] = {}

    def claim_or_replay(self, request: OperationRequest) -> ClaimResult:
        """先记录意图，再允许上层调用 Adapter；同键不同意图 fail-closed。"""
        operation_id = request.operation_id
        with self._lock:
            existing = self._records_by_operation.get(operation_id)
            if existing is not None:
                if self._operation_digest[operation_id] != request.intent_digest:
                    raise AttemptInvariantError("conflicting operation replay")
                return ClaimResult(record=existing, created=False)

            record = AttemptRecord(
                operation_id=operation_id,
                attempt_id=str(uuid4()),
                request=request,
                state=AttemptState.INTENT_RECORDED,
                created_at=datetime.now(timezone.utc),
            )
            self._records_by_operation[operation_id] = record
            self._records_by_attempt[record.attempt_id] = record
            self._operation_digest[operation_id] = request.intent_digest
            return ClaimResult(record=record, created=True)

    def complete_success(self, attempt_id: str, payload: dict[str, Any]) -> AttemptRecord:
        """只允许从已写意图状态转换到确认成功。"""
        with self._lock:
            record = self._awaiting_terminal_record(attempt_id)
            completed = replace(
                record,
                state=AttemptState.SUCCEEDED,
                terminal_payload=dict(payload),
            )
            self._replace_record(completed)
            return completed

    def complete_failure(self, attempt_id: str, failure: FailureFact) -> AttemptRecord:
        """保存 FailureFact，并按副作用确认状态选择不可逆终态。"""
        if failure.attempt_id != attempt_id:
            raise AttemptInvariantError("failure attempt_id does not match record")
        with self._lock:
            record = self._awaiting_terminal_record(attempt_id)
            state = (
                AttemptState.SIDE_EFFECT_UNKNOWN
                if failure.category.value == "SIDE_EFFECT_UNKNOWN"
                else AttemptState.FAILED
            )
            completed = replace(record, state=state, failure=failure)
            self._replace_record(completed)
            return completed

    def _awaiting_terminal_record(self, attempt_id: str) -> AttemptRecord:
        """读取仍可闭合的意图记录，防止成功/失败终态被二次覆盖。"""
        record = self._records_by_attempt.get(attempt_id)
        if record is None:
            raise AttemptInvariantError("attempt not found")
        if record.state != AttemptState.INTENT_RECORDED:
            raise AttemptInvariantError("attempt is not awaiting terminal result")
        return record

    def _replace_record(self, record: AttemptRecord) -> None:
        """同时更新两份索引，保证按 Operation 和 Attempt 读取同一事实。"""
        self._records_by_operation[record.operation_id] = record
        self._records_by_attempt[record.attempt_id] = record


class PostgresAttemptStore:
    """生产进程使用的 PostgreSQL Attempt Store。

    Operation 的数据库唯一约束是跨进程幂等性的权威来源。所有读取与条件更新使用
    READ COMMITTED：冲突 INSERT 等待胜者提交后，紧随其后的 SELECT 必须读取到
    已提交的首次意图，不能把并发重放误判为记录丢失。
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def claim_or_replay(self, request: OperationRequest) -> ClaimResult:
        """原子写入 Operation/Attempt 意图，或安全读取相同业务事实。"""
        insert_operation_sql = """
            INSERT INTO skill_execution_operations (
                operation_id, skill_id, skill_version, room_id, idempotency_key,
                request_digest, deadline_at, intent_payload
            ) VALUES (
                %(operation_id)s, %(skill_id)s, %(skill_version)s, %(room_id)s,
                %(idempotency_key)s, %(request_digest)s, %(deadline_at)s, %(intent_payload)s
            )
            -- operation_id 由同一业务身份确定性派生，因此并发首写既可能命中
            -- 业务唯一索引，也可能先命中主键。两种冲突都表示“已有胜者”，后续
            -- 必须读取并校验首次意图，而不是把主键冲突误报为基础设施失败。
            ON CONFLICT DO NOTHING
            RETURNING operation_id::text AS operation_id;
        """
        insert_attempt_sql = """
            INSERT INTO skill_execution_attempts (attempt_id, operation_id, state)
            VALUES (%(attempt_id)s, %(operation_id)s, %(state)s)
            RETURNING attempt_id::text AS attempt_id;
        """
        parameters = {
            "operation_id": request.operation_id,
            "skill_id": request.skill_id,
            "skill_version": request.skill_version,
            "room_id": request.room_id,
            "idempotency_key": request.idempotency_key,
            "request_digest": request.intent_digest,
            "deadline_at": request.deadline_at,
            "intent_payload": psycopg.types.json.Jsonb(request.intent_payload),
        }
        try:
            with psycopg.connect(
                **self._settings.postgres_connection_kwargs,
                row_factory=dict_row,
            ) as connection:
                connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                with connection.cursor() as cursor:
                    cursor.execute(insert_operation_sql, parameters)
                    inserted = cursor.fetchone()
                    if inserted is not None:
                        attempt_id = str(uuid4())
                        cursor.execute(
                            insert_attempt_sql,
                            {
                                "attempt_id": attempt_id,
                                "operation_id": request.operation_id,
                                "state": AttemptState.INTENT_RECORDED.value,
                            },
                        )
                        record = self._load_record(cursor, request.operation_id)
                        if record is None:
                            raise AttemptInvariantError("inserted operation cannot be loaded")
                        connection.commit()
                        return ClaimResult(record=record, created=True)

                    record = self._load_record(cursor, request.operation_id)
                    if record is None:
                        raise AttemptInvariantError("conflicting operation cannot be loaded")
                    if record.request.intent_digest != request.intent_digest:
                        raise AttemptInvariantError("conflicting operation replay")
                connection.commit()
                return ClaimResult(record=record, created=False)
        except AttemptInvariantError:
            raise
        except psycopg.Error as exc:
            raise AttemptInvariantError("failed to claim execution attempt") from exc

    def complete_success(self, attempt_id: str, payload: dict[str, Any]) -> AttemptRecord:
        """条件更新意图状态为成功，拒绝覆盖已有终态。"""
        return self._complete(
            attempt_id=attempt_id,
            state=AttemptState.SUCCEEDED,
            terminal_payload=payload,
            failure=None,
        )

    def complete_failure(self, attempt_id: str, failure: FailureFact) -> AttemptRecord:
        """保存失败事实，并将副作用未知作为独立不可自动重放的终态。"""
        if failure.attempt_id != attempt_id:
            raise AttemptInvariantError("failure attempt_id does not match record")
        state = (
            AttemptState.SIDE_EFFECT_UNKNOWN
            if failure.category.value == "SIDE_EFFECT_UNKNOWN"
            else AttemptState.FAILED
        )
        return self._complete(
            attempt_id=attempt_id,
            state=state,
            terminal_payload=None,
            failure=failure,
        )

    def _complete(
        self,
        *,
        attempt_id: str,
        state: AttemptState,
        terminal_payload: dict[str, Any] | None,
        failure: FailureFact | None,
    ) -> AttemptRecord:
        """执行一次带当前状态条件的终态更新，防止迟到 Worker 覆盖首次事实。"""
        update_sql = """
            UPDATE skill_execution_attempts
            SET state = %(state)s,
                terminal_payload = %(terminal_payload)s,
                failure_payload = %(failure_payload)s,
                completed_at = now()
            WHERE attempt_id = %(attempt_id)s::uuid
              AND state = %(expected_state)s
            RETURNING operation_id::text AS operation_id;
        """
        try:
            with psycopg.connect(
                **self._settings.postgres_connection_kwargs,
                row_factory=dict_row,
            ) as connection:
                connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                with connection.cursor() as cursor:
                    cursor.execute(
                        update_sql,
                        {
                            "attempt_id": attempt_id,
                            "state": state.value,
                            "expected_state": AttemptState.INTENT_RECORDED.value,
                            "terminal_payload": (
                                psycopg.types.json.Jsonb(terminal_payload)
                                if terminal_payload is not None
                                else None
                            ),
                            "failure_payload": (
                                psycopg.types.json.Jsonb(failure.model_dump(mode="json"))
                                if failure is not None
                                else None
                            ),
                        },
                    )
                    updated = cursor.fetchone()
                    if updated is None:
                        raise AttemptInvariantError("attempt is not awaiting terminal result")
                    record = self._load_record(cursor, str(updated["operation_id"]))
                    if record is None:
                        raise AttemptInvariantError("completed attempt cannot be loaded")
                connection.commit()
                return record
        except AttemptInvariantError:
            raise
        except psycopg.Error as exc:
            raise AttemptInvariantError("failed to complete execution attempt") from exc

    @staticmethod
    def _load_record(cursor: Any, operation_id: str) -> AttemptRecord | None:
        """读取 Operation 与唯一 Attempt 的完整事实，供 claim/terminal 共享。"""
        cursor.execute(
            """
            SELECT
                o.operation_id::text AS operation_id,
                o.skill_id,
                o.skill_version,
                o.room_id,
                o.idempotency_key,
                o.deadline_at,
                o.intent_payload,
                a.attempt_id::text AS attempt_id,
                a.state,
                a.created_at,
                a.terminal_payload,
                a.failure_payload
            FROM skill_execution_operations AS o
            JOIN skill_execution_attempts AS a ON a.operation_id = o.operation_id
            WHERE o.operation_id = %(operation_id)s::uuid;
            """,
            {"operation_id": operation_id},
        )
        row = cursor.fetchone()
        if row is None:
            return None
        request = OperationRequest(
            skill_id=str(row["skill_id"]),
            skill_version=str(row["skill_version"]),
            room_id=str(row["room_id"]),
            idempotency_key=str(row["idempotency_key"]),
            deadline_at=row["deadline_at"],
            intent_payload=dict(row["intent_payload"]),
        )
        failure_payload = row["failure_payload"]
        return AttemptRecord(
            operation_id=str(row["operation_id"]),
            attempt_id=str(row["attempt_id"]),
            request=request,
            state=AttemptState(str(row["state"])),
            created_at=row["created_at"],
            terminal_payload=(
                None if row["terminal_payload"] is None else dict(row["terminal_payload"])
            ),
            failure=(
                None if failure_payload is None else FailureFact.model_validate(failure_payload)
            ),
        )


def initialize_skill_execution_attempt_schema(settings: Any) -> None:
    """初始化 Phase 11B Attempt 表，供迁移、集成测试和本地演示显式调用。"""
    project_root = Path(__file__).resolve().parents[2]
    sql_path = project_root / "docker" / "init_phase11b_skill_attempts.sql"
    sql = sql_path.read_text(encoding="utf-8")
    try:
        with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()
    except psycopg.Error as exc:
        raise AttemptInvariantError("failed to initialize execution attempt schema") from exc
