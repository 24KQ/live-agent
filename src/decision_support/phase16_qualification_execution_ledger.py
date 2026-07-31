"""Phase 16 qualification campaign 的逐 slot/stage append-only 执行账本。

该模块扩展 qualification 的 identity ledger，而非 V5/V8 历史账本。它只保存 digest、枚举、
usage、成本和 HMAC；不保存模型正文、Prompt、API key、holdout plaintext 或经营建议。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Any, Sequence
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg.rows import dict_row

from src.decision_support.phase16_qualification_ledger import (
    Phase16QualificationLedgerError,
    PostgresPhase16QualificationLedger,
)
from src.specialist_runtime.model_port import ModelSuccess
from src.specialist_runtime.models import canonical_json_sha256


class QualificationExecutionStage(StrEnum):
    ANALYST = "ANALYST"
    PLANNER = "PLANNER"


class QualificationExecutionValidationVerdict(StrEnum):
    PASS = "PASS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class QualificationExecutionCaseStatus(StrEnum):
    PASS = "PASS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class QualificationExecutionSlot:
    case_id: str
    case_digest: str
    expected_e2e: bool


@dataclass(frozen=True)
class QualificationExecutionClaim:
    claim_id: str
    run_id: str
    case_id: str
    expected_e2e: bool


@dataclass(frozen=True)
class QualificationExecutionAttempt:
    attempt_id: str
    run_id: str
    claim_id: str
    stage: QualificationExecutionStage
    internal_request_id: str
    reservation_cny: Decimal


@dataclass(frozen=True)
class QualificationExecutionCaseOutcome:
    case_id: str
    status: QualificationExecutionCaseStatus
    reason_code: str


@dataclass(frozen=True)
class QualificationExecutionSummary:
    run_id: str
    case_outcomes: tuple[QualificationExecutionCaseOutcome, ...]
    attempted_stage_count: int
    authenticated_receipt_count: int


class PostgresPhase16QualificationExecutionLedger(PostgresPhase16QualificationLedger):
    """用 SQL slot/stage triggers 加固新 qualification campaign 的单发和证据顺序。"""

    _INPUT_CNY_PER_MILLION = Decimal("3.000000")
    _OUTPUT_CNY_PER_MILLION = Decimal("6.000000")

    def begin_run_with_slots(
        self,
        *,
        run_id: str,
        campaign_id: str,
        slots: Sequence[QualificationExecutionSlot],
    ) -> None:
        """创建唯一 run 并冻结 slot 顺序；同一 run 不能通过重入替换公开 case identity。"""

        if not slots or len({slot.case_id for slot in slots}) != len(slots):
            raise ValueError("qualification execution slots must be non-empty and unique")
        if any(len(slot.case_digest) != 64 for slot in slots):
            raise ValueError("qualification execution case digest is invalid")
        self.begin_run(run_id=run_id, campaign_id=campaign_id)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    self._lock_live_run(cursor, run_id)
                    for position, slot in enumerate(slots, start=1):
                        cursor.execute(
                            """INSERT INTO phase16_qualification_case_slots
                               (run_id, slot_position, case_id, case_digest, expected_e2e)
                               VALUES (%s,%s,%s,%s,%s)
                               ON CONFLICT (run_id, case_id) DO NOTHING""",
                            (run_id, position, slot.case_id, slot.case_digest, slot.expected_e2e),
                        )
                    cursor.execute(
                        """SELECT slot_position, case_id, case_digest, expected_e2e
                             FROM phase16_qualification_case_slots
                            WHERE run_id=%s ORDER BY slot_position""",
                        (run_id,),
                    )
                    rows = cursor.fetchall()
                    actual = tuple(
                        (row["case_id"], row["case_digest"], bool(row["expected_e2e"])) for row in rows
                    )
                    expected = tuple(
                        (slot.case_id, slot.case_digest, slot.expected_e2e) for slot in slots
                    )
                    if actual != expected:
                        raise Phase16QualificationLedgerError("qualification run slot identity conflicts")
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification execution slot initialization failed") from error

    def claim_case(self, *, run_id: str, case_id: str, case_digest: str) -> QualificationExecutionClaim:
        """追加唯一 claim；已终态或已领取 slot 绝不允许再次发送。"""

        claim_id = str(uuid5(NAMESPACE_URL, f"phase16-qualification:{run_id}:{case_id}"))
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    self._lock_live_run(cursor, run_id)
                    cursor.execute(
                        """SELECT case_digest, expected_e2e
                             FROM phase16_qualification_case_slots
                            WHERE run_id=%s AND case_id=%s FOR UPDATE""",
                        (run_id, case_id),
                    )
                    slot = cursor.fetchone()
                    if slot is None or slot["case_digest"] != case_digest:
                        raise Phase16QualificationLedgerError("qualification case slot identity is invalid")
                    cursor.execute(
                        """SELECT 1 FROM phase16_qualification_case_outcomes
                             WHERE run_id=%s AND case_id=%s""",
                        (run_id, case_id),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase16QualificationLedgerError("qualification case is already terminal")
                    cursor.execute(
                        """INSERT INTO phase16_qualification_case_claims (claim_id, run_id, case_id)
                           VALUES (%s::uuid,%s,%s) ON CONFLICT (run_id, case_id) DO NOTHING""",
                        (claim_id, run_id, case_id),
                    )
                    cursor.execute(
                        """SELECT claim_id FROM phase16_qualification_case_claims
                             WHERE run_id=%s AND case_id=%s FOR UPDATE""",
                        (run_id, case_id),
                    )
                    row = cursor.fetchone()
                    if row is None or str(row["claim_id"]) != claim_id:
                        raise Phase16QualificationLedgerError("qualification case claim conflicts")
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification case claim failed") from error
        return QualificationExecutionClaim(
            claim_id=claim_id,
            run_id=run_id,
            case_id=case_id,
            expected_e2e=bool(slot["expected_e2e"]),
        )

    def begin_dispatch(
        self,
        *,
        run_id: str,
        claim_id: str,
        stage: QualificationExecutionStage,
        profile_digest: str,
        internal_request_id: str,
        reservation_cny: Decimal,
    ) -> QualificationExecutionAttempt:
        """HTTP 前追加唯一 intent；预算 reservation 和 Analyst→Planner 顺序由 DB 共同约束。"""

        if reservation_cny <= 0:
            raise ValueError("qualification stage reservation is invalid")
        if len(profile_digest) != 64:
            raise ValueError("qualification stage profile digest is invalid")
        attempt_id = str(uuid5(NAMESPACE_URL, f"phase16-qualification:{run_id}:{claim_id}:{stage.value}"))
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    campaign = self._lock_live_run(cursor, run_id)
                    cursor.execute(
                        """SELECT slot.expected_e2e
                             FROM phase16_qualification_case_claims claim
                             JOIN phase16_qualification_case_slots slot
                               ON slot.run_id=claim.run_id AND slot.case_id=claim.case_id
                            WHERE claim.claim_id=%s::uuid AND claim.run_id=%s FOR UPDATE""",
                        (claim_id, run_id),
                    )
                    claim = cursor.fetchone()
                    if claim is None:
                        raise Phase16QualificationLedgerError("qualification dispatch claim is invalid")
                    if not claim["expected_e2e"]:
                        raise Phase16QualificationLedgerError("qualification non-E2E case cannot dispatch model")
                    cursor.execute(
                        """SELECT campaign.reservation_cny, policy.stage_reservation_cny,
                                  COALESCE(SUM(
                                      CASE WHEN receipt.attempt_id IS NULL OR receipt.actual_cost_cny IS NULL
                                           THEN attempt.reservation_cny ELSE receipt.actual_cost_cny END
                                  ), 0) AS reserved
                             FROM phase16_qualification_campaigns campaign
                             JOIN phase16_qualification_policies policy
                               ON policy.policy_digest = campaign.policy_digest
                             LEFT JOIN phase16_qualification_runs run ON run.campaign_id=campaign.campaign_id
                             LEFT JOIN phase16_qualification_dispatch_attempts attempt ON attempt.run_id=run.run_id
                             LEFT JOIN phase16_qualification_provider_receipts receipt
                               ON receipt.attempt_id=attempt.attempt_id
                            WHERE campaign.campaign_id=%s
                         GROUP BY campaign.reservation_cny, policy.stage_reservation_cny""",
                        (campaign,),
                    )
                    budget = cursor.fetchone()
                    # 已 settle 的 attempt 按实际成本占帽（真实花费远小于 worst_case 预留，
                    # 累计历史预留会把 campaign 帽虚占卡死）；未决 attempt 仍按最坏预留
                    # 计，保证 fail-closed：未决预留 + 已决成本永远不超过 campaign 帽。
                    if budget is None or Decimal(budget["reserved"]) + reservation_cny > Decimal(budget["reservation_cny"]):
                        raise Phase16QualificationLedgerError("qualification campaign budget reservation exceeded")
                    if reservation_cny > Decimal(budget["stage_reservation_cny"]):
                        raise Phase16QualificationLedgerError("qualification stage reservation is invalid")
                    cursor.execute(
                        """INSERT INTO phase16_qualification_dispatch_attempts
                           (attempt_id, run_id, claim_id, stage, profile_digest, internal_request_id, reservation_cny)
                           VALUES (%s::uuid,%s,%s::uuid,%s,%s,%s::uuid,%s)
                           ON CONFLICT (claim_id, stage) DO NOTHING""",
                        (
                            attempt_id,
                            run_id,
                            claim_id,
                            stage.value,
                            profile_digest,
                            internal_request_id,
                            reservation_cny,
                        ),
                    )
                    cursor.execute(
                        """SELECT attempt_id, internal_request_id, reservation_cny
                             FROM phase16_qualification_dispatch_attempts
                            WHERE claim_id=%s::uuid AND stage=%s FOR UPDATE""",
                        (claim_id, stage.value),
                    )
                    attempt = cursor.fetchone()
                    if (
                        attempt is None
                        or str(attempt["attempt_id"]) != attempt_id
                        or str(attempt["internal_request_id"]) != internal_request_id
                        or Decimal(attempt["reservation_cny"]) != reservation_cny
                    ):
                        raise Phase16QualificationLedgerError("qualification dispatch attempt conflicts")
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification dispatch intent failed") from error
        return QualificationExecutionAttempt(
            attempt_id=attempt_id,
            run_id=run_id,
            claim_id=claim_id,
            stage=stage,
            internal_request_id=internal_request_id,
            reservation_cny=reservation_cny,
        )

    def append_receipt(
        self, *, attempt_id: str, success: ModelSuccess, reasoning_effort: str | None = None
    ) -> bool:
        """追加脱敏 Provider receipt 并以新 domain HMAC 认证；不完整 receipt 只能导致失败。

        reasoning_effort 来自 campaign 声明（白名单内），与 model_id / responded_endpoint_host
        一起钉死运行时矩阵组合；与字段一样受 HMAC payload 覆盖，无法脱离行数据伪造。
        """

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT attempt.internal_request_id, attempt.reservation_cny
                             FROM phase16_qualification_dispatch_attempts attempt
                            WHERE attempt.attempt_id=%s::uuid FOR UPDATE""",
                        (attempt_id,),
                    )
                    attempt = cursor.fetchone()
                    if attempt is None or str(attempt["internal_request_id"]) != success.request_id:
                        raise Phase16QualificationLedgerError("qualification receipt identity conflicts")
                    usage = success.usage
                    actual_cost = None
                    if usage is not None:
                        actual_cost = self._cost(usage.input_tokens, usage.output_tokens)
                    provider_digest = (
                        None
                        if not success.provider_response_id
                        else canonical_json_sha256({"provider_response_id": success.provider_response_id})
                    )
                    complete = (
                        provider_digest is not None
                        and success.finish_reason == "stop"
                        and bool(success.model_id)
                        and success.endpoint_host is not None
                        and usage is not None
                        and actual_cost is not None
                        and actual_cost <= Decimal(attempt["reservation_cny"])
                    )
                    payload = {
                        "attempt_id": attempt_id,
                        "provider_response_id_digest": provider_digest,
                        "finish_reason": success.finish_reason,
                        "model_id": success.model_id,
                        "reasoning_effort": reasoning_effort,
                        "response_digest": success.response_digest,
                        "attempt_count": success.attempts,
                        "responded_endpoint_host": success.endpoint_host,
                        "input_tokens": None if usage is None else usage.input_tokens,
                        "output_tokens": None if usage is None else usage.output_tokens,
                        "total_tokens": None if usage is None else usage.total_tokens,
                        "latency_ms": str(Decimal(success.latency_ms).quantize(Decimal("0.001"))),
                        "actual_cost_cny": None if actual_cost is None else str(actual_cost),
                        "output_digest": canonical_json_sha256(success.output),
                        "receipt_complete": complete,
                    }
                    cursor.execute(
                        """INSERT INTO phase16_qualification_provider_receipts
                           (attempt_id, provider_response_id_digest, finish_reason, model_id, response_digest,
                            reasoning_effort,
                            attempt_count, responded_endpoint_host,
                            input_tokens, output_tokens, total_tokens, latency_ms, actual_cost_cny, output_digest,
                            receipt_complete, receipt_auth_tag)
                           VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            attempt_id,
                            provider_digest,
                            success.finish_reason,
                            success.model_id,
                            success.response_digest,
                            reasoning_effort,
                            success.attempts,
                            success.endpoint_host,
                            None if usage is None else usage.input_tokens,
                            None if usage is None else usage.output_tokens,
                            None if usage is None else usage.total_tokens,
                            Decimal(success.latency_ms).quantize(Decimal("0.001")),
                            actual_cost,
                            canonical_json_sha256(success.output),
                            complete,
                            self._tag(domain="execution-receipt", payload=payload),
                        ),
                    )
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification receipt append failed") from error
        return complete

    def append_validation(
        self,
        *,
        attempt_id: str,
        verdict: QualificationExecutionValidationVerdict,
        reason_code: str,
        validation_digest: str,
    ) -> None:
        """追加 stage validation；调用方只能提供闭合 reason 和摘要，HMAC 由账本自行生成。"""

        self._require_reason(reason_code)
        if len(validation_digest) != 64:
            raise ValueError("qualification validation digest is invalid")
        payload = {
            "attempt_id": attempt_id,
            "verdict": verdict.value,
            "reason_code": reason_code,
            "validation_digest": validation_digest,
        }
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_qualification_validation_facts
                           (attempt_id, verdict, reason_code, validation_digest, validation_auth_tag)
                           VALUES (%s::uuid,%s,%s,%s,%s)""",
                        (
                            attempt_id,
                            verdict.value,
                            reason_code,
                            validation_digest,
                            self._tag(domain="execution-validation", payload=payload),
                        ),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification validation append failed") from error

    def record_pre_dispatch_block(self, *, claim_id: str, reason_code: str) -> None:
        """封闭未写 attempt 前的本地阻断，避免把未知发送状态误标为可重试。"""

        self._require_reason(reason_code)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT run_id FROM phase16_qualification_case_claims
                             WHERE claim_id=%s::uuid FOR UPDATE""",
                        (claim_id,),
                    )
                    claim = cursor.fetchone()
                    if claim is None:
                        raise Phase16QualificationLedgerError("qualification pre-dispatch claim is unknown")
                    payload = {"claim_id": claim_id, "reason_code": reason_code}
                    cursor.execute(
                        """INSERT INTO phase16_qualification_pre_dispatch_blocks
                           (claim_id, run_id, reason_code, block_digest, block_auth_tag)
                           VALUES (%s::uuid,%s,%s,%s,%s)""",
                        (
                            claim_id,
                            claim["run_id"],
                            reason_code,
                            canonical_json_sha256(payload),
                            self._tag(domain="pre-dispatch-block", payload=payload),
                        ),
                    )
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification pre-dispatch block append failed") from error

    def close_case(
        self,
        *,
        claim_id: str,
        status: QualificationExecutionCaseStatus,
        reason_code: str,
    ) -> None:
        """追加 case terminal fact；SQL 独立核验 E2E/non-E2E 所需 stage 事实。"""

        self._require_reason(reason_code)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT run_id, case_id FROM phase16_qualification_case_claims
                             WHERE claim_id=%s::uuid FOR UPDATE""",
                        (claim_id,),
                    )
                    claim = cursor.fetchone()
                    if claim is None:
                        raise Phase16QualificationLedgerError("qualification case claim is unknown")
                    payload = {
                        "claim_id": claim_id,
                        "status": status.value,
                        "reason_code": reason_code,
                    }
                    cursor.execute(
                        """INSERT INTO phase16_qualification_case_outcomes
                           (run_id, case_id, claim_id, status, reason_code, outcome_digest)
                           VALUES (%s,%s,%s::uuid,%s,%s,%s)""",
                        (
                            claim["run_id"],
                            claim["case_id"],
                            claim_id,
                            status.value,
                            reason_code,
                            canonical_json_sha256(payload),
                        ),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification case close failed") from error

    def execution_summary(self, *, run_id: str) -> QualificationExecutionSummary:
        """只读返回 case outcomes 与 HMAC 完整 receipt 数，不泄漏任何 Provider/模型正文。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT case_id, status, reason_code
                             FROM phase16_qualification_case_outcomes
                            WHERE run_id=%s ORDER BY case_id""",
                        (run_id,),
                    )
                    cases = tuple(
                        QualificationExecutionCaseOutcome(
                            case_id=row["case_id"],
                            status=QualificationExecutionCaseStatus(row["status"]),
                            reason_code=row["reason_code"],
                        )
                        for row in cursor.fetchall()
                    )
                    cursor.execute(
                        """SELECT count(*) AS attempt_count,
                                  count(*) FILTER (WHERE receipt.receipt_complete=true) AS complete_receipts
                             FROM phase16_qualification_dispatch_attempts attempt
                             LEFT JOIN phase16_qualification_provider_receipts receipt ON receipt.attempt_id=attempt.attempt_id
                            WHERE attempt.run_id=%s""",
                        (run_id,),
                    )
                    counts = cursor.fetchone()
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification execution summary unavailable") from error
        return QualificationExecutionSummary(
            run_id=run_id,
            case_outcomes=cases,
            attempted_stage_count=int(counts["attempt_count"]),
            authenticated_receipt_count=int(counts["complete_receipts"]),
        )

    def _lock_live_run(self, cursor: Any, run_id: str) -> str:
        cursor.execute(
            """SELECT run.campaign_id,
                       EXISTS(SELECT 1 FROM phase16_qualification_results result WHERE result.run_id=run.run_id)
                           AS terminal
                 FROM phase16_qualification_runs run
                 JOIN phase16_qualification_campaigns campaign ON campaign.campaign_id=run.campaign_id
                WHERE run.run_id=%s FOR UPDATE OF run, campaign""",
            (run_id,),
        )
        row = cursor.fetchone()
        if row is None or row["terminal"]:
            raise Phase16QualificationLedgerError("qualification run is terminal or unavailable")
        return row["campaign_id"]

    @classmethod
    def _cost(cls, input_tokens: int, output_tokens: int) -> Decimal:
        raw = (
            Decimal(input_tokens) * cls._INPUT_CNY_PER_MILLION
            + Decimal(output_tokens) * cls._OUTPUT_CNY_PER_MILLION
        ) / Decimal("1000000")
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _require_reason(value: str) -> None:
        if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in value):
            raise ValueError("qualification reason code is invalid")
