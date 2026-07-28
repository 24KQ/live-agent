"""Phase 16 V3 Planner 单次诊断的独立 PostgreSQL append-only 账本。

V3 不是对 V1/V2 的重试，也不是新的生产决策路径。它只在一份新的固定 case 上发起一次
Planner 调用，并把成功回执或脱敏 ModelFailure 事实持久化，供后续判断应修 Prompt、超时、
适配器还是外部网络。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from hashlib import sha256
import hmac
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from src.decision_support.planner_diagnostic_v3 import (
    Phase16V3DiagnosticFailureCategory,
    Phase16V3ModelFailureFact,
)
from src.specialist_runtime.model_port import ModelFailureCategory, ModelSuccess


PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID = "phase16-v3-planner-diagnostic-001"
PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID = "phase16-high-conflict-paired-development-001"
PHASE16_V3_TOTAL_BUDGET_CNY = Decimal("1.000000")
PHASE16_V3_PLANNER_RESERVATION_CNY = Decimal("0.052000")
# 既有事实与 V2 未回执 Planner 的最大责任必须先占用预算，不能把未知调用当作零成本。
PHASE16_V3_PRIOR_EXPOSURES = {
    "PHASE16_V1_AND_HISTORICAL": Decimal("0.079526"),
    "PHASE16_V2_ANALYST_ACTUAL": Decimal("0.018639"),
    "PHASE16_V2_PLANNER_UNKNOWN_MAX": Decimal("0.052000"),
}


class Phase16V3DiagnosticLedgerError(RuntimeError):
    """V3 账本的身份、预算、顺序或 append-only 约束不满足时抛出。"""


class Phase16V3DiagnosticValidationVerdict(StrEnum):
    """单 Planner 尝试的终态验证结论。"""

    PASS = "PASS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class Phase16V3DiagnosticOutcomeStatus(StrEnum):
    """唯一固定 case 的最终状态；没有可重试的中间成功。"""

    PASS = "PASS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Phase16V3DiagnosticDispatchAttempt:
    """发送意图事实；它不等同于 Provider 已接收请求。"""

    attempt_id: str
    run_id: str
    case_id: str
    planner_profile_digest: str
    internal_request_id: str


@dataclass(frozen=True)
class Phase16V3PersistedModelFailureFact:
    """附带账本外 HMAC 的失败事实读取投影，不携带异常消息或模型文本。"""

    attempt_id: str
    category: ModelFailureCategory | Phase16V3DiagnosticFailureCategory
    request_sent: bool | None
    response_digest: str | None
    http_status: int | None
    retry_after_seconds: int | None
    latency_ms: Decimal
    fact_digest: str
    failure_auth_tag: str


@dataclass(frozen=True)
class Phase16V3ProviderReceipt:
    """V3 Planner 成功回执的脱敏读取投影，带有进程外 HMAC 完整性标签。"""

    attempt_id: str
    planner_profile_digest: str
    provider_response_id_digest: str
    finish_reason: str
    model_id: str
    response_digest: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: Decimal
    receipt_auth_tag: str


@dataclass(frozen=True)
class Phase16V3DiagnosticOutcome:
    """固定 case 的 append-only 终态投影。"""

    case_id: str
    status: Phase16V3DiagnosticOutcomeStatus
    reason_code: str
    outcome_digest: str


class Phase16V3FailureAuthenticator:
    """为失败事实生成独立 HMAC，防止数据库直写伪造外部故障根因。"""

    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("V3 failure signing key must contain at least 256 bits")
        self._signing_key = signing_key

    def sign(self, fact: Phase16V3ModelFailureFact) -> str:
        """只签名白名单结构化字段；不让 Prompt 或正文进入 HMAC 输入。"""

        # 虽然本机复用受保护的 HMAC 根密钥，域分隔仍让 V3 failure tag 不能充当 V2
        # provider receipt 的标签，避免不同账本之间的认证材料发生语义替换。
        payload = {
            "domain": "phase16-v3-planner-diagnostic-failure-v1",
            "fact": fact.model_dump(mode="json"),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._signing_key, encoded, sha256).hexdigest()

    def verify(self, fact: Phase16V3PersistedModelFailureFact) -> bool:
        """用常量时间比较认证标签，报告器可据此拒绝未认证的失败结论。"""

        try:
            unsigned = Phase16V3ModelFailureFact(
                attempt_id=fact.attempt_id,
                category=fact.category,
                request_sent=fact.request_sent,
                response_digest=fact.response_digest,
                http_status=fact.http_status,
                retry_after_seconds=fact.retry_after_seconds,
                latency_ms=fact.latency_ms,
                fact_digest=fact.fact_digest,
            )
        except ValueError:
            # 历史行与当前规范无法重建摘要时，报告必须得到明确的不可验证结论，不能因
            # 读取异常而跳过认证，也不能回填、更新或替换 append-only 原始失败事实。
            return False
        return hmac.compare_digest(self.sign(unsigned), fact.failure_auth_tag)


class Phase16V3ReceiptAuthenticator:
    """为 V3 成功回执签名，防止仅满足 SQL 形状的数据库直写伪造成模型成功。"""

    def __init__(self, signing_key: bytes) -> None:
        """只接受至少 256 位的进程外密钥，诊断路径不得内置可猜测默认值。"""

        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("V3 receipt signing key must contain at least 256 bits")
        self._signing_key = signing_key

    def sign(
        self,
        *,
        attempt_id: str,
        planner_profile_digest: str,
        provider_response_id_digest: str,
        finish_reason: str,
        model_id: str,
        response_digest: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        latency_ms: Decimal,
    ) -> str:
        """只签名可审计白名单字段，不把 Prompt、模型正文或思维链带入认证输入。"""

        payload = {
            # 与 failure HMAC 使用独立域，避免一个类别的标签被替换成另一个类别的证据。
            "domain": "phase16-v3-planner-diagnostic-receipt-v1",
            "attempt_id": str(UUID(attempt_id)),
            "planner_profile_digest": planner_profile_digest,
            "provider_response_id_digest": provider_response_id_digest,
            "finish_reason": finish_reason,
            "model_id": model_id,
            "response_digest": response_digest,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            # Receipt 与 failure 共享 PostgreSQL NUMERIC(16,3) 的 half-up 语义。默认
            # Decimal half-even 会在精确 x.xxx5 时与数据库保存值不同，导致未来成功
            # 回执虽已落库却无法复验 HMAC。
            "latency_ms": str(
                Decimal(latency_ms).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            ),
        }
        return hmac.new(
            self._signing_key,
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            sha256,
        ).hexdigest()

    def verify(self, receipt: Phase16V3ProviderReceipt) -> bool:
        """使用常量时间比较回执标签；报告器可据此拒绝未经受控进程写入的成功事实。"""

        if not self._is_auth_tag(receipt.receipt_auth_tag):
            return False
        expected = self.sign(
            attempt_id=receipt.attempt_id,
            planner_profile_digest=receipt.planner_profile_digest,
            provider_response_id_digest=receipt.provider_response_id_digest,
            finish_reason=receipt.finish_reason,
            model_id=receipt.model_id,
            response_digest=receipt.response_digest,
            input_tokens=receipt.input_tokens,
            output_tokens=receipt.output_tokens,
            total_tokens=receipt.total_tokens,
            latency_ms=receipt.latency_ms,
        )
        return hmac.compare_digest(expected, receipt.receipt_auth_tag)

    @staticmethod
    def _is_auth_tag(value: str) -> bool:
        """认证字段只能是固定长度的小写十六进制，不能接收自由文本。"""

        return isinstance(value, str) and len(value) == 64 and all(
            character in "0123456789abcdef" for character in value
        )


class PostgresPhase16V3PlannerDiagnosticLedger:
    """V3 账本唯一写接口；每次写入都依赖数据库事务和触发器双重校验。"""

    def __init__(
        self,
        settings: Any,
        *,
        failure_authenticator: Phase16V3FailureAuthenticator,
        receipt_authenticator: Phase16V3ReceiptAuthenticator,
    ) -> None:
        self._settings = settings
        # 测试与正式入口都必须显式提供签名器。禁止内置默认 key，避免诊断失败事实在
        # 某个错误装配路径下悄悄降级成可伪造的未认证记录。
        self._failure_authenticator = failure_authenticator
        # 成功与失败分别域分隔签名。即使暂时复用同一个受保护根密钥，标签也不能跨类别替换。
        self._receipt_authenticator = receipt_authenticator

    def _connection(self):
        return psycopg.connect(
            **self._settings.postgres_connection_kwargs,
            row_factory=dict_row,
        )

    def ensure_run(
        self,
        *,
        manifest_digest: str,
        planner_profile_digest: str,
        case_digest: str,
    ) -> None:
        """创建或精确复验唯一 run，同时导入保守的历史/未知风险事实。"""

        self._require_digest(manifest_digest, "manifest_digest")
        self._require_digest(planner_profile_digest, "planner_profile_digest")
        self._require_digest(case_digest, "case_digest")
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT manifest_digest, planner_profile_digest, case_digest
                           FROM phase16_v3_planner_diagnostic_runs WHERE run_id=%s FOR UPDATE""",
                        (PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID,),
                    )
                    existing = cursor.fetchone()
                    if existing is None:
                        cursor.execute(
                            """INSERT INTO phase16_v3_planner_diagnostic_runs
                               (run_id, manifest_digest, planner_profile_digest, case_digest,
                                total_budget_cny, planner_reservation_cny)
                               VALUES (%s,%s,%s,%s,%s,%s)""",
                            (
                                PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID,
                                manifest_digest,
                                planner_profile_digest,
                                case_digest,
                                PHASE16_V3_TOTAL_BUDGET_CNY,
                                PHASE16_V3_PLANNER_RESERVATION_CNY,
                            ),
                        )
                        cursor.execute(
                            """INSERT INTO phase16_v3_planner_diagnostic_case_slots
                               (run_id, case_id, case_digest) VALUES (%s,%s,%s)""",
                            (
                                PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID,
                                PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID,
                                case_digest,
                            ),
                        )
                        for source, amount in PHASE16_V3_PRIOR_EXPOSURES.items():
                            cursor.execute(
                                """INSERT INTO phase16_v3_planner_diagnostic_prior_exposures
                                   (run_id, source, amount_cny) VALUES (%s,%s,%s)""",
                                (PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID, source, amount),
                            )
                    elif (
                        existing["manifest_digest"] != manifest_digest
                        or existing["planner_profile_digest"] != planner_profile_digest
                        or existing["case_digest"] != case_digest
                    ):
                        raise Phase16V3DiagnosticLedgerError("V3 diagnostic run identity conflicts")
                connection.commit()
        except psycopg.Error as error:
            raise Phase16V3DiagnosticLedgerError("V3 diagnostic ledger initialization failed") from error

    def begin_dispatch(
        self,
        *,
        case_id: str,
        planner_profile_digest: str,
        internal_request_id: str,
    ) -> Phase16V3DiagnosticDispatchAttempt:
        """在调用模型前写入不可重试 intent，并以预算上界拒绝超额发送。"""

        if case_id != PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID:
            raise Phase16V3DiagnosticLedgerError("V3 diagnostic case is not frozen")
        self._require_digest(planner_profile_digest, "planner_profile_digest")
        self._require_uuid(internal_request_id, "internal_request_id")
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT * FROM phase16_v3_planner_diagnostic_runs
                           WHERE run_id=%s FOR UPDATE""",
                        (PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID,),
                    )
                    run = cursor.fetchone()
                    if run is None:
                        raise Phase16V3DiagnosticLedgerError("V3 diagnostic run is not initialized")
                    if run["planner_profile_digest"] != planner_profile_digest:
                        raise Phase16V3DiagnosticLedgerError("V3 planner profile digest conflicts")
                    cursor.execute(
                        """SELECT 1 FROM phase16_v3_planner_diagnostic_case_outcomes
                           WHERE run_id=%s AND case_id=%s""",
                        (PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID, case_id),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase16V3DiagnosticLedgerError("V3 diagnostic run is terminal")
                    cursor.execute(
                        """SELECT 1 FROM phase16_v3_planner_diagnostic_dispatch_attempts
                           WHERE run_id=%s AND case_id=%s""",
                        (PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID, case_id),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase16V3DiagnosticLedgerError("V3 diagnostic dispatch already exists")
                    cursor.execute(
                        """SELECT COALESCE(sum(amount_cny), 0) AS prior_exposure
                           FROM phase16_v3_planner_diagnostic_prior_exposures WHERE run_id=%s""",
                        (PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID,),
                    )
                    prior_exposure = Decimal(cursor.fetchone()["prior_exposure"])
                    if prior_exposure + Decimal(run["planner_reservation_cny"]) > Decimal(
                        run["total_budget_cny"]
                    ):
                        raise Phase16V3DiagnosticLedgerError("V3 diagnostic budget exposure exceeded")
                    cursor.execute(
                        """INSERT INTO phase16_v3_planner_diagnostic_dispatch_attempts
                           (attempt_id, run_id, case_id, planner_profile_digest, internal_request_id)
                           VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                        (
                            str(uuid4()),
                            PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID,
                            case_id,
                            planner_profile_digest,
                            internal_request_id,
                        ),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except Phase16V3DiagnosticLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16V3DiagnosticLedgerError("V3 diagnostic dispatch failed") from error
        return Phase16V3DiagnosticDispatchAttempt(
            attempt_id=str(row["attempt_id"]),
            run_id=str(row["run_id"]),
            case_id=str(row["case_id"]),
            planner_profile_digest=str(row["planner_profile_digest"]),
            internal_request_id=str(row["internal_request_id"]),
        )

    def append_model_failure(
        self, fact: Phase16V3ModelFailureFact
    ) -> Phase16V3PersistedModelFailureFact:
        """追加端口失败事实；数据库保证它不能与同 attempt 的成功回执并存。"""

        if not isinstance(fact, Phase16V3ModelFailureFact):
            raise TypeError("V3 diagnostic requires a structured model failure fact")
        auth_tag = self._failure_authenticator.sign(fact)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_v3_planner_diagnostic_model_failure_facts
                           (attempt_id, failure_category, request_sent, response_digest, http_status,
                            retry_after_seconds, latency_ms, fact_digest, failure_auth_tag)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (
                            fact.attempt_id,
                            fact.category.value,
                            fact.request_sent,
                            fact.response_digest,
                            fact.http_status,
                            fact.retry_after_seconds,
                            fact.latency_ms,
                            fact.fact_digest,
                            auth_tag,
                        ),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except psycopg.Error as error:
            raise Phase16V3DiagnosticLedgerError("V3 model failure append failed") from error
        return self._failure_from_row(row)

    def append_provider_receipt(
        self, *, attempt_id: str, success: ModelSuccess
    ) -> Phase16V3ProviderReceipt:
        """追加成功调用的最小回执，使 Planner schema 失败不会被误写成端口失败。

        V3 诊断的主目标是失败分类，但“模型成功、后续结构校验失败”也必须能与
        `ModelFailure` 区分。因此这里仅记录 Provider 标识摘要、usage 和响应摘要，
        不保存模型输出或 Provider 原始 ID。
        """

        if not isinstance(success, ModelSuccess):
            raise TypeError("V3 provider receipt requires ModelSuccess")
        if (
            success.usage is None
            or not success.provider_response_id
            or success.finish_reason != "stop"
            or success.model_id != "deepseek-v4-pro"
        ):
            raise Phase16V3DiagnosticLedgerError("V3 provider receipt is incomplete")
        self._require_uuid(attempt_id, "attempt_id")
        self._require_uuid(success.request_id, "request_id")
        provider_response_id_digest = sha256(
            success.provider_response_id.encode("utf-8")
        ).hexdigest()
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    # 回执必须绑定预先持久化的内部 request 与冻结 Profile，不能借用其他
                    # dispatch 的模型响应来伪造这一次 Planner 调用成功。
                    cursor.execute(
                        """SELECT internal_request_id, planner_profile_digest
                             FROM phase16_v3_planner_diagnostic_dispatch_attempts
                            WHERE attempt_id=%s FOR UPDATE""",
                        (attempt_id,),
                    )
                    attempt = cursor.fetchone()
                    if attempt is None:
                        raise Phase16V3DiagnosticLedgerError("V3 provider receipt has no dispatch attempt")
                    if str(attempt["internal_request_id"]) != success.request_id:
                        raise Phase16V3DiagnosticLedgerError("V3 provider receipt request identity conflicts")
                    receipt_auth_tag = self._receipt_authenticator.sign(
                        attempt_id=attempt_id,
                        planner_profile_digest=str(attempt["planner_profile_digest"]),
                        provider_response_id_digest=provider_response_id_digest,
                        finish_reason=success.finish_reason,
                        model_id=success.model_id,
                        response_digest=success.response_digest,
                        input_tokens=success.usage.input_tokens,
                        output_tokens=success.usage.output_tokens,
                        total_tokens=success.usage.total_tokens,
                        latency_ms=success.latency_ms,
                    )
                    cursor.execute(
                        """INSERT INTO phase16_v3_planner_diagnostic_provider_receipts
                           (attempt_id, provider_response_id_digest, finish_reason, model_id,
                            response_digest, input_tokens, output_tokens, total_tokens, latency_ms,
                            receipt_auth_tag)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (
                            attempt_id,
                            provider_response_id_digest,
                            success.finish_reason,
                            success.model_id,
                            success.response_digest,
                            success.usage.input_tokens,
                            success.usage.output_tokens,
                            success.usage.total_tokens,
                            success.latency_ms,
                            receipt_auth_tag,
                        ),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except psycopg.Error as error:
            raise Phase16V3DiagnosticLedgerError("V3 provider receipt append failed") from error
        # RETURNING * 只包含 receipt 表自身字段；Profile 摘要来自同一事务中已锁定的
        # dispatch attempt，必须显式带回投影，不能退化成未绑定 Profile 的成功回执。
        return self._receipt_from_row(
            {**row, "planner_profile_digest": str(attempt["planner_profile_digest"])}
        )

    def append_validation_fact(
        self,
        *,
        attempt_id: str,
        verdict: Phase16V3DiagnosticValidationVerdict,
        reason_code: str,
    ) -> None:
        """在 receipt 或 failure 已写入后追加验证结论，禁止以验证事实伪造调用结果。"""

        self._require_uuid(attempt_id, "attempt_id")
        self._require_reason_code(reason_code)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_v3_planner_diagnostic_validation_facts
                           (attempt_id, verdict, reason_code) VALUES (%s,%s,%s)""",
                        (attempt_id, verdict.value, reason_code),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise Phase16V3DiagnosticLedgerError("V3 validation append failed") from error

    def verify_model_failure(
        self, fact: Phase16V3PersistedModelFailureFact
    ) -> bool:
        """公开复验失败事实 HMAC，报告器无需访问私有签名器或原始数据库连接。"""

        if not isinstance(fact, Phase16V3PersistedModelFailureFact):
            raise TypeError("V3 failure verification requires a persisted failure fact")
        return self._failure_authenticator.verify(fact)

    def verify_provider_receipt(self, receipt: Phase16V3ProviderReceipt) -> bool:
        """复验成功回执的 HMAC，调用方不得只因数据库存在一行 receipt 就认定模型成功。"""

        if not isinstance(receipt, Phase16V3ProviderReceipt):
            raise TypeError("V3 receipt verification requires a persisted provider receipt")
        return self._receipt_authenticator.verify(receipt)

    def close_case(
        self,
        *,
        case_id: str,
        status: Phase16V3DiagnosticOutcomeStatus,
        reason_code: str,
    ) -> Phase16V3DiagnosticOutcome:
        """依据数据库已有 attempt、failure/receipt 与 validation 事实关闭唯一 case。"""

        if case_id != PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID:
            raise Phase16V3DiagnosticLedgerError("V3 diagnostic case is not frozen")
        self._require_reason_code(reason_code)
        payload = {"case_id": case_id, "status": status.value, "reason_code": reason_code}
        digest = sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    if status is Phase16V3DiagnosticOutcomeStatus.PASS:
                        # PASS 是唯一可被后续报告解释为 Planner 成功的终态；在同一事务中
                        # 重读并复验 receipt，避免 append 后被数据库直写替换的竞态窗口。
                        receipt = self._load_provider_receipt_in_cursor(cursor, case_id=case_id)
                        if receipt is None or not self.verify_provider_receipt(receipt):
                            raise Phase16V3DiagnosticLedgerError(
                                "V3 PASS requires an authenticated provider receipt"
                            )
                    cursor.execute(
                        """INSERT INTO phase16_v3_planner_diagnostic_case_outcomes
                           (run_id, case_id, status, reason_code, outcome_digest)
                           VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                        (
                            PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID,
                            case_id,
                            status.value,
                            reason_code,
                            digest,
                        ),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except psycopg.Error as error:
            raise Phase16V3DiagnosticLedgerError("V3 diagnostic outcome append failed") from error
        return Phase16V3DiagnosticOutcome(
            case_id=str(row["case_id"]),
            status=Phase16V3DiagnosticOutcomeStatus(row["status"]),
            reason_code=str(row["reason_code"]),
            outcome_digest=str(row["outcome_digest"]),
        )

    @staticmethod
    def _failure_from_row(row: dict[str, Any]) -> Phase16V3PersistedModelFailureFact:
        """将数据库 row 缩减为公开读取投影，原始 DB 行不会泄漏到调用方。"""

        return Phase16V3PersistedModelFailureFact(
            attempt_id=str(row["attempt_id"]),
            category=_failure_category_from_value(str(row["failure_category"])),
            request_sent=None if row["request_sent"] is None else bool(row["request_sent"]),
            response_digest=row["response_digest"],
            http_status=row["http_status"],
            retry_after_seconds=row["retry_after_seconds"],
            latency_ms=Decimal(row["latency_ms"]),
            fact_digest=str(row["fact_digest"]),
            failure_auth_tag=str(row["failure_auth_tag"]),
        )

    @staticmethod
    def _receipt_from_row(row: dict[str, Any]) -> Phase16V3ProviderReceipt:
        """将成功回执限制为脱敏字段；此投影不公开 Provider 原始 ID 或模型正文。"""

        return Phase16V3ProviderReceipt(
            attempt_id=str(row["attempt_id"]),
            planner_profile_digest=str(row["planner_profile_digest"]),
            provider_response_id_digest=str(row["provider_response_id_digest"]),
            finish_reason=str(row["finish_reason"]),
            model_id=str(row["model_id"]),
            response_digest=str(row["response_digest"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            total_tokens=int(row["total_tokens"]),
            latency_ms=Decimal(row["latency_ms"]),
            receipt_auth_tag=str(row["receipt_auth_tag"]),
        )

    def _load_provider_receipt_in_cursor(
        self, cursor: Any, *, case_id: str
    ) -> Phase16V3ProviderReceipt | None:
        """在终态事务内读取唯一回执，确保 PASS 的认证输入与 case slot 同源。"""

        cursor.execute(
            """SELECT receipt.*, attempt.planner_profile_digest
                 FROM phase16_v3_planner_diagnostic_provider_receipts receipt
                 JOIN phase16_v3_planner_diagnostic_dispatch_attempts attempt
                   ON attempt.attempt_id=receipt.attempt_id
                WHERE attempt.run_id=%s AND attempt.case_id=%s
                FOR UPDATE""",
            (PHASE16_V3_PLANNER_DIAGNOSTIC_RUN_ID, case_id),
        )
        row = cursor.fetchone()
        return None if row is None else self._receipt_from_row(row)

    @staticmethod
    def _require_digest(value: str, name: str) -> None:
        """所有身份摘要必须是小写 SHA-256，不能将自由文本写入证据身份列。"""

        if not isinstance(value, str) or len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError(f"{name} must be a SHA-256 digest")

    @staticmethod
    def _require_uuid(value: str, name: str) -> None:
        """通过 UUID 边界防止异常文本、URL 或 Prompt 片段进入内部调用身份。"""

        try:
            UUID(value)
        except (AttributeError, ValueError) as error:
            raise ValueError(f"{name} must be a UUID") from error

    @staticmethod
    def _require_reason_code(value: str) -> None:
        """reason code 只允许稳定大写标识，报告不接受未脱敏异常字符串。"""

        if not isinstance(value, str) or not value or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in value
        ):
            raise ValueError("V3 reason_code must be an uppercase stable identifier")


def initialize_phase16_v3_planner_diagnostic_ledger_schema(settings: Any) -> None:
    """执行 V3 专属 DDL；测试与迁移共用同一 UTF-8 SQL，避免 schema 语义漂移。"""

    sql_path = Path(__file__).resolve().parents[2] / "docker" / "init_phase16_v3_planner_diagnostic.sql"
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql_path.read_text(encoding="utf-8"))
        connection.commit()


def _failure_category_from_value(
    value: str,
) -> ModelFailureCategory | Phase16V3DiagnosticFailureCategory:
    """优先还原共享端口分类；只有 V3 专属契约失约才使用 V3 自有封闭枚举。"""

    try:
        return ModelFailureCategory(value)
    except ValueError:
        return Phase16V3DiagnosticFailureCategory(value)
