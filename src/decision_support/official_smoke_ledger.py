"""Phase 16 正式真实模型 smoke 的独立 PostgreSQL 账本。

该模块只记录发送前后的最小审计事实，永不保存 API Key、Prompt、模型正文、思维链或
经营建议文本。它与历史 ``phase16_smoke_*`` 预算表物理隔离，不能把旧直接模式结果
误写为正式 10/10 证据。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import hmac
import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from src.decision_support.official_smoke_evidence import (
    FORMAL_INPUT_PRICE_CNY_PER_MILLION,
    FORMAL_OUTPUT_PRICE_CNY_PER_MILLION,
    PHASE16_OFFICIAL_SMOKE_RUN_ID,
    Phase16OfficialSmokeEvidenceManifest,
)
from src.specialist_runtime.profiles import FORMAL_MODEL_ID


# 该三项数值属于 D-168 的固定正式预算事实。历史支出不构成正式成功证据，
# 但必须在任何正式 case claim 前占用同一 Phase 16 一元总上限。
PHASE16_OFFICIAL_SMOKE_TOTAL_BUDGET_CNY = Decimal("1.000000")
PHASE16_OFFICIAL_SMOKE_HISTORICAL_DIRECT_MODE_CNY = Decimal("0.073220")
PHASE16_OFFICIAL_SMOKE_ANALYST_RESERVATION_CNY = Decimal("0.040000")
PHASE16_OFFICIAL_SMOKE_PLANNER_RESERVATION_CNY = Decimal("0.052000")
PHASE16_OFFICIAL_SMOKE_CASE_RESERVATION_CNY = Decimal("0.092000")
PHASE16_OFFICIAL_SMOKE_MAX_EXPOSURE_CNY = Decimal("0.993220")
PHASE16_OFFICIAL_SMOKE_FIXED_CASE_COUNT = 10
_ALLOWED_PROVIDER_FINISH_REASONS = frozenset(
    {"stop", "length", "content_filter", "tool_calls"}
)


class Phase16OfficialSmokeLedgerError(RuntimeError):
    """正式账本事实不一致或不满足固定 run 边界时抛出。"""


class Phase16OfficialSmokeDispatchStage(StrEnum):
    """正式 run 的两段固定调用顺序，任何额外 stage 都不能进入账本。"""

    ANALYST = "ANALYST"
    PLANNER = "PLANNER"


class Phase16OfficialSmokeValidationVerdict(StrEnum):
    """对固定 stage 的最终验证结论；没有可重试的中间态。"""

    PASS = "PASS"
    FAILED = "FAILED"
    # ``begin_dispatch`` 为零重试先写入发送意图；若共享 Runner 在进入模型端口前
    # deadline 耗尽，或端口明确 ``request_sent=False``，该 intent 必须可审计地闭合为
    # BLOCKED。它不是模型失败，也不能携带 Provider receipt。
    BLOCKED = "BLOCKED"


class Phase16OfficialSmokeCaseOutcomeStatus(StrEnum):
    """每个固定 case 的唯一 terminal outcome；正式路径没有可重开或重试状态。"""

    PASS = "PASS"
    FAILED = "FAILED"
    # BLOCKED 只表示尚未产生失败响应的本地发送前阻断：允许零次 dispatch，或允许
    # 已通过 Analyst receipt/validation 但 Planner 尚未创建 dispatch。它不能掩盖任一
    # 已发送但未验证、失败或 Planner 阶段的调用，后者必须走 FAILED 恢复链。
    BLOCKED = "BLOCKED"


class Phase16OfficialSmokeReceiptAuthenticator:
    """为正式 Provider receipt 提供数据库外的最小完整性证明。

    签名 key 只由受控 Smoke Runner 的启动配置提供，绝不写入 PostgreSQL、日志或报告。
    因此拥有数据库写权限的客户端可以插入形状正确的行，却不能生成会被报告校验器接受的
    HMAC 标签。该标签证明“可信发送进程记录了这份回执”，不冒充 Provider 的数字签名。
    """

    def __init__(self, signing_key: bytes) -> None:
        """固定接受至少 256 位的二进制 key，拒绝把可猜测字符串当作正式证据密钥。"""

        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("formal receipt signing key must contain at least 256 bits")
        self._signing_key = signing_key

    def sign(
        self,
        *,
        attempt_id: str,
        stage: Phase16OfficialSmokeDispatchStage,
        profile_digest: str,
        provider_response_id_digest: str,
        finish_reason: str,
        model_id: str,
        response_digest: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        latency_ms: Decimal,
        input_cost_cny: Decimal,
        output_cost_cny: Decimal,
        total_cost_cny: Decimal,
    ) -> str:
        """对不含 Prompt/正文的 receipt 摘要事实生成稳定 HMAC-SHA256 标签。"""

        return hmac.new(
            self._signing_key,
            self._canonical_receipt_payload(
                attempt_id=attempt_id,
                stage=stage,
                profile_digest=profile_digest,
                provider_response_id_digest=provider_response_id_digest,
                finish_reason=finish_reason,
                model_id=model_id,
                response_digest=response_digest,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
                input_cost_cny=input_cost_cny,
                output_cost_cny=output_cost_cny,
                total_cost_cny=total_cost_cny,
            ),
            sha256,
        ).hexdigest()

    def verify(
        self,
        *,
        receipt_auth_tag: str,
        attempt_id: str,
        stage: Phase16OfficialSmokeDispatchStage,
        profile_digest: str,
        provider_response_id_digest: str,
        finish_reason: str,
        model_id: str,
        response_digest: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        latency_ms: Decimal,
        input_cost_cny: Decimal,
        output_cost_cny: Decimal,
        total_cost_cny: Decimal,
    ) -> bool:
        """以恒定时间比较重建标签，拒绝直写数据库的伪造 receipt。"""

        if not self._is_auth_tag(receipt_auth_tag):
            return False
        expected = self.sign(
            attempt_id=attempt_id,
            stage=stage,
            profile_digest=profile_digest,
            provider_response_id_digest=provider_response_id_digest,
            finish_reason=finish_reason,
            model_id=model_id,
            response_digest=response_digest,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            input_cost_cny=input_cost_cny,
            output_cost_cny=output_cost_cny,
            total_cost_cny=total_cost_cny,
        )
        return hmac.compare_digest(expected, receipt_auth_tag)

    @staticmethod
    def _canonical_receipt_payload(
        *,
        attempt_id: str,
        stage: Phase16OfficialSmokeDispatchStage,
        profile_digest: str,
        provider_response_id_digest: str,
        finish_reason: str,
        model_id: str,
        response_digest: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        latency_ms: Decimal,
        input_cost_cny: Decimal,
        output_cost_cny: Decimal,
        total_cost_cny: Decimal,
    ) -> bytes:
        """只序列化审计白名单字段，保证标签计算不会间接持久化模型正文或思维链。"""

        payload = {
            "attempt_id": str(UUID(attempt_id)),
            "finish_reason": finish_reason,
            "input_cost_cny": str(Decimal(input_cost_cny).quantize(Decimal("0.000001"))),
            "input_tokens": input_tokens,
            "latency_ms": str(Decimal(latency_ms).quantize(Decimal("0.001"))),
            "model_id": model_id,
            "output_cost_cny": str(Decimal(output_cost_cny).quantize(Decimal("0.000001"))),
            "output_tokens": output_tokens,
            "profile_digest": profile_digest,
            "provider_response_id_digest": provider_response_id_digest,
            "response_digest": response_digest,
            "stage": stage.value,
            "total_cost_cny": str(Decimal(total_cost_cny).quantize(Decimal("0.000001"))),
            "total_tokens": total_tokens,
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _is_auth_tag(value: str) -> bool:
        """标签只接受固定长度的小写十六进制，避免把自由文本写入完整性字段。"""

        return isinstance(value, str) and len(value) == 64 and all(
            character in "0123456789abcdef" for character in value
        )


@dataclass(frozen=True)
class Phase16OfficialSmokeRunSnapshot:
    """从 run、历史支出和固定 slot 事实重建的不可变预算快照。"""

    run_id: str
    manifest_digest: str
    historical_spend_cny: Decimal
    fixed_case_slot_count: int
    case_reservation_cny: Decimal
    maximum_exposure_cny: Decimal


@dataclass(frozen=True)
class Phase16OfficialSmokeCaseSlot:
    """正式 run 的一个不可扩展 case slot，只保存 case 身份摘要和两段预约额度。"""

    run_id: str
    slot_position: int
    case_id: str
    case_digest: str
    analyst_reservation_cny: Decimal
    planner_reservation_cny: Decimal


@dataclass(frozen=True)
class Phase16OfficialSmokeCaseClaim:
    """一个固定 case 的唯一预算 claim；重复读取只重放原事实，不会重新预约。"""

    claim_id: str
    run_id: str
    case_id: str
    manifest_digest: str
    reserved_amount_cny: Decimal
    created: bool


@dataclass(frozen=True)
class Phase16OfficialSmokeDispatchAttempt:
    """发送前追加的一次外部 dispatch 意图，不保存 Prompt 或模型输入内容。"""

    attempt_id: str
    claim_id: str
    run_id: str
    case_id: str
    stage: Phase16OfficialSmokeDispatchStage
    profile_digest: str
    internal_request_id: str


@dataclass(frozen=True)
class Phase16OfficialSmokeProviderReceipt:
    """供应商最小可审计回执，字段全部可脱敏展示且不包含模型正文。"""

    attempt_id: str
    provider_response_id_digest: str
    finish_reason: str
    model_id: str
    response_digest: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: Decimal
    input_cost_cny: Decimal
    output_cost_cny: Decimal
    total_cost_cny: Decimal
    receipt_auth_tag: str


@dataclass(frozen=True)
class Phase16OfficialSmokeValidationFact:
    """独立于 Provider receipt 的验证结论，保留 Schema/EvidenceRef 校验的摘要而非正文。"""

    attempt_id: str
    verdict: Phase16OfficialSmokeValidationVerdict
    reason_code: str
    validation_digest: str


@dataclass(frozen=True)
class Phase16OfficialSmokeCaseOutcome:
    """一个 slot 的最终事实，报告只能从此 append-only 结论与 receipt 聚合生成。"""

    run_id: str
    case_id: str
    claim_id: str
    status: Phase16OfficialSmokeCaseOutcomeStatus
    reason_code: str
    outcome_digest: str


class PostgresPhase16OfficialSmokeLedger:
    """以 run 行锁和 append-only 事实表装配正式 smoke 的基础预算边界。"""

    def __init__(
        self,
        settings: Any,
        *,
        receipt_authenticator: Phase16OfficialSmokeReceiptAuthenticator,
    ) -> None:
        """保存可信 Settings 与数据库外 receipt 签名器，二者都不写入审计事实。"""

        self._settings = settings
        self._receipt_authenticator = receipt_authenticator

    def ensure_run(
        self,
        manifest: Phase16OfficialSmokeEvidenceManifest,
    ) -> Phase16OfficialSmokeRunSnapshot:
        """一次性导入历史支出并冻结十个 case slot，重复调用只允许同一事实重放。

        先插入 run 再锁定该行，因此不同进程的并发初始化会在同一序列化点比较
        Manifest，而不会产生两份历史扣费或第十一个 slot。
        """

        self._validate_manifest(manifest)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._assert_schema_contract_in_cursor(cursor)
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_runs
                       (run_id, manifest_digest, analyst_profile_digest, planner_profile_digest,
                        total_budget_cny)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (run_id) DO NOTHING;""",
                    (
                        manifest.run_id,
                        manifest.manifest_digest,
                        manifest.profile_digests["analyst"],
                        manifest.profile_digests["planner"],
                        PHASE16_OFFICIAL_SMOKE_TOTAL_BUDGET_CNY,
                    ),
                )
                cursor.execute(
                    "SELECT * FROM phase16_official_smoke_runs WHERE run_id=%s FOR UPDATE;",
                    (manifest.run_id,),
                )
                run_row = cursor.fetchone()
                self._assert_run_matches_manifest(run_row, manifest)

                cursor.execute(
                    """INSERT INTO phase16_official_smoke_historical_spend
                       (run_id, source, amount_cny)
                       VALUES (%s,'HISTORICAL_DIRECT_MODE',%s)
                       ON CONFLICT (run_id, source) DO NOTHING;""",
                    (manifest.run_id, PHASE16_OFFICIAL_SMOKE_HISTORICAL_DIRECT_MODE_CNY),
                )
                cursor.execute(
                    """SELECT amount_cny FROM phase16_official_smoke_historical_spend
                       WHERE run_id=%s AND source='HISTORICAL_DIRECT_MODE' FOR UPDATE;""",
                    (manifest.run_id,),
                )
                historical_row = cursor.fetchone()
                if historical_row is None or Decimal(historical_row["amount_cny"]) != PHASE16_OFFICIAL_SMOKE_HISTORICAL_DIRECT_MODE_CNY:
                    raise Phase16OfficialSmokeLedgerError("formal historical spend does not match D-168")

                for slot_position, case_id in enumerate(manifest.case_ids, start=1):
                    cursor.execute(
                        """INSERT INTO phase16_official_smoke_case_slots
                           (run_id, slot_position, case_id, case_digest,
                            analyst_reservation_cny, planner_reservation_cny)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (run_id, case_id) DO NOTHING;""",
                        (
                            manifest.run_id,
                            slot_position,
                            case_id,
                            manifest.case_digests[case_id],
                            PHASE16_OFFICIAL_SMOKE_ANALYST_RESERVATION_CNY,
                            PHASE16_OFFICIAL_SMOKE_PLANNER_RESERVATION_CNY,
                        ),
                    )

                cursor.execute(
                    """SELECT * FROM phase16_official_smoke_case_slots
                       WHERE run_id=%s ORDER BY slot_position FOR UPDATE;""",
                    (manifest.run_id,),
                )
                slot_rows = cursor.fetchall()
                self._assert_slots_match_manifest(slot_rows, manifest)
            connection.commit()
        return self.snapshot()

    def list_case_slots(self) -> tuple[Phase16OfficialSmokeCaseSlot, ...]:
        """按冻结顺序返回十个 slot，调用方不能通过列表顺序重新解释 case 身份。"""

        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._assert_schema_contract_in_cursor(cursor)
                cursor.execute(
                    """SELECT * FROM phase16_official_smoke_case_slots
                       WHERE run_id=%s ORDER BY slot_position;""",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID,),
                )
                rows = cursor.fetchall()
        return tuple(self._slot_from_row(row) for row in rows)

    def claim_case(self, case_id: str) -> Phase16OfficialSmokeCaseClaim:
        """原子预约一个已冻结 case，跨进程竞争只能产生一条 append-only claim。

        claim 不会因真实费用较低而删除或释放。这样即使发生崩溃、usage 缺失或后续
        验证失败，仍没有第十一个 case 可借余额发送，预算暴露始终受 0.993220 上界保护。
        """

        if not case_id:
            raise Phase16OfficialSmokeLedgerError("formal smoke case ID is required")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._assert_schema_contract_in_cursor(cursor)
                # run 行是所有 claim 的单一线性化点；先锁它再计算累计暴露，两个连接
                # 无法同时看到同一份可用预算并双写穿一元硬上限。
                cursor.execute(
                    "SELECT * FROM phase16_official_smoke_runs WHERE run_id=%s FOR UPDATE;",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID,),
                )
                run_row = cursor.fetchone()
                if run_row is None:
                    raise Phase16OfficialSmokeLedgerError("formal smoke run has not been initialized")
                cursor.execute(
                    """SELECT * FROM phase16_official_smoke_case_slots
                       WHERE run_id=%s AND case_id=%s;""",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID, case_id),
                )
                slot_row = cursor.fetchone()
                if slot_row is None:
                    raise Phase16OfficialSmokeLedgerError("case is not a frozen case slot")
                cursor.execute(
                    """SELECT * FROM phase16_official_smoke_case_claims
                       WHERE run_id=%s AND case_id=%s FOR UPDATE;""",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID, case_id),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    return self._claim_from_row(existing, created=False)

                cursor.execute(
                    """SELECT
                           COALESCE((SELECT sum(amount_cny)
                                       FROM phase16_official_smoke_historical_spend
                                      WHERE run_id=%s), 0) AS historical_spend_cny,
                           COALESCE((SELECT sum(reserved_amount_cny)
                                       FROM phase16_official_smoke_case_claims
                                      WHERE run_id=%s), 0) AS claimed_reservation_cny;""",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID, PHASE16_OFFICIAL_SMOKE_RUN_ID),
                )
                exposure_row = cursor.fetchone()
                next_exposure = (
                    Decimal(exposure_row["historical_spend_cny"])
                    + Decimal(exposure_row["claimed_reservation_cny"])
                    + PHASE16_OFFICIAL_SMOKE_CASE_RESERVATION_CNY
                )
                if next_exposure > Decimal(run_row["total_budget_cny"]):
                    raise Phase16OfficialSmokeLedgerError("formal smoke budget exposure exceeded")
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_case_claims
                       (claim_id, run_id, case_id, manifest_digest, reserved_amount_cny)
                       VALUES (%s::uuid,%s,%s,%s,%s) RETURNING *;""",
                    (
                        str(uuid4()),
                        PHASE16_OFFICIAL_SMOKE_RUN_ID,
                        case_id,
                        run_row["manifest_digest"],
                        PHASE16_OFFICIAL_SMOKE_CASE_RESERVATION_CNY,
                    ),
                )
                created = cursor.fetchone()
            connection.commit()
        return self._claim_from_row(created, created=True)

    def begin_dispatch(
        self,
        *,
        claim_id: str,
        stage: Phase16OfficialSmokeDispatchStage,
        profile_digest: str,
        internal_request_id: str,
    ) -> Phase16OfficialSmokeDispatchAttempt:
        """在外部请求离开进程前追加唯一 attempt，并让 Planner 受 Analyst PASS 约束。

        无论网络调用是否最终可观察，attempt 一旦存在就占用该 stage；崩溃恢复只能
        追加 UNKNOWN/FAILED 终态，不能生成第二次请求来“补齐”正式 10/10 证据。
        """

        if not claim_id or not self._is_digest(profile_digest):
            raise Phase16OfficialSmokeLedgerError("formal dispatch identity is invalid")
        try:
            # 内部 request ID 只用于把 attempt 与共享 Runner 的一次调用关联。强制 UUID
            # 让任意 Prompt、API Key 或运营建议文本无法被借此字段写入审计账本。
            normalized_internal_request_id = str(UUID(internal_request_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise Phase16OfficialSmokeLedgerError("formal dispatch identity is invalid") from error
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._assert_schema_contract_in_cursor(cursor)
                cursor.execute(
                    "SELECT * FROM phase16_official_smoke_runs WHERE run_id=%s FOR UPDATE;",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID,),
                )
                run_row = cursor.fetchone()
                if run_row is None:
                    raise Phase16OfficialSmokeLedgerError("formal smoke run has not been initialized")
                cursor.execute(
                    """SELECT * FROM phase16_official_smoke_case_claims
                       WHERE run_id=%s AND claim_id=%s::uuid FOR UPDATE;""",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID, claim_id),
                )
                claim_row = cursor.fetchone()
                if claim_row is None:
                    raise Phase16OfficialSmokeLedgerError("formal dispatch claim is unknown")
                expected_profile_digest = (
                    run_row["analyst_profile_digest"]
                    if stage is Phase16OfficialSmokeDispatchStage.ANALYST
                    else run_row["planner_profile_digest"]
                )
                if profile_digest != expected_profile_digest:
                    raise Phase16OfficialSmokeLedgerError("formal dispatch profile digest conflicts with run")
                cursor.execute(
                    """SELECT attempt_id FROM phase16_official_smoke_dispatch_attempts
                       WHERE claim_id=%s::uuid AND stage=%s FOR UPDATE;""",
                    (claim_id, stage.value),
                )
                if cursor.fetchone() is not None:
                    raise Phase16OfficialSmokeLedgerError("formal dispatch attempt already exists")
                if stage is Phase16OfficialSmokeDispatchStage.PLANNER:
                    cursor.execute(
                        """SELECT validation.verdict
                             FROM phase16_official_smoke_dispatch_attempts attempt
                             JOIN phase16_official_smoke_validation_facts validation
                               ON validation.attempt_id=attempt.attempt_id
                            WHERE attempt.claim_id=%s::uuid AND attempt.stage='ANALYST';""",
                        (claim_id,),
                    )
                    analyst_validation = cursor.fetchone()
                    if analyst_validation is None or analyst_validation["verdict"] != Phase16OfficialSmokeValidationVerdict.PASS.value:
                        raise Phase16OfficialSmokeLedgerError("planner dispatch requires analyst validation PASS")
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_dispatch_attempts
                       (attempt_id, run_id, case_id, claim_id, stage, profile_digest, internal_request_id)
                       VALUES (%s::uuid,%s,%s,%s::uuid,%s,%s,%s) RETURNING *;""",
                    (
                        str(uuid4()),
                        PHASE16_OFFICIAL_SMOKE_RUN_ID,
                        claim_row["case_id"],
                        claim_id,
                        stage.value,
                        profile_digest,
                        normalized_internal_request_id,
                    ),
                )
                row = cursor.fetchone()
            connection.commit()
        return self._attempt_from_row(row)

    def append_provider_receipt(
        self,
        *,
        attempt_id: str,
        provider_response_id: str,
        finish_reason: str,
        model_id: str,
        response_digest: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        latency_ms: Decimal,
    ) -> Phase16OfficialSmokeProviderReceipt:
        """追加一次完整 Provider receipt，并由冻结价格计算成本而非信任调用方报价。"""

        if (
            not attempt_id
            or not self._is_non_blank(provider_response_id)
            or finish_reason not in _ALLOWED_PROVIDER_FINISH_REASONS
            or model_id != FORMAL_MODEL_ID
            or not self._is_digest(response_digest)
            or not self._valid_usage(input_tokens, output_tokens, total_tokens)
            or Decimal(latency_ms) < 0
        ):
            raise Phase16OfficialSmokeLedgerError("formal provider receipt is invalid")
        # Provider 的响应 ID 来自不可信网络载荷。正式报告只需要可复验的关联性，
        # 因此保存固定长度 SHA-256 摘要而非原始值，避免响应字段携带意外敏感文本。
        provider_response_id_digest = self._text_digest(provider_response_id)
        input_cost, output_cost, total_cost = self._usage_cost(input_tokens, output_tokens)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._assert_schema_contract_in_cursor(cursor)
                cursor.execute(
                    """SELECT attempt.*, slot.analyst_reservation_cny, slot.planner_reservation_cny
                         FROM phase16_official_smoke_dispatch_attempts attempt
                         JOIN phase16_official_smoke_case_slots slot
                           ON slot.run_id=attempt.run_id AND slot.case_id=attempt.case_id
                        WHERE attempt.attempt_id=%s::uuid FOR UPDATE;""",
                    (attempt_id,),
                )
                attempt_row = cursor.fetchone()
                if attempt_row is None:
                    raise Phase16OfficialSmokeLedgerError("formal dispatch attempt is unknown")
                stage_limit = Decimal(
                    attempt_row["analyst_reservation_cny"]
                    if attempt_row["stage"] == Phase16OfficialSmokeDispatchStage.ANALYST.value
                    else attempt_row["planner_reservation_cny"]
                )
                if total_cost > stage_limit:
                    raise Phase16OfficialSmokeLedgerError("provider receipt cost exceeds frozen stage reservation")
                cursor.execute(
                    """SELECT attempt_id FROM phase16_official_smoke_provider_receipts
                       WHERE attempt_id=%s::uuid FOR UPDATE;""",
                    (attempt_id,),
                )
                if cursor.fetchone() is not None:
                    raise Phase16OfficialSmokeLedgerError("provider receipt already exists")
                # 同一 Provider response ID 只能对应一次正式 dispatch。即使应用层在
                # 并发窗口中同时观察到“尚不存在”，数据库的 UNIQUE 约束仍是最终防线；
                # 这里先给顺序调用一个语义明确、且不泄露原始 Provider ID 的失败原因。
                cursor.execute(
                    """SELECT attempt_id FROM phase16_official_smoke_provider_receipts
                       WHERE provider_response_id_digest=%s FOR UPDATE;""",
                    (provider_response_id_digest,),
                )
                if cursor.fetchone() is not None:
                    raise Phase16OfficialSmokeLedgerError(
                        "provider response ID already belongs to another formal dispatch attempt"
                    )
                # 标签由进程外于 PostgreSQL 的 HMAC key 生成并绑定 attempt/stage/Profile、
                # usage、成本与所有 Provider 摘要。数据库直写者无法得到 key，因此其行不会
                # 通过后续正式报告的 verify_case_outcome_receipts() 完整性校验。
                receipt_auth_tag = self._receipt_authenticator.sign(
                    attempt_id=attempt_id,
                    stage=Phase16OfficialSmokeDispatchStage(attempt_row["stage"]),
                    profile_digest=attempt_row["profile_digest"],
                    provider_response_id_digest=provider_response_id_digest,
                    finish_reason=finish_reason,
                    model_id=model_id,
                    response_digest=response_digest,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    latency_ms=Decimal(latency_ms),
                    input_cost_cny=input_cost,
                    output_cost_cny=output_cost,
                    total_cost_cny=total_cost,
                )
                try:
                    cursor.execute(
                        """INSERT INTO phase16_official_smoke_provider_receipts
                           (attempt_id, provider_response_id_digest, finish_reason, model_id, response_digest,
                            input_tokens, output_tokens, total_tokens, latency_ms,
                            input_cost_cny, output_cost_cny, total_cost_cny, receipt_auth_tag)
                           VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *;""",
                        (
                            attempt_id,
                            provider_response_id_digest,
                            finish_reason,
                            model_id,
                            response_digest,
                            input_tokens,
                            output_tokens,
                            total_tokens,
                            Decimal(latency_ms),
                            input_cost,
                            output_cost,
                            total_cost,
                            receipt_auth_tag,
                        ),
                    )
                except psycopg.errors.UniqueViolation as error:
                    # 该分支只会在并发写入穿过前置查询时触发。向上统一为账本领域错误，
                    # 让 Runner 把本 case 记为失败而非把驱动异常误当成可重试网络故障。
                    raise Phase16OfficialSmokeLedgerError(
                        "provider response ID already belongs to another formal dispatch attempt"
                    ) from error
                row = cursor.fetchone()
            connection.commit()
        return self._receipt_from_row(row)

    def append_validation_fact(
        self,
        *,
        attempt_id: str,
        verdict: Phase16OfficialSmokeValidationVerdict,
        reason_code: str,
        validation_digest: str,
    ) -> Phase16OfficialSmokeValidationFact:
        """追加一次最终验证结论；PASS 只能建立在已保存的完整 Provider receipt 上。"""

        if not attempt_id or not self._is_reason_code(reason_code) or not self._is_digest(validation_digest):
            raise Phase16OfficialSmokeLedgerError("formal validation fact is invalid")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._assert_schema_contract_in_cursor(cursor)
                cursor.execute(
                    """SELECT attempt_id FROM phase16_official_smoke_dispatch_attempts
                       WHERE attempt_id=%s::uuid FOR UPDATE;""",
                    (attempt_id,),
                )
                if cursor.fetchone() is None:
                    raise Phase16OfficialSmokeLedgerError("formal validation attempt is unknown")
                cursor.execute(
                    """SELECT attempt_id FROM phase16_official_smoke_validation_facts
                       WHERE attempt_id=%s::uuid FOR UPDATE;""",
                    (attempt_id,),
                )
                if cursor.fetchone() is not None:
                    raise Phase16OfficialSmokeLedgerError("validation fact already exists")
                if verdict is Phase16OfficialSmokeValidationVerdict.PASS:
                    cursor.execute(
                        """SELECT attempt_id FROM phase16_official_smoke_provider_receipts
                           WHERE attempt_id=%s::uuid;""",
                        (attempt_id,),
                    )
                    if cursor.fetchone() is None:
                        raise Phase16OfficialSmokeLedgerError("validation PASS requires provider receipt")
                if verdict is Phase16OfficialSmokeValidationVerdict.BLOCKED:
                    # ``BLOCKED`` 只能表达“明确尚未发送”的本地事实。若已有 Provider
                    # receipt，网络边界已经被跨越，不能再把外部失败粉饰为未发送阻断。
                    cursor.execute(
                        """SELECT attempt_id FROM phase16_official_smoke_provider_receipts
                           WHERE attempt_id=%s::uuid;""",
                        (attempt_id,),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase16OfficialSmokeLedgerError(
                            "validation BLOCKED cannot carry provider receipt"
                        )
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_validation_facts
                       (attempt_id, verdict, reason_code, validation_digest)
                       VALUES (%s::uuid,%s,%s,%s) RETURNING *;""",
                    (attempt_id, verdict.value, reason_code, validation_digest),
                )
                row = cursor.fetchone()
            connection.commit()
        return self._validation_from_row(row)

    def close_case(
        self,
        *,
        claim_id: str,
        status: Phase16OfficialSmokeCaseOutcomeStatus,
        reason_code: str,
    ) -> Phase16OfficialSmokeCaseOutcome:
        """追加一个 case 终态；PASS 必须精确闭合两段 receipt 与验证事实。"""

        if not claim_id or not self._is_reason_code(reason_code):
            raise Phase16OfficialSmokeLedgerError("formal case outcome identity is invalid")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._assert_schema_contract_in_cursor(cursor)
                cursor.execute(
                    "SELECT * FROM phase16_official_smoke_runs WHERE run_id=%s FOR UPDATE;",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID,),
                )
                if cursor.fetchone() is None:
                    raise Phase16OfficialSmokeLedgerError("formal smoke run has not been initialized")
                outcome = self._close_case_in_cursor(
                    cursor,
                    claim_id=claim_id,
                    status=status,
                    reason_code=reason_code,
                )
            connection.commit()
        return outcome

    def recover_open_attempts(self) -> tuple[Phase16OfficialSmokeCaseOutcome, ...]:
        """从所有未闭合 claim 恢复终态，绝不把已发送调用重试为第二次请求。

        crash 可能发生在 attempt 写入后、validation 写入后，或两个 PASS validation
        都已提交但 outcome 尚未提交的任一点。恢复以 PostgreSQL 的已有事实为唯一依据：
        已 FAILED 的 validation 直接闭合 FAILED；缺 validation 的已发送 attempt 追加
        ``UNKNOWN_ATTEMPT_AFTER_RESTART`` 后闭合 FAILED；完整两段 PASS receipt/validation
        则只补写 PASS outcome。未发送的 Planner 不会被本方法伪造为失败，后续 Runner
        仍可在不重发任何已发送调用的前提下继续该唯一 stage。
        """

        recovered: list[Phase16OfficialSmokeCaseOutcome] = []
        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._assert_schema_contract_in_cursor(cursor)
                cursor.execute(
                    "SELECT * FROM phase16_official_smoke_runs WHERE run_id=%s FOR UPDATE;",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID,),
                )
                if cursor.fetchone() is None:
                    raise Phase16OfficialSmokeLedgerError("formal smoke run has not been initialized")
                cursor.execute(
                    """SELECT claim.*
                         FROM phase16_official_smoke_case_claims claim
                    LEFT JOIN phase16_official_smoke_case_outcomes outcome
                           ON outcome.claim_id=claim.claim_id
                        WHERE claim.run_id=%s
                          AND outcome.claim_id IS NULL
                     ORDER BY claim.created_at, claim.claim_id
                       FOR UPDATE OF claim;""",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID,),
                )
                open_claims = cursor.fetchall()
                for claim in open_claims:
                    cursor.execute(
                        """SELECT attempt.*, validation.verdict, validation.reason_code,
                                   receipt.attempt_id AS receipt_attempt_id
                             FROM phase16_official_smoke_dispatch_attempts attempt
                        LEFT JOIN phase16_official_smoke_validation_facts validation
                               ON validation.attempt_id=attempt.attempt_id
                        LEFT JOIN phase16_official_smoke_provider_receipts receipt
                               ON receipt.attempt_id=attempt.attempt_id
                            WHERE attempt.claim_id=%s::uuid
                         ORDER BY attempt.created_at, attempt.attempt_id
                           FOR UPDATE OF attempt;""",
                        (str(claim["claim_id"]),),
                    )
                    attempts = cursor.fetchall()
                    # claim 已预约但尚未写 attempt 时，外部请求尚未离开进程；保留它
                    # 允许 Runner 从未发送的唯一 stage 继续，而不把本地崩溃错误计成模型失败。
                    if not attempts:
                        continue

                    failed_validation = next(
                        (
                            attempt
                            for attempt in attempts
                            if attempt["verdict"]
                            == Phase16OfficialSmokeValidationVerdict.FAILED.value
                        ),
                        None,
                    )
                    if failed_validation is not None:
                        recovered.append(
                            self._close_case_in_cursor(
                                cursor,
                                claim_id=str(claim["claim_id"]),
                                status=Phase16OfficialSmokeCaseOutcomeStatus.FAILED,
                                reason_code=failed_validation["reason_code"],
                            )
                        )
                        continue

                    blocked_validation = next(
                        (
                            attempt
                            for attempt in attempts
                            if attempt["verdict"]
                            == Phase16OfficialSmokeValidationVerdict.BLOCKED.value
                        ),
                        None,
                    )
                    if blocked_validation is not None:
                        # 已经由同一进程明确记录为未发送的 attempt 不应在恢复时升级为
                        # UNKNOWN/FAILED；恢复只补写唯一 BLOCKED outcome，仍绝不重发。
                        recovered.append(
                            self._close_case_in_cursor(
                                cursor,
                                claim_id=str(claim["claim_id"]),
                                status=Phase16OfficialSmokeCaseOutcomeStatus.BLOCKED,
                                reason_code=blocked_validation["reason_code"],
                            )
                        )
                        continue

                    unvalidated_attempt = next(
                        (attempt for attempt in attempts if attempt["verdict"] is None),
                        None,
                    )
                    if unvalidated_attempt is not None:
                        validation_digest = self._digest(
                            {
                                "attempt_id": str(unvalidated_attempt["attempt_id"]),
                                "stage": unvalidated_attempt["stage"],
                                "reason_code": "UNKNOWN_ATTEMPT_AFTER_RESTART",
                            }
                        )
                        cursor.execute(
                            """INSERT INTO phase16_official_smoke_validation_facts
                               (attempt_id, verdict, reason_code, validation_digest)
                               VALUES (%s::uuid,'FAILED','UNKNOWN_ATTEMPT_AFTER_RESTART',%s);""",
                            (str(unvalidated_attempt["attempt_id"]), validation_digest),
                        )
                        recovered.append(
                            self._close_case_in_cursor(
                                cursor,
                                claim_id=str(claim["claim_id"]),
                                status=Phase16OfficialSmokeCaseOutcomeStatus.FAILED,
                                reason_code="UNKNOWN_ATTEMPT_AFTER_RESTART",
                            )
                        )
                        continue

                    passed_stages = {
                        attempt["stage"]
                        for attempt in attempts
                        if attempt["verdict"]
                        == Phase16OfficialSmokeValidationVerdict.PASS.value
                        and attempt["receipt_attempt_id"] is not None
                    }
                    if passed_stages == {
                        Phase16OfficialSmokeDispatchStage.ANALYST.value,
                        Phase16OfficialSmokeDispatchStage.PLANNER.value,
                    }:
                        recovered.append(
                            self._close_case_in_cursor(
                                cursor,
                                claim_id=str(claim["claim_id"]),
                                status=Phase16OfficialSmokeCaseOutcomeStatus.PASS,
                                reason_code="RECOVERED_VALIDATED_PASS",
                            )
                        )
            connection.commit()
        return tuple(recovered)

    def get_case_outcome(self, *, case_id: str) -> Phase16OfficialSmokeCaseOutcome | None:
        """读取可作为正式证据消费的唯一终态；PASS 必须先通过两条 HMAC receipt 复验。"""

        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._assert_schema_contract_in_cursor(cursor)
                cursor.execute(
                    """SELECT * FROM phase16_official_smoke_case_outcomes
                       WHERE run_id=%s AND case_id=%s;""",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID, case_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                outcome = self._outcome_from_row(row)
                if outcome.status is Phase16OfficialSmokeCaseOutcomeStatus.PASS:
                    self._assert_authenticated_pass_receipts_in_cursor(
                        cursor,
                        claim_id=outcome.claim_id,
                    )
        return outcome

    def verify_case_outcome_receipts(
        self,
        *,
        case_id: str,
    ) -> Phase16OfficialSmokeCaseOutcome:
        """兼容的显式复验入口；正式 get_case_outcome 已强制执行同一认证投影。"""

        outcome = self.get_case_outcome(case_id=case_id)
        if outcome is None:
            raise Phase16OfficialSmokeLedgerError("formal case outcome is unknown")
        return outcome

    def snapshot(self) -> Phase16OfficialSmokeRunSnapshot:
        """从 PostgreSQL 事实重建预算快照，避免依赖进程内累计值。"""

        with self._connection() as connection:
            with connection.cursor() as cursor:
                self._assert_schema_contract_in_cursor(cursor)
                cursor.execute(
                    "SELECT * FROM phase16_official_smoke_runs WHERE run_id=%s;",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID,),
                )
                run_row = cursor.fetchone()
                if run_row is None:
                    raise Phase16OfficialSmokeLedgerError("formal smoke run has not been initialized")
                cursor.execute(
                    """SELECT amount_cny FROM phase16_official_smoke_historical_spend
                       WHERE run_id=%s AND source='HISTORICAL_DIRECT_MODE';""",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID,),
                )
                historical_row = cursor.fetchone()
                cursor.execute(
                    """SELECT count(*) AS slot_count,
                              COALESCE(sum(analyst_reservation_cny + planner_reservation_cny), 0)
                                  AS maximum_case_reservation_cny
                       FROM phase16_official_smoke_case_slots WHERE run_id=%s;""",
                    (PHASE16_OFFICIAL_SMOKE_RUN_ID,),
                )
                slot_totals = cursor.fetchone()
        historical = Decimal(historical_row["amount_cny"]) if historical_row is not None else Decimal("0")
        case_reservation = Decimal(slot_totals["maximum_case_reservation_cny"]) / Decimal(
            PHASE16_OFFICIAL_SMOKE_FIXED_CASE_COUNT
        )
        return Phase16OfficialSmokeRunSnapshot(
            run_id=run_row["run_id"],
            manifest_digest=run_row["manifest_digest"],
            historical_spend_cny=historical,
            fixed_case_slot_count=int(slot_totals["slot_count"]),
            case_reservation_cny=case_reservation,
            maximum_exposure_cny=historical + Decimal(slot_totals["maximum_case_reservation_cny"]),
        )

    def _connection(self):
        """统一返回字典行连接，禁止把 Settings 或凭据写入任何审计事实。"""

        return psycopg.connect(
            **self._settings.postgres_connection_kwargs,
            row_factory=dict_row,
        )

    @staticmethod
    def _assert_schema_contract_in_cursor(cursor: Any) -> None:
        """调用数据库内冻结 schema contract，拒绝旧列、缺失 FK/检查或被移除的防变更触发器。"""

        try:
            cursor.execute("SELECT phase16_official_smoke_assert_schema_contract();")
        except psycopg.Error as error:
            raise Phase16OfficialSmokeLedgerError(
                "formal smoke schema contract verification failed"
            ) from error

    @staticmethod
    def _validate_manifest(manifest: Phase16OfficialSmokeEvidenceManifest) -> None:
        """在任何 SQL 发送前检查正式 run 和固定十例预算的静态身份。"""

        if manifest.run_id != PHASE16_OFFICIAL_SMOKE_RUN_ID:
            raise Phase16OfficialSmokeLedgerError("formal ledger accepts only the frozen run ID")
        if len(manifest.case_ids) != PHASE16_OFFICIAL_SMOKE_FIXED_CASE_COUNT:
            raise Phase16OfficialSmokeLedgerError("formal ledger requires exactly ten frozen case slots")

    @staticmethod
    def _assert_run_matches_manifest(row: dict[str, Any] | None, manifest: Phase16OfficialSmokeEvidenceManifest) -> None:
        """重放初始化时逐项比对 run 绑定事实，拒绝同 ID 替换 Manifest 或 Profile。"""

        if row is None or (
            row["manifest_digest"] != manifest.manifest_digest
            or row["analyst_profile_digest"] != manifest.profile_digests["analyst"]
            or row["planner_profile_digest"] != manifest.profile_digests["planner"]
            or Decimal(row["total_budget_cny"]) != PHASE16_OFFICIAL_SMOKE_TOTAL_BUDGET_CNY
        ):
            raise Phase16OfficialSmokeLedgerError("formal smoke run identity conflicts with frozen manifest")

    @staticmethod
    def _assert_slots_match_manifest(
        rows: list[dict[str, Any]],
        manifest: Phase16OfficialSmokeEvidenceManifest,
    ) -> None:
        """验证数据库 slot 顺序、case 摘要和预约额度全都与 Manifest 精确一致。"""

        if len(rows) != PHASE16_OFFICIAL_SMOKE_FIXED_CASE_COUNT:
            raise Phase16OfficialSmokeLedgerError("formal smoke slots are incomplete or expanded")
        expected = tuple(manifest.case_ids)
        actual = tuple(row["case_id"] for row in rows)
        if actual != expected:
            raise Phase16OfficialSmokeLedgerError("formal smoke case slot order conflicts with manifest")
        for row, case_id in zip(rows, expected, strict=True):
            if (
                row["case_digest"] != manifest.case_digests[case_id]
                or Decimal(row["analyst_reservation_cny"]) != PHASE16_OFFICIAL_SMOKE_ANALYST_RESERVATION_CNY
                or Decimal(row["planner_reservation_cny"]) != PHASE16_OFFICIAL_SMOKE_PLANNER_RESERVATION_CNY
            ):
                raise Phase16OfficialSmokeLedgerError("formal smoke case slot facts conflict with manifest")

    @staticmethod
    def _slot_from_row(row: dict[str, Any]) -> Phase16OfficialSmokeCaseSlot:
        """显式还原 PostgreSQL Decimal，避免驱动类型差异改变正式预算比较。"""

        return Phase16OfficialSmokeCaseSlot(
            run_id=row["run_id"],
            slot_position=int(row["slot_position"]),
            case_id=row["case_id"],
            case_digest=row["case_digest"],
            analyst_reservation_cny=Decimal(row["analyst_reservation_cny"]),
            planner_reservation_cny=Decimal(row["planner_reservation_cny"]),
        )

    def _assert_authenticated_pass_receipts_in_cursor(
        self,
        cursor: Any,
        *,
        claim_id: str,
    ) -> None:
        """验证一个 PASS claim 恰好拥有 Analyst/Planner 两条、且由受控 Runner HMAC 的 receipt。

        这是唯一的正式 PASS 认证投影：正常 close、重启恢复和 get_case_outcome 都复用它。
        任何数据库直写行即使满足 SQL 形状、价格和 lineage，也因没有进程外 HMAC key
        而无法成为可消费的正式成功证据。
        """

        cursor.execute(
            """SELECT attempt.attempt_id, attempt.stage, attempt.profile_digest,
                       receipt.provider_response_id_digest, receipt.finish_reason, receipt.model_id,
                       receipt.response_digest, receipt.input_tokens, receipt.output_tokens,
                       receipt.total_tokens, receipt.latency_ms, receipt.input_cost_cny,
                       receipt.output_cost_cny, receipt.total_cost_cny, receipt.receipt_auth_tag,
                       validation.verdict
                 FROM phase16_official_smoke_dispatch_attempts attempt
                 JOIN phase16_official_smoke_provider_receipts receipt
                   ON receipt.attempt_id=attempt.attempt_id
                 JOIN phase16_official_smoke_validation_facts validation
                   ON validation.attempt_id=attempt.attempt_id
                WHERE attempt.claim_id=%s::uuid
                  AND validation.verdict='PASS'
             ORDER BY attempt.stage;""",
            (claim_id,),
        )
        receipt_rows = cursor.fetchall()
        expected_stages = {
            Phase16OfficialSmokeDispatchStage.ANALYST.value,
            Phase16OfficialSmokeDispatchStage.PLANNER.value,
        }
        if len(receipt_rows) != 2 or {row["stage"] for row in receipt_rows} != expected_stages:
            raise Phase16OfficialSmokeLedgerError(
                "formal PASS requires two authenticated provider receipts"
            )
        for row in receipt_rows:
            valid = self._receipt_authenticator.verify(
                receipt_auth_tag=row["receipt_auth_tag"],
                attempt_id=str(row["attempt_id"]),
                stage=Phase16OfficialSmokeDispatchStage(row["stage"]),
                profile_digest=row["profile_digest"],
                provider_response_id_digest=row["provider_response_id_digest"],
                finish_reason=row["finish_reason"],
                model_id=row["model_id"],
                response_digest=row["response_digest"],
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                total_tokens=int(row["total_tokens"]),
                latency_ms=Decimal(row["latency_ms"]),
                input_cost_cny=Decimal(row["input_cost_cny"]),
                output_cost_cny=Decimal(row["output_cost_cny"]),
                total_cost_cny=Decimal(row["total_cost_cny"]),
            )
            if not valid:
                raise Phase16OfficialSmokeLedgerError(
                    "formal provider receipt authenticity verification failed"
                )

    def _close_case_in_cursor(
        self,
        cursor: Any,
        *,
        claim_id: str,
        status: Phase16OfficialSmokeCaseOutcomeStatus,
        reason_code: str,
    ) -> Phase16OfficialSmokeCaseOutcome:
        """在已锁定 run 的事务内闭合 case，供正常路径和崩溃恢复共用同一证据规则。"""

        cursor.execute(
            """SELECT * FROM phase16_official_smoke_case_claims
               WHERE run_id=%s AND claim_id=%s::uuid FOR UPDATE;""",
            (PHASE16_OFFICIAL_SMOKE_RUN_ID, claim_id),
        )
        claim_row = cursor.fetchone()
        if claim_row is None:
            raise Phase16OfficialSmokeLedgerError("formal case outcome claim is unknown")
        cursor.execute(
            """SELECT * FROM phase16_official_smoke_case_outcomes
               WHERE run_id=%s AND case_id=%s FOR UPDATE;""",
            (PHASE16_OFFICIAL_SMOKE_RUN_ID, claim_row["case_id"]),
        )
        existing = cursor.fetchone()
        outcome_digest = self._digest(
            {
                "run_id": PHASE16_OFFICIAL_SMOKE_RUN_ID,
                "case_id": claim_row["case_id"],
                "claim_id": str(claim_row["claim_id"]),
                "status": status.value,
                "reason_code": reason_code,
            }
        )
        if existing is not None:
            loaded = self._outcome_from_row(existing)
            expected = Phase16OfficialSmokeCaseOutcome(
                run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
                case_id=claim_row["case_id"],
                claim_id=str(claim_row["claim_id"]),
                status=status,
                reason_code=reason_code,
                outcome_digest=outcome_digest,
            )
            if loaded != expected:
                raise Phase16OfficialSmokeLedgerError("formal case outcome replay conflicts with terminal fact")
            if loaded.status is Phase16OfficialSmokeCaseOutcomeStatus.PASS:
                self._assert_authenticated_pass_receipts_in_cursor(
                    cursor,
                    claim_id=claim_id,
                )
            return loaded

        cursor.execute(
            """SELECT attempt.stage, validation.verdict, receipt.attempt_id AS receipt_attempt_id
                 FROM phase16_official_smoke_dispatch_attempts attempt
            LEFT JOIN phase16_official_smoke_validation_facts validation
                   ON validation.attempt_id=attempt.attempt_id
            LEFT JOIN phase16_official_smoke_provider_receipts receipt
                   ON receipt.attempt_id=attempt.attempt_id
                WHERE attempt.claim_id=%s::uuid
             ORDER BY attempt.stage;""",
            (claim_id,),
        )
        evidence_rows = cursor.fetchall()
        if status is Phase16OfficialSmokeCaseOutcomeStatus.PASS:
            passed_stages = {
                row["stage"]
                for row in evidence_rows
                if row["verdict"] == Phase16OfficialSmokeValidationVerdict.PASS.value
                and row["receipt_attempt_id"] is not None
            }
            if passed_stages != {
                Phase16OfficialSmokeDispatchStage.ANALYST.value,
                Phase16OfficialSmokeDispatchStage.PLANNER.value,
            }:
                raise Phase16OfficialSmokeLedgerError("formal PASS requires two validated provider receipts")
            self._assert_authenticated_pass_receipts_in_cursor(cursor, claim_id=claim_id)
        elif status is Phase16OfficialSmokeCaseOutcomeStatus.BLOCKED:
            # ``begin_dispatch`` 落库的是零重试 intent，不是 Provider 已确认发送。
            # 因此只要 validation 明确为 BLOCKED 且没有 receipt，就可以闭合为本地
            # 未发送；未知 attempt、FAILED validation、缺 receipt 的 PASS 或完整 PASS
            # Planner 均仍必须拒绝，避免把任何已发送/未知外部行为伪装成 INCONCLUSIVE。
            if any(
                row["verdict"] is None
                or row["verdict"] == Phase16OfficialSmokeValidationVerdict.FAILED.value
                or (
                    row["verdict"] == Phase16OfficialSmokeValidationVerdict.PASS.value
                    and row["receipt_attempt_id"] is None
                )
                or (
                    row["verdict"] == Phase16OfficialSmokeValidationVerdict.BLOCKED.value
                    and row["receipt_attempt_id"] is not None
                )
                or (
                    row["stage"] == Phase16OfficialSmokeDispatchStage.PLANNER.value
                    and row["verdict"] == Phase16OfficialSmokeValidationVerdict.PASS.value
                )
                for row in evidence_rows
            ):
                raise Phase16OfficialSmokeLedgerError(
                    "formal BLOCKED outcome cannot conceal sent or invalid dispatch"
                )
        elif not any(
            row["verdict"] == Phase16OfficialSmokeValidationVerdict.FAILED.value
            for row in evidence_rows
        ):
            raise Phase16OfficialSmokeLedgerError(
                "formal FAILED outcome requires a failed validation fact"
            )
        cursor.execute(
            """INSERT INTO phase16_official_smoke_case_outcomes
               (run_id, case_id, claim_id, status, reason_code, outcome_digest)
               VALUES (%s,%s,%s::uuid,%s,%s,%s) RETURNING *;""",
            (
                PHASE16_OFFICIAL_SMOKE_RUN_ID,
                claim_row["case_id"],
                claim_id,
                status.value,
                reason_code,
                outcome_digest,
            ),
        )
        return self._outcome_from_row(cursor.fetchone())

    @staticmethod
    def _claim_from_row(row: dict[str, Any], *, created: bool) -> Phase16OfficialSmokeCaseClaim:
        """把唯一 claim 行还原为公开值对象，不暴露数据库连接或内部游标。"""

        return Phase16OfficialSmokeCaseClaim(
            claim_id=str(row["claim_id"]),
            run_id=row["run_id"],
            case_id=row["case_id"],
            manifest_digest=row["manifest_digest"],
            reserved_amount_cny=Decimal(row["reserved_amount_cny"]),
            created=created,
        )

    @staticmethod
    def _attempt_from_row(row: dict[str, Any]) -> Phase16OfficialSmokeDispatchAttempt:
        """把仅含身份摘要的 attempt 行还原为值对象，禁止附带模型正文。"""

        return Phase16OfficialSmokeDispatchAttempt(
            attempt_id=str(row["attempt_id"]),
            claim_id=str(row["claim_id"]),
            run_id=row["run_id"],
            case_id=row["case_id"],
            stage=Phase16OfficialSmokeDispatchStage(row["stage"]),
            profile_digest=row["profile_digest"],
            internal_request_id=row["internal_request_id"],
        )

    @staticmethod
    def _receipt_from_row(row: dict[str, Any]) -> Phase16OfficialSmokeProviderReceipt:
        """把脱敏 receipt 行还原为值对象，金额固定使用六位 Decimal 精度。"""

        return Phase16OfficialSmokeProviderReceipt(
            attempt_id=str(row["attempt_id"]),
            provider_response_id_digest=row["provider_response_id_digest"],
            finish_reason=row["finish_reason"],
            model_id=row["model_id"],
            response_digest=row["response_digest"],
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            total_tokens=int(row["total_tokens"]),
            latency_ms=Decimal(row["latency_ms"]),
            input_cost_cny=Decimal(row["input_cost_cny"]),
            output_cost_cny=Decimal(row["output_cost_cny"]),
            total_cost_cny=Decimal(row["total_cost_cny"]),
            receipt_auth_tag=row["receipt_auth_tag"],
        )

    @staticmethod
    def _validation_from_row(row: dict[str, Any]) -> Phase16OfficialSmokeValidationFact:
        """恢复单次终态验证事实；同一 attempt 不能有第二个相互矛盾的 verdict。"""

        return Phase16OfficialSmokeValidationFact(
            attempt_id=str(row["attempt_id"]),
            verdict=Phase16OfficialSmokeValidationVerdict(row["verdict"]),
            reason_code=row["reason_code"],
            validation_digest=row["validation_digest"],
        )

    @staticmethod
    def _outcome_from_row(row: dict[str, Any]) -> Phase16OfficialSmokeCaseOutcome:
        """恢复终态值对象，摘要来自安全字段的规范化哈希而不是模型响应文本。"""

        return Phase16OfficialSmokeCaseOutcome(
            run_id=row["run_id"],
            case_id=row["case_id"],
            claim_id=str(row["claim_id"]),
            status=Phase16OfficialSmokeCaseOutcomeStatus(row["status"]),
            reason_code=row["reason_code"],
            outcome_digest=row["outcome_digest"],
        )

    @staticmethod
    def _usage_cost(input_tokens: int, output_tokens: int) -> tuple[Decimal, Decimal, Decimal]:
        """使用 D-168 已冻结的 cache-miss 官方价计算 receipt 成本，避免调用方伪造金额。"""

        divisor = Decimal("1000000")
        input_cost = (Decimal(input_tokens) * FORMAL_INPUT_PRICE_CNY_PER_MILLION / divisor).quantize(
            Decimal("0.000001")
        )
        output_cost = (Decimal(output_tokens) * FORMAL_OUTPUT_PRICE_CNY_PER_MILLION / divisor).quantize(
            Decimal("0.000001")
        )
        return input_cost, output_cost, input_cost + output_cost

    @staticmethod
    def _is_digest(value: str) -> bool:
        """检查 SHA-256 十六进制摘要，拒绝把自由文本放进审计身份列。"""

        return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)

    @staticmethod
    def _is_non_blank(value: str) -> bool:
        """仅用于入库前的短生命周期网络字段检查，调用方不可依此保存自由文本。"""

        return isinstance(value, str) and bool(value) and value == value.strip() and len(value) <= 256

    @staticmethod
    def _is_reason_code(value: str) -> bool:
        """reason code 只能是服务端定义的符号，不接受用户文本、模型正文或运营建议。"""

        return isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) is not None

    @staticmethod
    def _valid_usage(input_tokens: int, output_tokens: int, total_tokens: int) -> bool:
        """Usage 必须完整且精确相加；缺失或不一致由调用方追加 FAILED validation，而非 receipt。"""

        return (
            type(input_tokens) is int
            and type(output_tokens) is int
            and type(total_tokens) is int
            and input_tokens >= 0
            and output_tokens >= 0
            and total_tokens == input_tokens + output_tokens
        )

    @staticmethod
    def _digest(value: dict[str, str]) -> str:
        """对已白名单的身份事实生成稳定 SHA-256，禁止此辅助函数接收模型正文。"""

        payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256(payload).hexdigest()

    @staticmethod
    def _text_digest(value: str) -> str:
        """只为不可信 Provider ID 生成审计关联摘要，绝不把原始字符串写入 PostgreSQL。"""

        return sha256(value.encode("utf-8")).hexdigest()


def initialize_phase16_official_smoke_ledger_schema(settings: Any) -> None:
    """执行正式账本专属 DDL；测试和迁移共用同一 UTF-8 SQL 文件。"""

    sql_path = Path(__file__).resolve().parents[2] / "docker" / "init_phase16_official_smoke_ledger.sql"
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql_path.read_text(encoding="utf-8"))
        connection.commit()
