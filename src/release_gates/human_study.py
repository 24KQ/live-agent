"""Phase 15 真人交叉对照采集器。

该模块只保存真实参与者的加盐摘要、封闭动作、服务端耗时和 Promotion artifact
绑定。它不接受姓名、自由文本或客户端计时，也不提供 ScriptedModel 伪造路径；
缺少真实参与者时，证据状态只能是 ``BLOCKED``。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
import re
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping

import psycopg
from psycopg.rows import dict_row
from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from src.specialist_runtime.models import StrictFrozenModel, _freeze_json, _plain_json, canonical_json_sha256


HASH_PATTERN = r"^[0-9a-f]{64}$"
STUDY_GROUPS = (
    "SOLD_OUT_BACKUP_CONFLICT",
    "DANMAKU_NOISE",
    "PACE_SHIFT",
    "EVIDENCE_CONFLICT",
)


class StudyCondition(StrEnum):
    """每个场景的两种封闭实验条件。"""

    BASELINE = "BASELINE"
    DECISION_SUPPORT = "DECISION_SUPPORT"


class StudyDecisionAction(StrEnum):
    """真人只能选择的结构化动作，不允许提交自由执行命令。"""

    WAIT_OPERATOR = "WAIT_OPERATOR"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"
    IGNORE_NOISE = "IGNORE_NOISE"
    WAIT_TIMING = "WAIT_TIMING"


class StudySessionStatus(StrEnum):
    """交叉对照 session 的生命周期。"""

    OPEN = "OPEN"
    COMPLETED = "COMPLETED"


class StudyEvidenceStatus(StrEnum):
    """真人证据是否达到 Promotion 输入要求。"""

    READY = "READY"
    BLOCKED = "BLOCKED"


class HumanStudyConfig(StrictFrozenModel):
    """绑定 Golden Manifest、四组 case 和真实参与者上限的冻结配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    study_id: str = Field(..., min_length=1)
    seed: int = Field(..., ge=0, strict=True)
    dataset_manifest_digest: str = Field(..., pattern=HASH_PATTERN)
    promotion_artifact_digest: str | None = Field(default=None, pattern=HASH_PATTERN)
    group_case_ids: Mapping[str, tuple[str, ...]]
    participant_limit: int = Field(default=5, ge=3, le=5, strict=True)

    @field_validator("group_case_ids", mode="after")
    @classmethod
    def _freeze_groups(cls, value: Mapping[str, tuple[str, ...]]) -> Mapping[str, tuple[str, ...]]:
        """固定四组等价场景和每组候选 case，禁止运行时重排数据集。"""

        plain = _plain_json(value)
        if set(plain) != set(STUDY_GROUPS) or any(
            not isinstance(items, list) or len(items) < 2 or any(not item for item in items)
            for items in plain.values()
        ):
            raise ValueError("group_case_ids must contain four non-empty scenario groups")
        return _freeze_json({key: items for key, items in plain.items()})

    @field_serializer("group_case_ids", when_used="json")
    def _serialize_groups(self, value: Any) -> Any:
        return _plain_json(value)


class StudyAssignment(StrictFrozenModel):
    """服务端生成的一个实验 assignment。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assignment_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    participant_digest: str = Field(..., pattern=HASH_PATTERN)
    scenario_group: str = Field(..., pattern=r"^[A-Z_]+$")
    case_id: str = Field(..., min_length=1)
    condition: StudyCondition
    sequence: int = Field(..., ge=1, le=8, strict=True)
    started_at: datetime | None = None

    @field_validator("started_at")
    @classmethod
    def _timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("assignment started_at requires timezone")
        return value


class HumanStudySession(StrictFrozenModel):
    """不含原始参与者身份的 Study session。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(..., min_length=1)
    study_id: str = Field(..., min_length=1)
    participant_digest: str = Field(..., pattern=HASH_PATTERN)
    assignment_ids: tuple[str, ...] = Field(..., min_length=8, max_length=8)
    status: StudySessionStatus = StudySessionStatus.OPEN


class StudyResponse(StrictFrozenModel):
    """客户端可提交的唯一响应协议；没有 latency/PII/free-text 字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: StudyDecisionAction
    conflict_detected: bool
    workload_score: int = Field(..., ge=1, le=7, strict=True)


class StudyResponseRecord(StrictFrozenModel):
    """服务端补充时间和 artifact 绑定后的不可变响应事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(..., min_length=1)
    assignment_id: str = Field(..., min_length=1)
    participant_digest: str = Field(..., pattern=HASH_PATTERN)
    action: StudyDecisionAction
    conflict_detected: bool
    workload_score: int = Field(..., ge=1, le=7, strict=True)
    server_latency_ms: Decimal = Field(..., ge=0)
    promotion_artifact_digest: str | None = Field(default=None, pattern=HASH_PATTERN)

    @field_validator("server_latency_ms", mode="after")
    @classmethod
    def _latency_precision(cls, value: Decimal) -> Decimal:
        """服务端耗时只保存数据库支持的毫秒精度。"""

        if value != value.quantize(Decimal("0.001")):
            raise ValueError("server_latency_ms exceeds millisecond precision")
        return value


class StudyEvidence(StrictFrozenModel):
    """供 Promotion Gate 消费的真人证据摘要，不包含参与者原始身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: StudyEvidenceStatus
    reason_codes: tuple[str, ...] = ()
    study_id: str = Field(..., min_length=1)
    dataset_manifest_digest: str = Field(..., pattern=HASH_PATTERN)
    promotion_artifact_digest: str | None = Field(default=None, pattern=HASH_PATTERN)
    participant_count: int = Field(..., ge=0, le=5, strict=True)
    response_count: int = Field(..., ge=0, strict=True)
    evidence_digest: str = Field(..., pattern=HASH_PATTERN)


def _build_session_facts(
    config: HumanStudyConfig,
    participant_digest: str,
    participant_index: int,
) -> tuple[HumanStudySession, tuple[StudyAssignment, ...]]:
    """按固定 seed/参与者序号生成同一组可在内存和 PostgreSQL 重放的 assignment。"""

    session_id = f"{config.study_id}-{participant_digest[:16]}"
    assignments: list[StudyAssignment] = []
    sequence = 1
    for group_index, group in enumerate(STUDY_GROUPS):
        cases = config.group_case_ids[group]
        case_id = cases[(participant_index + group_index) % len(cases)]
        for condition in StudyCondition:
            assignments.append(
                StudyAssignment(
                    assignment_id=f"{session_id}-{sequence:02d}",
                    session_id=session_id,
                    participant_digest=participant_digest,
                    scenario_group=group,
                    case_id=case_id,
                    condition=condition,
                    sequence=sequence,
                )
            )
            sequence += 1
    return (
        HumanStudySession(
            session_id=session_id,
            study_id=config.study_id,
            participant_digest=participant_digest,
            assignment_ids=tuple(item.assignment_id for item in assignments),
        ),
        tuple(assignments),
    )


class HumanStudyStore:
    """内存 Study Store；生产 PostgreSQL 适配器复用同一模型和状态机。"""

    def __init__(self, config: HumanStudyConfig, *, participant_salt: str) -> None:
        if not participant_salt or not participant_salt.strip():
            raise ValueError("participant_salt is required")
        self._config = config
        self._salt = participant_salt
        self._lock = RLock()
        self._sessions: dict[str, HumanStudySession] = {}
        self._assignments: dict[str, StudyAssignment] = {}
        self._responses: dict[str, StudyResponseRecord] = {}
        self._session_by_participant: dict[str, str] = {}

    def create_session(self, participant_code: str) -> HumanStudySession:
        """创建或幂等重放真实参与者 session；不保存原始 code。"""

        if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", participant_code or "") is None:
            raise ValueError("participant_code must be an opaque non-PII token")
        digest = sha256(f"{self._salt}:{participant_code}".encode("utf-8")).hexdigest()
        with self._lock:
            existing_id = self._session_by_participant.get(digest)
            if existing_id is not None:
                return self._sessions[existing_id]
            if len(self._sessions) >= self._config.participant_limit:
                raise ValueError("participant limit reached")
            participant_index = len(self._sessions)
            session, assignments = _build_session_facts(self._config, digest, participant_index)
            # 只能使用由冻结事实构造器生成的 session_id；这里不能重新拼接或接受
            # 客户端提供的身份，确保内存实现与 PostgreSQL 实现共享同一主键事实。
            self._sessions[session.session_id] = session
            self._session_by_participant[digest] = session.session_id
            self._assignments.update({item.assignment_id: item for item in assignments})
            return session

    def list_assignments(self, session_id: str) -> tuple[StudyAssignment, ...]:
        """读取一个 session 的固定 assignment 顺序。"""

        with self._lock:
            session = self._required_session(session_id)
            return tuple(self._assignments[item] for item in session.assignment_ids)

    def next_trial(self, session_id: str) -> StudyAssignment | None:
        """返回下一个未响应试验，并由服务端记录开始时间。"""

        with self._lock:
            session = self._required_session(session_id)
            for assignment_id in session.assignment_ids:
                assignment = self._assignments[assignment_id]
                if assignment_id in self._responses:
                    continue
                if assignment.started_at is None:
                    assignment = assignment.model_validate(
                        {**assignment.model_dump(mode="json"), "started_at": datetime.now(timezone.utc)}
                    )
                    self._assignments[assignment_id] = assignment
                return assignment
            self._sessions[session_id] = HumanStudySession.model_validate(
                {**session.model_dump(mode="json"), "status": StudySessionStatus.COMPLETED.value}
            )
            return None

    def record_response(self, session_id: str, assignment_id: str, response: StudyResponse) -> StudyResponseRecord:
        """校验 assignment 作用域并用服务端时钟追加响应，重复事实幂等。"""

        with self._lock:
            session = self._required_session(session_id)
            assignment = self._assignments.get(assignment_id)
            if assignment is None or assignment.session_id != session_id:
                raise ValueError("assignment does not belong to session")
            if assignment.started_at is None:
                raise ValueError("trial must be started by next_trial")
            existing = self._responses.get(assignment_id)
            if existing is not None:
                if (
                    existing.action is not response.action
                    or existing.conflict_detected != response.conflict_detected
                    or existing.workload_score != response.workload_score
                ):
                    raise ValueError("conflicting response replay")
                return existing
            now = datetime.now(timezone.utc)
            latency = max((now - assignment.started_at).total_seconds() * 1000, 0)
            record = StudyResponseRecord(
                session_id=session_id,
                assignment_id=assignment_id,
                participant_digest=session.participant_digest,
                action=response.action,
                conflict_detected=response.conflict_detected,
                workload_score=response.workload_score,
                server_latency_ms=Decimal(str(latency)).quantize(Decimal("0.001")),
                promotion_artifact_digest=self._config.promotion_artifact_digest,
            )
            self._responses[assignment_id] = record
            return record

    def promotion_evidence(self) -> StudyEvidence:
        """只有完整 3-5 人真人数据且绑定 smoke artifact 才解锁 Promotion 输入。"""

        with self._lock:
            participant_count = len(self._sessions)
            response_count = len(self._responses)
            reasons: list[str] = []
            if participant_count < 3:
                reasons.append("REAL_PARTICIPANTS_INSUFFICIENT")
            if any(session.status is not StudySessionStatus.COMPLETED for session in self._sessions.values()):
                reasons.append("MISSING_RESPONSES")
            if self._config.promotion_artifact_digest is None:
                reasons.append("PROMOTION_ARTIFACT_MISSING")
            status = StudyEvidenceStatus.READY if not reasons else StudyEvidenceStatus.BLOCKED
            digest = canonical_json_sha256(
                {
                    "study_id": self._config.study_id,
                    "dataset_manifest_digest": self._config.dataset_manifest_digest,
                    "promotion_artifact_digest": self._config.promotion_artifact_digest,
                    "participant_digests": sorted(self._sessions),
                    "responses": [item.model_dump(mode="json") for item in sorted(self._responses.values(), key=lambda item: item.assignment_id)],
                    "status": status.value,
                }
            )
            return StudyEvidence(
                status=status,
                reason_codes=tuple(reasons),
                study_id=self._config.study_id,
                dataset_manifest_digest=self._config.dataset_manifest_digest,
                promotion_artifact_digest=self._config.promotion_artifact_digest,
                participant_count=participant_count,
                response_count=response_count,
                evidence_digest=digest,
            )

    def snapshot(self) -> Mapping[str, Any]:
        """返回只读快照供 Task 7/报告层消费。"""

        with self._lock:
            return MappingProxyType(
                {
                    "sessions": tuple(self._sessions.values()),
                    "assignments": tuple(self._assignments.values()),
                    "responses": tuple(self._responses.values()),
                }
            )

    def _required_session(self, session_id: str) -> HumanStudySession:
        try:
            return self._sessions[session_id]
        except KeyError as error:
            raise ValueError("study session does not exist") from error


class PostgresHumanStudyStore:
    """使用 Phase 15 study 表保存可重启真人采集事实。"""

    def __init__(self, settings: Any, config: HumanStudyConfig, *, participant_salt: str) -> None:
        if not participant_salt or not participant_salt.strip():
            raise ValueError("participant_salt is required")
        self._settings = settings
        self._config = config
        self._salt = participant_salt
        initialize_phase15_human_study_schema(settings)

    def create_session(self, participant_code: str) -> HumanStudySession:
        """创建或重放 PostgreSQL 中的匿名 session。"""

        digest = self._participant_digest(participant_code)
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                # 按 study 维度串行化 participant limit 检查与创建，避免两个连接
                # 同时看到相同 count 后突破 3-5 人边界；事务提交即释放该锁。
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0));",
                    (self._config.study_id,),
                )
                cursor.execute(
                    "SELECT session_id FROM phase15_human_study_sessions WHERE study_id=%s AND participant_digest=%s;",
                    (self._config.study_id, digest),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    session = self._load_session(cursor, existing["session_id"])
                    conn.commit()
                    return session
                cursor.execute(
                    "SELECT count(*) AS count FROM phase15_human_study_sessions WHERE study_id=%s;",
                    (self._config.study_id,),
                )
                participant_count = int(cursor.fetchone()["count"])
                if participant_count >= self._config.participant_limit:
                    raise ValueError("participant limit reached")
                participant_index = participant_count
                session, assignments = _build_session_facts(self._config, digest, participant_index)
                cursor.execute(
                    "INSERT INTO phase15_human_study_sessions (session_id,study_id,participant_digest,dataset_manifest_digest,promotion_artifact_digest,status) VALUES (%s,%s,%s,%s,%s,%s);",
                    (session.session_id, session.study_id, session.participant_digest, self._config.dataset_manifest_digest, self._config.promotion_artifact_digest, session.status.value),
                )
                cursor.executemany(
                    "INSERT INTO phase15_human_study_assignments (assignment_id,session_id,scenario_group,case_id,condition,sequence) VALUES (%s,%s,%s,%s,%s,%s);",
                    [(item.assignment_id, item.session_id, item.scenario_group, item.case_id, item.condition.value, item.sequence) for item in assignments],
                )
            conn.commit()
        return session

    def list_assignments(self, session_id: str) -> tuple[StudyAssignment, ...]:
        """按服务端 sequence 重载一个 session 的 assignment。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                self._load_session(cursor, session_id)
                cursor.execute(
                    """
                    SELECT a.*, s.participant_digest
                    FROM phase15_human_study_assignments a
                    JOIN phase15_human_study_sessions s ON s.session_id=a.session_id
                    WHERE a.session_id=%s AND s.study_id=%s
                    ORDER BY a.sequence;
                    """,
                    (session_id, self._config.study_id),
                )
                rows = cursor.fetchall()
        return tuple(self._assignment_from_row(row) for row in rows)

    def next_trial(self, session_id: str) -> StudyAssignment | None:
        """在行锁内领取下一个 trial 并持久化服务端开始时间。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                self._load_session(cursor, session_id)
                cursor.execute(
                    """
                    SELECT a.*, s.participant_digest
                    FROM phase15_human_study_assignments a
                    JOIN phase15_human_study_sessions s ON s.session_id=a.session_id
                    WHERE a.session_id=%s AND s.study_id=%s AND NOT EXISTS (
                        SELECT 1 FROM phase15_human_study_responses r WHERE r.assignment_id=a.assignment_id
                    ) ORDER BY a.sequence LIMIT 1 FOR UPDATE;
                    """,
                    (session_id, self._config.study_id),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        "UPDATE phase15_human_study_sessions SET status='COMPLETED' WHERE session_id=%s AND study_id=%s AND status='OPEN';",
                        (session_id, self._config.study_id),
                    )
                    conn.commit()
                    return None
                participant_digest = row["participant_digest"]
                cursor.execute(
                    "UPDATE phase15_human_study_assignments SET started_at=COALESCE(started_at, now()) WHERE assignment_id=%s RETURNING *;",
                    (row["assignment_id"],),
                )
                row = cursor.fetchone()
                # UPDATE RETURNING 只返回 assignment 表列，补回同一事务中已校验的
                # session digest，避免恢复后的公开模型退化为占位身份。
                row["participant_digest"] = participant_digest
            conn.commit()
        return self._assignment_from_row(row)

    def record_response(self, session_id: str, assignment_id: str, response: StudyResponse) -> StudyResponseRecord:
        """校验作用域、锁定 assignment 并追加幂等服务端响应。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT a.*, s.participant_digest FROM phase15_human_study_assignments a JOIN phase15_human_study_sessions s ON s.session_id=a.session_id WHERE a.assignment_id=%s AND a.session_id=%s AND s.study_id=%s FOR UPDATE;",
                    (assignment_id, session_id, self._config.study_id),
                )
                assignment = cursor.fetchone()
                if assignment is None:
                    raise ValueError("assignment does not belong to session")
                cursor.execute(
                    """
                    SELECT r.*, s.participant_digest
                    FROM phase15_human_study_responses r
                    JOIN phase15_human_study_sessions s ON s.session_id=r.session_id
                    WHERE r.assignment_id=%s AND r.session_id=%s AND s.study_id=%s;
                    """,
                    (assignment_id, session_id, self._config.study_id),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if existing["action"] != response.action.value or bool(existing["conflict_detected"]) != response.conflict_detected or int(existing["workload_score"]) != response.workload_score:
                        raise ValueError("conflicting response replay")
                    record = self._response_from_row(existing)
                    conn.commit()
                    return record
                if assignment["started_at"] is None:
                    raise ValueError("trial must be started by next_trial")
                latency = max((datetime.now(timezone.utc) - assignment["started_at"]).total_seconds() * 1000, 0)
                latency_decimal = Decimal(str(latency)).quantize(Decimal("0.001"))
                cursor.execute(
                    "INSERT INTO phase15_human_study_responses (assignment_id,session_id,action,conflict_detected,workload_score,server_latency_ms,promotion_artifact_digest) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *;",
                    (assignment_id, session_id, response.action.value, response.conflict_detected, response.workload_score, latency_decimal, self._config.promotion_artifact_digest),
                )
                row = cursor.fetchone()
                # INSERT RETURNING 只返回响应事实表列；participant_digest 必须沿用
                # 已锁定 assignment 的 session 身份，不能从客户端响应重新计算。
                row["participant_digest"] = assignment["participant_digest"]
                cursor.execute(
                    "SELECT count(*) AS count FROM phase15_human_study_responses WHERE session_id=%s;",
                    (session_id,),
                )
                if int(cursor.fetchone()["count"]) == 8:
                    cursor.execute(
                        "UPDATE phase15_human_study_sessions SET status='COMPLETED' WHERE session_id=%s AND study_id=%s;",
                        (session_id, self._config.study_id),
                    )
            conn.commit()
        return self._response_from_row(row)

    def promotion_evidence(self) -> StudyEvidence:
        """从持久化 session/response 事实重算真人 Promotion 输入状态。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM phase15_human_study_sessions WHERE study_id=%s ORDER BY session_id;", (self._config.study_id,))
                sessions = tuple(cursor.fetchall())
                for row in sessions:
                    self._validate_session_config(row)
                cursor.execute(
                    """
                    SELECT r.*, s.participant_digest
                    FROM phase15_human_study_responses r
                    JOIN phase15_human_study_sessions s ON s.session_id=r.session_id
                    WHERE s.study_id=%s
                    ORDER BY r.assignment_id;
                    """,
                    (self._config.study_id,),
                )
                responses = tuple(cursor.fetchall())
        reasons: list[str] = []
        if len(sessions) < 3:
            reasons.append("REAL_PARTICIPANTS_INSUFFICIENT")
        if any(row["status"] != StudySessionStatus.COMPLETED.value for row in sessions):
            reasons.append("MISSING_RESPONSES")
        if self._config.promotion_artifact_digest is None:
            reasons.append("PROMOTION_ARTIFACT_MISSING")
        status = StudyEvidenceStatus.READY if not reasons else StudyEvidenceStatus.BLOCKED
        digest = canonical_json_sha256(
            {
                "study_id": self._config.study_id,
                "dataset_manifest_digest": self._config.dataset_manifest_digest,
                "promotion_artifact_digest": self._config.promotion_artifact_digest,
                "participant_digests": sorted(row["participant_digest"] for row in sessions),
                "responses": [self._response_from_row(row).model_dump(mode="json") for row in responses],
                "status": status.value,
            }
        )
        return StudyEvidence(
            status=status,
            reason_codes=tuple(reasons),
            study_id=self._config.study_id,
            dataset_manifest_digest=self._config.dataset_manifest_digest,
            promotion_artifact_digest=self._config.promotion_artifact_digest,
            participant_count=len(sessions),
            response_count=len(responses),
            evidence_digest=digest,
        )

    def _participant_digest(self, participant_code: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", participant_code or "") is None:
            raise ValueError("participant_code must be an opaque non-PII token")
        return sha256(f"{self._salt}:{participant_code}".encode("utf-8")).hexdigest()

    def _load_session(self, cursor: Any, session_id: str) -> HumanStudySession:
        """按当前 study 和冻结 digest 重载 session，跨 study 一律不可见。"""

        cursor.execute(
            "SELECT * FROM phase15_human_study_sessions WHERE session_id=%s AND study_id=%s;",
            (session_id, self._config.study_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("study session does not exist")
        self._validate_session_config(row)
        cursor.execute(
            "SELECT assignment_id FROM phase15_human_study_assignments WHERE session_id=%s ORDER BY sequence;",
            (session_id,),
        )
        assignment_ids = tuple(item["assignment_id"] for item in cursor.fetchall())
        return HumanStudySession(
            session_id=row["session_id"], study_id=row["study_id"], participant_digest=row["participant_digest"], assignment_ids=assignment_ids, status=row["status"]
        )

    def _validate_session_config(self, row: Mapping[str, Any]) -> None:
        """拒绝同一 study_id 下混入其他 Manifest 或 Promotion artifact 的事实。"""

        if row["dataset_manifest_digest"] != self._config.dataset_manifest_digest:
            raise ValueError("study session manifest digest does not match frozen config")
        if row["promotion_artifact_digest"] != self._config.promotion_artifact_digest:
            raise ValueError("study session promotion artifact digest does not match frozen config")

    @staticmethod
    def _assignment_from_row(row: Mapping[str, Any]) -> StudyAssignment:
        participant_digest = row.get("participant_digest")
        if not isinstance(participant_digest, str) or re.fullmatch(HASH_PATTERN, participant_digest) is None:
            raise ValueError("assignment row is missing authoritative participant digest")
        return StudyAssignment(
            assignment_id=row["assignment_id"], session_id=row["session_id"], participant_digest=participant_digest, scenario_group=row["scenario_group"], case_id=row["case_id"], condition=row["condition"], sequence=int(row["sequence"]), started_at=row["started_at"]
        )

    @staticmethod
    def _response_from_row(row: Mapping[str, Any]) -> StudyResponseRecord:
        participant_digest = row.get("participant_digest")
        if not isinstance(participant_digest, str) or re.fullmatch(HASH_PATTERN, participant_digest) is None:
            raise ValueError("response row is missing authoritative participant digest")
        return StudyResponseRecord(
            session_id=row["session_id"], assignment_id=row["assignment_id"], participant_digest=participant_digest, action=row["action"], conflict_detected=bool(row["conflict_detected"]), workload_score=int(row["workload_score"]), server_latency_ms=Decimal(row["server_latency_ms"]), promotion_artifact_digest=row["promotion_artifact_digest"]
        )


def initialize_phase15_human_study_schema(settings: Any) -> None:
    """执行包含 study 表的 Phase 15 幂等 DDL。"""

    sql_path = Path(__file__).resolve().parents[2] / "docker" / "init_phase15_release_gates.sql"
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql_path.read_text(encoding="utf-8"))
        conn.commit()
