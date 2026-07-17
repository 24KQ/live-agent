"""Phase 14 Task 9 播后记忆资格与人工确认的受控闭环。

本模块把“规则判定合格”和“运营确认晋升”拆成两条不可互相覆盖的事实链：
规则只负责把候选推进到 ``ELIGIBLE_AWAITING_OPERATOR``，PromotionPolicy
仍是唯一 active memory 写入口；确认命令必须绑定操作员、候选版本和资格事实。
Agent、HTTP 页面和自由文本都不能绕过这里直接调用 active-memory Port。
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.config.settings import Settings
from src.memory.candidate_store import (
    MemoryCandidateStatus,
    MemoryPromotionCommand,
    PromotionResult,
)


class MemoryEligibilityFact(BaseModel):
    """规则资格的不可变摘要；不保存模型自由正文，只保存可回放身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(..., min_length=1)
    command_id: str = Field(..., min_length=1)
    candidate_version: int = Field(..., ge=1)
    status: MemoryCandidateStatus
    reason_code: str = Field(..., min_length=1)
    evidence_ids: tuple[str, ...] = Field(..., min_length=1)
    product_whitelist: tuple[str, ...] = Field(..., min_length=1)
    anchor_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    whitelist_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @field_validator("status")
    @classmethod
    def _require_eligibility_status(cls, value: MemoryCandidateStatus) -> MemoryCandidateStatus:
        """资格事实不能伪装成已批准或已应用的终态。"""

        if value not in {
            MemoryCandidateStatus.STAGED,
            MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR,
        }:
            raise ValueError("eligibility fact has an invalid status")
        return value

    @property
    def version(self) -> int:
        """提供与候选/确认结果一致的只读版本别名，避免调用方混淆事实版本。"""

        return self.candidate_version


class MemoryConfirmationResult(BaseModel):
    """人工确认命令的可重放结果，明确保留 operator_id 审计身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str = Field(..., min_length=1)
    candidate_id: str = Field(..., min_length=1)
    operator_id: str = Field(..., min_length=1)
    status: MemoryCandidateStatus
    reason_code: str = Field(..., min_length=1)
    version: int = Field(..., ge=1)


class MemoryConfirmationIntent(BaseModel):
    """写 active memory 前持久化的人工确认意图，作为唯一授权事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str = Field(..., min_length=1)
    candidate_id: str = Field(..., min_length=1)
    expected_version: int = Field(..., ge=1)
    operator_id: str = Field(..., min_length=1)


class InMemoryDecisionTraceResolver:
    """测试用可信 Trace Port；调用方只能按已登记的 trace_id 读取事实。"""

    def __init__(self, records: tuple[dict[str, Any], ...] = ()) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        for record in records:
            self.add(record)

    def add(self, record: dict[str, Any]) -> None:
        """登记一条不可覆盖的测试事实，模拟真实 Trace Store 的 append-only 语义。"""

        trace_id = str(record.get("trace_id") or record.get("decision_trace_id") or "").strip()
        if not trace_id:
            raise ValueError("trusted decision trace requires trace_id")
        stored = dict(record)
        stored["trace_id"] = trace_id
        self._records.setdefault(trace_id, stored)
        if self._records[trace_id] != stored:
            raise ValueError("trusted decision trace conflicts with existing fact")

    def resolve(self, trace_id: str) -> dict[str, Any] | None:
        """缺失 Trace 返回 None，禁止服务层把调用方摘要当作事实。"""

        record = self._records.get(trace_id)
        return None if record is None else dict(record)


class PostgresDecisionTraceResolver:
    """把既有 DecisionTraceStore 适配为只读可信 Trace Port。"""

    def __init__(self, settings: Settings) -> None:
        from src.memory.decision_trace_store import DecisionTraceStore

        self._store = DecisionTraceStore(settings)

    def resolve(self, trace_id: str) -> dict[str, Any] | None:
        """按业务 trace_id 从 PostgreSQL 重载完整记录，不接受 HTTP/模型摘要。"""

        records = self._store.list_traces(trace_id)
        if not records:
            return None
        record = records[0]
        return {
            "trace_id": record.trace_id,
            "decision_trace_id": record.trace_id,
            "anchor_id": record.anchor_id,
            "room_id": record.room_id,
            "recommendation": dict(record.recommendation),
            "anchor_action": record.anchor_action.value,
            "business_result": record.business_result.value,
        }


class InMemoryReviewFeedbackStore:
    """资格和人工确认的内存事实仓储，语义与 PostgreSQL 表一一对应。"""

    def __init__(self) -> None:
        self._eligibility_by_candidate: dict[str, MemoryEligibilityFact] = {}
        self._eligibility_by_command: dict[str, MemoryEligibilityFact] = {}
        self._intents: dict[str, MemoryConfirmationIntent] = {}
        self._confirmations: dict[str, MemoryConfirmationResult] = {}

    def record_eligibility(self, fact: MemoryEligibilityFact) -> MemoryEligibilityFact:
        """候选只允许形成一条资格事实；相同事实可以幂等重放。"""

        fact = MemoryEligibilityFact.model_validate(fact.model_dump(mode="python"))
        existing = self._eligibility_by_candidate.get(fact.candidate_id)
        if existing is not None:
            if existing != fact:
                raise ValueError("candidate already has a different eligibility fact")
            return existing
        command_existing = self._eligibility_by_command.get(fact.command_id)
        if command_existing is not None and command_existing != fact:
            raise ValueError("eligibility command conflicts with existing fact")
        self._eligibility_by_candidate[fact.candidate_id] = fact
        self._eligibility_by_command[fact.command_id] = fact
        return fact

    def get_eligibility(self, candidate_id: str) -> MemoryEligibilityFact | None:
        """读取资格事实；不存在时返回 None，由服务层 fail-closed。"""

        return self._eligibility_by_candidate.get(candidate_id)

    def get_confirmation_result(self, command_id: str) -> MemoryConfirmationResult | None:
        """读取确认命令结果，支持重启后的无副作用重放。"""

        return self._confirmations.get(command_id)

    def get_confirmation_intent(self, command_id: str) -> MemoryConfirmationIntent | None:
        """读取已持久化的人工授权意图。"""

        return self._intents.get(command_id)

    def record_confirmation_intent(self, intent: MemoryConfirmationIntent) -> MemoryConfirmationIntent:
        """确认意图先于副作用写入；同命令只能绑定同一操作员和版本。"""

        intent = MemoryConfirmationIntent.model_validate(intent.model_dump(mode="python"))
        existing = self._intents.setdefault(intent.command_id, intent)
        if existing != intent:
            raise ValueError("confirmation intent conflicts with existing command")
        return existing

    def record_confirmation(self, result: MemoryConfirmationResult) -> MemoryConfirmationResult:
        """同一 command_id 不能绑定不同操作员或不同结果。"""

        result = MemoryConfirmationResult.model_validate(result.model_dump(mode="python"))
        existing = self._confirmations.setdefault(result.command_id, result)
        if existing != result:
            raise ValueError("confirmation command conflicts with existing result")
        return existing


class PostgresReviewFeedbackStore:
    """Phase 14 资格/确认事实的 PostgreSQL Store，依靠唯一键实现幂等。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def initialize_schema(self) -> None:
        """重复执行专用 DDL，确保旧测试库也安装新状态约束。"""

        from pathlib import Path

        sql = (Path(__file__).parents[2] / "docker" / "init_phase14_memory_feedback.sql").read_text(encoding="utf-8")
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()

    def record_eligibility(self, fact: MemoryEligibilityFact) -> MemoryEligibilityFact:
        """候选主键保证单一资格事实；冲突时只返回首次事实。"""

        fact = MemoryEligibilityFact.model_validate(fact.model_dump(mode="python"))
        sql = """
            INSERT INTO phase14_memory_eligibility(
                candidate_id, command_id, candidate_version, result_status,
                reason_code, evidence_ids, product_whitelist, anchor_id, room_id,
                whitelist_digest
            ) VALUES (%(candidate_id)s, %(command_id)s, %(candidate_version)s,
                %(status)s, %(reason_code)s, %(evidence_ids)s, %(product_whitelist)s,
                %(anchor_id)s, %(room_id)s, %(whitelist_digest)s)
            ON CONFLICT (candidate_id) DO NOTHING
            RETURNING *;
        """
        params = {
            **fact.model_dump(mode="python"),
            "status": fact.status.value,
            "evidence_ids": Jsonb(list(fact.evidence_ids)),
            "product_whitelist": Jsonb(list(fact.product_whitelist)),
        }
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                if row is None:
                    cur.execute("SELECT * FROM phase14_memory_eligibility WHERE candidate_id=%s", (fact.candidate_id,))
                    row = cur.fetchone()
            conn.commit()
        if row is None:
            raise ValueError("eligibility fact was not persisted")
        stored = self._eligibility_from_row(row)
        if stored != fact:
            raise ValueError("candidate already has a different eligibility fact")
        return stored

    def get_eligibility(self, candidate_id: str) -> MemoryEligibilityFact | None:
        """读取候选资格事实，不为缺失事实推导默认合格。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM phase14_memory_eligibility WHERE candidate_id=%s", (candidate_id,))
                row = cur.fetchone()
        return None if row is None else self._eligibility_from_row(row)

    def get_confirmation_result(self, command_id: str) -> MemoryConfirmationResult | None:
        """读取确认结果，确保重启后重放不会再次调用 active-memory Port。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM phase14_memory_confirmations WHERE command_id=%s", (command_id,))
                row = cur.fetchone()
        return None if row is None else self._confirmation_from_row(row)

    def get_confirmation_intent(self, command_id: str) -> MemoryConfirmationIntent | None:
        """从数据库读取人工授权意图，供 PromotionPolicy 做最终门禁。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM phase14_memory_confirmation_intents WHERE command_id=%s", (command_id,))
                row = cur.fetchone()
        return None if row is None else MemoryConfirmationIntent(
            command_id=row["command_id"], candidate_id=row["candidate_id"],
            expected_version=int(row["expected_version"]), operator_id=row["operator_id"],
        )

    def record_confirmation_intent(self, intent: MemoryConfirmationIntent) -> MemoryConfirmationIntent:
        """使用 command_id 唯一约束持久化人工授权意图。"""

        intent = MemoryConfirmationIntent.model_validate(intent.model_dump(mode="python"))
        sql = """
            INSERT INTO phase14_memory_confirmation_intents(command_id,candidate_id,expected_version,operator_id)
            VALUES (%(command_id)s,%(candidate_id)s,%(expected_version)s,%(operator_id)s)
            ON CONFLICT (command_id) DO NOTHING RETURNING *;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, intent.model_dump(mode="python"))
                row = cur.fetchone()
                if row is None:
                    cur.execute("SELECT * FROM phase14_memory_confirmation_intents WHERE command_id=%s", (intent.command_id,))
                    row = cur.fetchone()
            conn.commit()
        if row is None:
            raise ValueError("confirmation intent was not persisted")
        stored = MemoryConfirmationIntent(
            command_id=row["command_id"], candidate_id=row["candidate_id"],
            expected_version=int(row["expected_version"]), operator_id=row["operator_id"],
        )
        if stored != intent:
            raise ValueError("confirmation intent conflicts with existing command")
        return stored

    def record_confirmation(self, result: MemoryConfirmationResult) -> MemoryConfirmationResult:
        """确认命令主键和操作员身份共同形成 append-only 审计事实。"""

        result = MemoryConfirmationResult.model_validate(result.model_dump(mode="python"))
        sql = """
            INSERT INTO phase14_memory_confirmations(
                command_id, candidate_id, operator_id, result_status,
                reason_code, result_version
            ) VALUES (%(command_id)s, %(candidate_id)s, %(operator_id)s,
                %(status)s, %(reason_code)s, %(version)s)
            ON CONFLICT (command_id) DO NOTHING
            RETURNING *;
        """
        params = {**result.model_dump(mode="python"), "status": result.status.value}
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                if row is None:
                    cur.execute("SELECT * FROM phase14_memory_confirmations WHERE command_id=%s", (result.command_id,))
                    row = cur.fetchone()
            conn.commit()
        if row is None:
            raise ValueError("confirmation result was not persisted")
        stored = self._confirmation_from_row(row)
        if stored != result:
            raise ValueError("confirmation command conflicts with existing result")
        return stored

    @staticmethod
    def _eligibility_from_row(row: dict[str, Any]) -> MemoryEligibilityFact:
        """严格从数据库行重建资格事实，拒绝隐式状态转换。"""

        return MemoryEligibilityFact(
            candidate_id=row["candidate_id"], command_id=row["command_id"], candidate_version=int(row["candidate_version"]),
            status=MemoryCandidateStatus(row["result_status"]), reason_code=row["reason_code"],
            evidence_ids=tuple(row["evidence_ids"]), product_whitelist=tuple(row["product_whitelist"]),
            anchor_id=row["anchor_id"], room_id=row["room_id"], whitelist_digest=row["whitelist_digest"],
        )

    @staticmethod
    def _confirmation_from_row(row: dict[str, Any]) -> MemoryConfirmationResult:
        """严格从数据库行重建确认结果。"""

        return MemoryConfirmationResult(
            command_id=row["command_id"], candidate_id=row["candidate_id"], operator_id=row["operator_id"],
            status=MemoryCandidateStatus(row["result_status"]), reason_code=row["reason_code"], version=int(row["result_version"]),
        )


class ReviewFeedbackService:
    """把资格计算、人工确认和唯一 PromotionPolicy 写入口串成两阶段服务。"""

    _SENSITIVE_KEYS = frozenset({
        "free_text", "raw_text", "chain_of_thought", "prompt", "secret", "token", "embedding",
    })

    def __init__(self, *, candidate_store: Any, feedback_store: Any, promotion_policy: Any, decision_trace_resolver: Any) -> None:
        self._candidate_store = candidate_store
        self._feedback_store = feedback_store
        self._promotion_policy = promotion_policy
        self._decision_trace_resolver = decision_trace_resolver

    def evaluate_eligibility(
        self,
        *,
        command_id: str,
        candidate_id: str,
        expected_version: int,
        trace_ids: tuple[str, ...],
        product_whitelist: set[str],
    ) -> MemoryEligibilityFact:
        """只计算资格并持久化事实，绝不调用 active-memory Port。"""

        candidate = self._candidate_store.get(candidate_id)
        replay = self._feedback_store.get_eligibility(candidate_id)
        if replay is not None:
            if candidate.version != expected_version:
                raise ValueError("expected_version does not match memory candidate")
            if replay.command_id != command_id:
                raise ValueError("candidate already has an eligibility fact")
            if replay.status is MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR:
                if candidate.status is MemoryCandidateStatus.STAGED and candidate.version + 1 == replay.version:
                    # 资格事实已持久化但状态转换中断时，重试只补齐确定性的 CAS 转换。
                    self._candidate_store.transition(
                        candidate_id,
                        status=MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR,
                    )
                elif candidate.status is not MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR or candidate.version != replay.version:
                    raise ValueError("eligibility fact version does not match memory candidate")
            elif candidate.version != replay.version:
                raise ValueError("eligibility fact version does not match memory candidate")
            return replay
        if candidate.version != expected_version:
            raise ValueError("expected_version does not match memory candidate")
        if candidate.status is not MemoryCandidateStatus.STAGED:
            raise ValueError("candidate must be STAGED before eligibility evaluation")
        decision_traces = tuple(
            self._decision_trace_resolver.resolve(trace_id)
            for trace_id in trace_ids
        )
        if any(item is None for item in decision_traces):
            reason = "TRACE_NOT_FOUND"
            resolved_traces: tuple[dict[str, Any], ...] = tuple(
                item for item in decision_traces if item is not None
            )
        else:
            resolved_traces = tuple(item for item in decision_traces if item is not None)
            reason = self._eligibility_reason(candidate, resolved_traces, product_whitelist)
        status = MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR if reason is None else MemoryCandidateStatus.STAGED
        version = candidate.version + 1 if reason is None else candidate.version
        ordered_whitelist = tuple(sorted(set(product_whitelist)))
        fact = MemoryEligibilityFact(
            candidate_id=candidate.candidate_id,
            command_id=command_id,
            candidate_version=version,
            status=status,
            reason_code=reason or "ELIGIBLE_AWAITING_OPERATOR",
            evidence_ids=tuple(str(item.get("trace_id") or "") for item in resolved_traces if item.get("trace_id")),
            product_whitelist=ordered_whitelist or ("__EMPTY__",),
            anchor_id=candidate.anchor_id,
            room_id=candidate.room_id,
            whitelist_digest=_whitelist_digest(ordered_whitelist),
        )
        persisted = self._feedback_store.record_eligibility(fact)
        if reason is None:
            # 先有资格事实、后做 CAS 状态转换；若进程在这里退出，下一次调用可按上面的
            # replay 分支补齐转换，而不会留下“状态已变但资格证据不存在”的半链路。
            self._candidate_store.transition(
                candidate_id,
                status=MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR,
            )
        return persisted

    def confirm_promotion(
        self,
        *,
        command_id: str,
        candidate_id: str,
        expected_version: int,
        operator_id: str,
    ) -> MemoryConfirmationResult:
        """仅允许人工确认合格候选，并把实际晋升交给 PromotionPolicy。"""

        if not operator_id or not operator_id.strip():
            raise ValueError("operator_id is required")
        replay = self._feedback_store.get_confirmation_result(command_id)
        if replay is not None:
            intent = self._feedback_store.get_confirmation_intent(command_id)
            if intent is None or intent.candidate_id != candidate_id or intent.expected_version != expected_version or intent.operator_id != operator_id:
                raise ValueError("confirmation command conflicts with replay")
            return replay
        candidate = self._candidate_store.get(candidate_id)
        recovering_after_cas = candidate.status is MemoryCandidateStatus.APPLIED and candidate.version == expected_version + 1
        if candidate.version != expected_version and not recovering_after_cas:
            raise ValueError("expected_version does not match memory candidate")
        fact = self._feedback_store.get_eligibility(candidate_id)
        if fact is None or fact.status is not MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR:
            raise ValueError("candidate must be ELIGIBLE_AWAITING_OPERATOR before confirmation")
        if not recovering_after_cas and candidate.status is not MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR:
            raise ValueError("candidate status is not ELIGIBLE_AWAITING_OPERATOR")
        self._feedback_store.record_confirmation_intent(
            MemoryConfirmationIntent(
                command_id=command_id,
                candidate_id=candidate_id,
                expected_version=expected_version,
                operator_id=operator_id.strip(),
            )
        )
        policy_result: PromotionResult = self._promotion_policy.promote(
            MemoryPromotionCommand(
                command_id=command_id,
                candidate_id=candidate_id,
                expected_version=expected_version,
                expected_status=MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR,
            ),
            operator_id=operator_id.strip(),
        )
        result = MemoryConfirmationResult(
            command_id=command_id,
            candidate_id=candidate_id,
            operator_id=operator_id.strip(),
            status=policy_result.status,
            reason_code=policy_result.reason_code,
            version=policy_result.version,
        )
        return self._feedback_store.record_confirmation(result)

    @classmethod
    def _eligibility_reason(
        cls,
        candidate: Any,
        decision_traces: tuple[dict[str, Any], ...],
        product_whitelist: set[str],
    ) -> str | None:
        """按固定优先级检查证据、作用域、冲突、敏感字段和货盘白名单。"""

        if any(cls._contains_sensitive(item) for item in decision_traces):
            return "SENSITIVE_FIELD_PRESENT"
        if any(bool(item.get("conflict") or item.get("conflict_flag") or item.get("evidence_conflict")) for item in decision_traces):
            return "EVIDENCE_CONFLICT"
        linked = [
            item for item in decision_traces
            if cls._trace_identity(item) in candidate.evidence_ids
        ]
        if any(item.get("anchor_id") != candidate.anchor_id or item.get("room_id") != candidate.room_id for item in linked):
            return "TRACE_SCOPE_CONFLICT"
        if len({cls._trace_identity(item) for item in linked}) < 2:
            return "INSUFFICIENT_INDEPENDENT_EVIDENCE"
        if not set(candidate.preferred_product_ids).issubset(product_whitelist):
            return "PRODUCT_WHITELIST_MISMATCH"
        return None

    @staticmethod
    def _trace_identity(trace: dict[str, Any]) -> str:
        """统一业务 trace_id 与数据库生成 ID 的受控读取命名。"""

        return str(trace.get("trace_id") or trace.get("decision_trace_id") or "")

    @classmethod
    def _contains_sensitive(cls, value: Any) -> bool:
        """递归拒绝自由正文和秘密字段，避免其进入资格/记忆证据链。"""

        if isinstance(value, dict):
            return any(
                str(key).lower() in cls._SENSITIVE_KEYS or cls._contains_sensitive(item)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(cls._contains_sensitive(item) for item in value)
        return False


def _whitelist_digest(product_whitelist: tuple[str, ...]) -> str:
    """对规范化货盘集合做稳定摘要，便于资格事实重放和审计。"""

    payload = json.dumps(list(product_whitelist), ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()
