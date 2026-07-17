"""Phase 13 播后记忆候选的受控暂存模型与内存事实仓储。"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import Settings
from src.memory.memory_store import MemoryStore


class MemoryCandidateStatus(StrEnum):
    """候选只能按受控命令从暂存进入终态，Agent 不能直接写 active memory。"""

    STAGED = "STAGED"
    ELIGIBLE_AWAITING_OPERATOR = "ELIGIBLE_AWAITING_OPERATOR"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"


class MemoryCandidate(BaseModel):
    """只保存 PromotionPolicy 可验证的结构化偏好和证据身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    anchor_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    evidence_ids: tuple[str, ...] = Field(..., min_length=1)
    preferred_category: str = Field(..., min_length=1)
    preferred_tags: tuple[str, ...] = ()
    preferred_product_ids: tuple[str, ...] = Field(..., min_length=1)
    confidence: Decimal = Field(..., ge=Decimal("0"), le=Decimal("1"))
    status: MemoryCandidateStatus = MemoryCandidateStatus.STAGED
    version: int = Field(default=1, ge=1)


class MemoryPromotionCommand(BaseModel):
    """晋升命令必须携带候选的乐观版本和预期状态，重放使用稳定 command_id。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str = Field(..., min_length=1)
    candidate_id: str = Field(..., min_length=1)
    expected_version: int = Field(..., ge=1)
    expected_status: MemoryCandidateStatus


class PromotionResult(BaseModel):
    """Policy 的可审计决定，不把内部异常或自由文本传给后续调用方。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    status: MemoryCandidateStatus
    reason_code: str
    version: int


class InMemoryMemoryCandidateStore:
    """测试与基线使用的独立候选事实仓储，显式表达幂等和命令重放语义。"""

    def __init__(self) -> None:
        self._by_id: dict[str, MemoryCandidate] = {}
        self._by_idempotency: dict[str, str] = {}
        self._commands: dict[str, PromotionResult] = {}

    def stage(self, candidate: MemoryCandidate) -> MemoryCandidate:
        """写入首个候选；同幂等键只能重放同一份不可变业务内容。"""

        # Pydantic 的 model_copy(update=...) 不会重新执行 extra 校验。Store 是 Agent
        # staging 的最终边界，因此必须拒绝该路径夹带的 free_text 或其他未声明字段。
        unexpected = set(candidate.__dict__) - set(type(candidate).model_fields)
        if candidate.__pydantic_extra__ or unexpected:
            fields = ",".join(sorted(set(candidate.__pydantic_extra__ or {}) | unexpected))
            raise ValueError(f"memory candidate contains forbidden fields: {fields}")
        validated = MemoryCandidate.model_validate(candidate.model_dump(mode="python"))
        existing_id = self._by_idempotency.get(validated.idempotency_key)
        if existing_id is not None:
            existing = self._by_id[existing_id]
            if existing != validated:
                raise ValueError("idempotency_key conflicts with existing memory candidate")
            return existing
        if validated.candidate_id in self._by_id:
            raise ValueError("candidate_id already exists")
        self._by_id[validated.candidate_id] = validated
        self._by_idempotency[validated.idempotency_key] = validated.candidate_id
        return validated

    def get(self, candidate_id: str) -> MemoryCandidate:
        """候选不存在即 fail-closed，避免 PromotionPolicy 伪造默认候选。"""

        try:
            return self._by_id[candidate_id]
        except KeyError as exc:
            raise ValueError("memory candidate not found") from exc

    def get_command_result(self, command_id: str) -> PromotionResult | None:
        """读取已处理命令，供调用方无副作用重放。"""

        return self._commands.get(command_id)

    def transition(self, candidate_id: str, *, status: MemoryCandidateStatus) -> MemoryCandidate:
        """Policy/人工确认门面是唯一可变更状态的调用方；每次转换递增版本。"""

        candidate = self.get(candidate_id)
        allowed = {
            MemoryCandidateStatus.STAGED: {
                MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR,
                MemoryCandidateStatus.APPROVED,
                MemoryCandidateStatus.REJECTED,
                MemoryCandidateStatus.APPLIED,
            },
            MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR: {
                MemoryCandidateStatus.APPLIED,
                MemoryCandidateStatus.REJECTED,
            },
        }
        if status not in allowed.get(candidate.status, set()):
            raise ValueError("memory candidate has no legal status transition")
        updated = candidate.model_copy(update={"status": status, "version": candidate.version + 1})
        self._by_id[candidate_id] = updated
        return updated

    def record_command_result(self, command_id: str, result: PromotionResult) -> PromotionResult:
        """首个 command_id 决定结果；同 ID 不能悄悄覆盖审计事实。"""

        existing = self._commands.setdefault(command_id, result)
        if existing != result:
            raise ValueError("command_id conflicts with existing promotion result")
        return existing


class PostgresMemoryCandidateStore:
    """生产闭环使用的候选/命令事实仓储，所有状态变更均通过 SQL 乐观版本保护。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def initialize_schema(self) -> None:
        """独立执行 Task 9 DDL，便于测试和启动入口重复调用。"""
        from pathlib import Path
        sql = (Path(__file__).parents[2] / "docker" / "init_phase13_memory_candidates.sql").read_text(encoding="utf-8")
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()

    def memory_port(self) -> MemoryStore:
        """返回受既有 MemoryStore 校验保护的 active-memory 写入口。"""
        return MemoryStore(self._settings)

    def stage(self, candidate: MemoryCandidate) -> MemoryCandidate:
        """同幂等键只重放完全相同的结构化候选，拒绝覆盖既有事实。"""
        validated = MemoryCandidate.model_validate(candidate.model_dump(mode="python"))
        sql = """INSERT INTO phase13_memory_candidates(candidate_id,idempotency_key,anchor_id,room_id,evidence_ids,preferred_category,preferred_tags,preferred_product_ids,confidence,status,version) VALUES (%(candidate_id)s,%(idempotency_key)s,%(anchor_id)s,%(room_id)s,%(evidence_ids)s,%(preferred_category)s,%(preferred_tags)s,%(preferred_product_ids)s,%(confidence)s,%(status)s,1) ON CONFLICT (idempotency_key) DO NOTHING RETURNING *;"""
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {**validated.model_dump(mode="python"), "evidence_ids": Jsonb(list(validated.evidence_ids)), "preferred_tags": Jsonb(list(validated.preferred_tags)), "preferred_product_ids": Jsonb(list(validated.preferred_product_ids)), "status": validated.status.value})
                row = cur.fetchone()
                if row is None:
                    cur.execute("SELECT * FROM phase13_memory_candidates WHERE idempotency_key=%s", (validated.idempotency_key,))
                    row = cur.fetchone()
            conn.commit()
        result = self._candidate_from_row(row)
        if result != validated:
            raise ValueError("idempotency_key conflicts with existing memory candidate")
        return result

    def get(self, candidate_id: str) -> MemoryCandidate:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM phase13_memory_candidates WHERE candidate_id=%s", (candidate_id,))
                row = cur.fetchone()
        if row is None:
            raise ValueError("memory candidate not found")
        return self._candidate_from_row(row)

    def transition(self, candidate_id: str, *, status: MemoryCandidateStatus) -> MemoryCandidate:
        candidate = self.get(candidate_id)
        allowed = {
            MemoryCandidateStatus.STAGED: {
                MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR,
                MemoryCandidateStatus.APPROVED,
                MemoryCandidateStatus.REJECTED,
                MemoryCandidateStatus.APPLIED,
            },
            MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR: {
                MemoryCandidateStatus.APPLIED,
                MemoryCandidateStatus.REJECTED,
            },
        }
        if status not in allowed.get(candidate.status, set()):
            raise ValueError("memory candidate has no legal status transition")
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE phase13_memory_candidates SET status=%s,version=version+1,updated_at=NOW() WHERE candidate_id=%s AND version=%s AND status IN ('STAGED','ELIGIBLE_AWAITING_OPERATOR') RETURNING *", (status.value, candidate_id, candidate.version))
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise ValueError("memory candidate transition conflict")
        return self._candidate_from_row(row)

    def get_command_result(self, command_id: str) -> PromotionResult | None:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM phase13_memory_promotion_commands WHERE command_id=%s", (command_id,))
                row = cur.fetchone()
        return None if row is None else PromotionResult(candidate_id=row["candidate_id"], status=MemoryCandidateStatus(row["result_status"]), reason_code=row["reason_code"], version=int(row["result_version"]))

    def record_command_result(self, command_id: str, result: PromotionResult) -> PromotionResult:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO phase13_memory_promotion_commands(command_id,candidate_id,result_status,reason_code,result_version) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (command_id) DO NOTHING RETURNING *", (command_id,result.candidate_id,result.status.value,result.reason_code,result.version))
                row = cur.fetchone()
                if row is None:
                    cur.execute("SELECT * FROM phase13_memory_promotion_commands WHERE command_id=%s", (command_id,))
                    row = cur.fetchone()
            conn.commit()
        stored = PromotionResult(candidate_id=row["candidate_id"], status=MemoryCandidateStatus(row["result_status"]), reason_code=row["reason_code"], version=int(row["result_version"]))
        if stored != result:
            raise ValueError("command_id conflicts with existing promotion result")
        return stored

    @staticmethod
    def _candidate_from_row(row: dict) -> MemoryCandidate:
        return MemoryCandidate(candidate_id=row["candidate_id"], idempotency_key=row["idempotency_key"], anchor_id=row["anchor_id"], room_id=row["room_id"], evidence_ids=tuple(row["evidence_ids"]), preferred_category=row["preferred_category"], preferred_tags=tuple(row["preferred_tags"]), preferred_product_ids=tuple(row["preferred_product_ids"]), confidence=row["confidence"], status=MemoryCandidateStatus(row["status"]), version=int(row["version"]))
