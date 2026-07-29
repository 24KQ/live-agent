"""Phase 16 V5 受控 E2E campaign 的 PostgreSQL append-only 审计账本。

V5 与 V1 至 V4 物理隔离：它有新的 campaign、run、slot、attempt、receipt、validation 和
outcome 表。账本只保存摘要、枚举、计量值和 HMAC，不保存 API Key、Prompt、模型正文、
思维链、原始 Provider ID 或任何经营建议。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from hashlib import sha256
import hmac
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from src.specialist_runtime.model_port import ModelSuccess
from src.specialist_runtime.models import canonical_json_sha256


class Phase16V5CampaignLedgerError(RuntimeError):
    """V5 账本的稳定错误，不向调用方泄漏 SQL、Provider 或模型正文。"""


class Phase16V5RunKind(StrEnum):
    """V5 只允许一个校准 run 和一个依赖校准的正式 run。"""

    CALIBRATION = "CALIBRATION"
    FORMAL = "FORMAL"


class Phase16V5RunStatus(StrEnum):
    """run 终态按发送状态表达，任何终态均禁止二次执行。"""

    PASS = "PASS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class Phase16V5CaseOutcomeStatus(StrEnum):
    """单 slot 的 append-only 终态，与 run 终态分开以保留失败位置。"""

    PASS = "PASS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class Phase16V5DispatchStage(StrEnum):
    """双 Agent 必须固定为 Analyst 先、Planner 后的两个唯一 stage。"""

    ANALYST = "ANALYST"
    PLANNER = "PLANNER"


class Phase16V5ValidationVerdict(StrEnum):
    """receipt 之后的受控验证结论，不保存自由异常或模型文本。"""

    PASS = "PASS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Phase16V5CaseClaim:
    """已经线性化的 case 领取事实；只有它可创建两个 stage 的 dispatch intent。"""

    claim_id: str
    run_id: str
    case_id: str


@dataclass(frozen=True)
class Phase16V5DispatchAttempt:
    """网络调用前写入的唯一 intent，不能被“发送失败”解释为可重试。"""

    attempt_id: str
    run_id: str
    claim_id: str
    stage: Phase16V5DispatchStage
    internal_request_id: str
    reservation_cny: Decimal


@dataclass(frozen=True)
class Phase16V5RecoveredAttempt:
    """进程重启时对未验证发送 intent 的不可变失败封口。

    ``begin_dispatch`` 在网络调用前追加，进程若随后崩溃，无法可靠判断 Provider 是否已经
    收包。该值对象只公开内部 attempt 身份、所属 run/case 和稳定失败码；它不包含异常正文、
    Prompt、模型输出或 Provider 原始 ID。
    """

    attempt_id: str
    run_id: str
    case_id: str
    status: Phase16V5CaseOutcomeStatus
    reason_code: str


@dataclass(frozen=True)
class Phase16V5RecoveredCase:
    """进程重启时对已领取但未闭合 case 的不可变终态事实。

    该事实处理两类不能重发的中断：尚未生成 dispatch intent 的本地阻断，以及已有完整
    stage validation、但尚未来得及写 case/run outcome 的不完整 E2E。它不保存模型正文、
    Provider 标识或异常原文，也绝不篡改已存在的 validation 结论。
    """

    run_id: str
    case_id: str
    status: Phase16V5CaseOutcomeStatus
    reason_code: str


class PostgresPhase16V5CampaignLedger:
    """为 V5 维护行锁预算、收据认证和零重试终态的 PostgreSQL 实现。"""

    def __init__(self, settings: Any, *, hmac_key: bytes) -> None:
        """仅持有可信连接配置和进程外签名密钥，二者都不会写入审计行。"""

        if len(hmac_key) < 32:
            raise ValueError("V5 receipt HMAC key must contain at least 256 bits")
        self._settings = settings
        self._hmac_key = hmac_key

    def _connection(self):
        """每项账本操作使用独立事务，依赖 PostgreSQL 行锁跨进程序列化预算与 slot。"""

        return psycopg.connect(
            **self._settings.postgres_connection_kwargs,
            row_factory=dict_row,
        )

    def ensure_campaign(self, manifest: Any) -> None:
        """创建或精确复验 V5 campaign，不允许同名 campaign 被新 Manifest 替换。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_v5_campaigns
                           (campaign_id, manifest_digest, total_budget_cny, input_cny_per_million,
                            output_cny_per_million, analyst_profile_digest, planner_profile_digest,
                            model_id, thinking_mode)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'disabled')
                           ON CONFLICT (campaign_id) DO NOTHING""",
                        (
                            manifest.campaign_id,
                            manifest.manifest_digest,
                            manifest.total_budget_cny,
                            manifest.input_cny_per_million,
                            manifest.output_cny_per_million,
                            manifest.profile_digests["analyst"],
                            manifest.profile_digests["planner"],
                            manifest.model_id,
                        ),
                    )
                    cursor.execute(
                        """SELECT manifest_digest, total_budget_cny, input_cny_per_million,
                                  output_cny_per_million, analyst_profile_digest, planner_profile_digest,
                                  model_id, thinking_mode
                             FROM phase16_v5_campaigns WHERE campaign_id=%s FOR UPDATE""",
                        (manifest.campaign_id,),
                    )
                    row = cursor.fetchone()
                    expected = (
                        row is not None
                        and row["manifest_digest"] == manifest.manifest_digest
                        and Decimal(row["total_budget_cny"]) == manifest.total_budget_cny
                        and Decimal(row["input_cny_per_million"]) == manifest.input_cny_per_million
                        and Decimal(row["output_cny_per_million"]) == manifest.output_cny_per_million
                        and row["analyst_profile_digest"] == manifest.profile_digests["analyst"]
                        and row["planner_profile_digest"] == manifest.profile_digests["planner"]
                        and row["model_id"] == manifest.model_id
                        and row["thinking_mode"] == "disabled"
                    )
                    if not expected:
                        raise Phase16V5CampaignLedgerError("V5 campaign identity conflicts")
                connection.commit()
        except Phase16V5CampaignLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 campaign initialization failed") from error

    def begin_run(self, *, run_id: str, run_kind: Phase16V5RunKind, manifest: Any) -> None:
        """创建唯一 run 与冻结 slot；FORMAL 只能在认证的 CALIBRATION PASS 后开始。"""

        case_slots = self._run_slots(run_kind=run_kind, manifest=manifest)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT 1 FROM phase16_v5_campaigns WHERE campaign_id=%s FOR UPDATE",
                        (manifest.campaign_id,),
                    )
                    if cursor.fetchone() is None:
                        raise Phase16V5CampaignLedgerError("V5 campaign is not initialized")
                    if run_kind is Phase16V5RunKind.FORMAL:
                        cursor.execute(
                            """SELECT outcome.status FROM phase16_v5_runs run
                                 JOIN phase16_v5_run_outcomes outcome ON outcome.run_id=run.run_id
                                WHERE run.run_id=%s AND run.run_kind='CALIBRATION'""",
                            ("phase16-v5-calibration-001",),
                        )
                        calibration = cursor.fetchone()
                        if calibration is None or calibration["status"] != Phase16V5RunStatus.PASS.value:
                            raise Phase16V5CampaignLedgerError("V5 formal run requires calibration PASS")
                    cursor.execute(
                        """INSERT INTO phase16_v5_runs (run_id, campaign_id, run_kind)
                           VALUES (%s,%s,%s) ON CONFLICT (run_id) DO NOTHING""",
                        (run_id, manifest.campaign_id, run_kind.value),
                    )
                    cursor.execute(
                        """SELECT campaign_id, run_kind FROM phase16_v5_runs
                             WHERE run_id=%s FOR UPDATE""",
                        (run_id,),
                    )
                    run = cursor.fetchone()
                    if run is None or run["campaign_id"] != manifest.campaign_id or run["run_kind"] != run_kind.value:
                        raise Phase16V5CampaignLedgerError("V5 run identity conflicts")
                    # 唯一 run 一旦已有终态，就绝不能借由 ``INSERT ... DO NOTHING`` 重新打开。
                    # 这条查询同时处理进程重启后的重复 CLI 调用，避免它被误解为合法重试。
                    cursor.execute(
                        "SELECT 1 FROM phase16_v5_run_outcomes WHERE run_id=%s",
                        (run_id,),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase16V5CampaignLedgerError("V5 run is already terminal")
                    for position, (case_id, case_digest) in enumerate(case_slots, start=1):
                        cursor.execute(
                            """INSERT INTO phase16_v5_case_slots
                               (run_id, slot_position, case_id, case_digest)
                               VALUES (%s,%s,%s,%s)
                               ON CONFLICT (run_id, case_id) DO NOTHING""",
                            (run_id, position, case_id, case_digest),
                        )
                    cursor.execute(
                        """SELECT slot_position, case_id, case_digest FROM phase16_v5_case_slots
                             WHERE run_id=%s ORDER BY slot_position""",
                        (run_id,),
                    )
                    rows = cursor.fetchall()
                    if tuple((row["case_id"], row["case_digest"]) for row in rows) != case_slots:
                        raise Phase16V5CampaignLedgerError("V5 run slot identity conflicts")
                connection.commit()
        except Phase16V5CampaignLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 run initialization failed") from error

    def calibration_passed(self) -> bool:
        """读取数据库事实而非进程缓存，正式 run 据此决定能否创建第一个 slot。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT outcome.status FROM phase16_v5_runs run
                             JOIN phase16_v5_run_outcomes outcome ON outcome.run_id=run.run_id
                            WHERE run.run_id=%s AND run.run_kind='CALIBRATION'""",
                        ("phase16-v5-calibration-001",),
                    )
                    row = cursor.fetchone()
                    return row is not None and row["status"] == Phase16V5RunStatus.PASS.value
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 calibration status is unavailable") from error

    def claim_case(self, *, run_id: str, case_id: str, case_digest: str) -> Phase16V5CaseClaim:
        """锁定 run 和 slot 后写入唯一 claim，已存在 claim 或终态均禁止再次发送。"""

        claim_id = str(uuid5(NAMESPACE_URL, f"phase16-v5:{run_id}:{case_id}"))
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    self._lock_live_run(cursor, run_id)
                    cursor.execute(
                        """SELECT case_digest FROM phase16_v5_case_slots
                             WHERE run_id=%s AND case_id=%s FOR UPDATE""",
                        (run_id, case_id),
                    )
                    slot = cursor.fetchone()
                    if slot is None or slot["case_digest"] != case_digest:
                        raise Phase16V5CampaignLedgerError("V5 case slot identity is invalid")
                    cursor.execute(
                        """SELECT 1 FROM phase16_v5_case_outcomes
                             WHERE run_id=%s AND case_id=%s""",
                        (run_id, case_id),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase16V5CampaignLedgerError("V5 case is already terminal")
                    cursor.execute(
                        """INSERT INTO phase16_v5_case_claims (claim_id, run_id, case_id)
                           VALUES (%s::uuid,%s,%s) ON CONFLICT (run_id, case_id) DO NOTHING""",
                        (claim_id, run_id, case_id),
                    )
                    cursor.execute(
                        """SELECT claim_id FROM phase16_v5_case_claims
                             WHERE run_id=%s AND case_id=%s FOR UPDATE""",
                        (run_id, case_id),
                    )
                    row = cursor.fetchone()
                    if row is None or str(row["claim_id"]) != claim_id:
                        raise Phase16V5CampaignLedgerError("V5 case claim conflicts")
                connection.commit()
        except Phase16V5CampaignLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 case claim failed") from error
        return Phase16V5CaseClaim(claim_id=claim_id, run_id=run_id, case_id=case_id)

    def begin_dispatch(
        self,
        *,
        run_id: str,
        claim_id: str,
        stage: Phase16V5DispatchStage,
        profile_digest: str,
        internal_request_id: str,
        reservation_cny: Decimal,
    ) -> Phase16V5DispatchAttempt:
        """在网络前持久化唯一 stage intent，并以 campaign 行锁拒绝总预算越界。"""

        self._require_uuid(claim_id, "claim_id")
        self._require_uuid(internal_request_id, "internal_request_id")
        if reservation_cny <= 0 or reservation_cny > Decimal("0.030000"):
            raise Phase16V5CampaignLedgerError("V5 stage reservation is invalid")
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    campaign_id = self._lock_live_run(cursor, run_id)
                    cursor.execute(
                        """SELECT case_id FROM phase16_v5_case_claims
                             WHERE claim_id=%s::uuid AND run_id=%s FOR UPDATE""",
                        (claim_id, run_id),
                    )
                    claim = cursor.fetchone()
                    if claim is None:
                        raise Phase16V5CampaignLedgerError("V5 dispatch claim is unknown")
                    if stage is Phase16V5DispatchStage.PLANNER:
                        cursor.execute(
                            """SELECT validation.verdict, receipt.receipt_complete
                                 FROM phase16_v5_dispatch_attempts attempt
                                 LEFT JOIN phase16_v5_validation_facts validation ON validation.attempt_id=attempt.attempt_id
                                 LEFT JOIN phase16_v5_provider_receipts receipt ON receipt.attempt_id=attempt.attempt_id
                                WHERE attempt.claim_id=%s::uuid AND attempt.stage='ANALYST'""",
                            (claim_id,),
                        )
                        analyst = cursor.fetchone()
                        if analyst is None or analyst["verdict"] != Phase16V5ValidationVerdict.PASS.value or not analyst["receipt_complete"]:
                            raise Phase16V5CampaignLedgerError("V5 planner requires validated analyst")
                    cursor.execute(
                        """SELECT total_budget_cny, analyst_profile_digest, planner_profile_digest
                              FROM phase16_v5_campaigns
                               WHERE campaign_id=%s FOR UPDATE""",
                        (campaign_id,),
                    )
                    budget = cursor.fetchone()
                    # attempt 的 Profile 摘要不仅是展示字段，更是 campaign 身份的一部分。
                    # 账本在网络前把 stage 与冻结摘要绑定，防止绕过 Runner 的调用方用任意
                    # Profile 或 Prompt 取得一个表面合法的 dispatch intent。
                    expected_profile_digest = (
                        budget["analyst_profile_digest"]
                        if stage is Phase16V5DispatchStage.ANALYST
                        else budget["planner_profile_digest"]
                    )
                    if profile_digest != expected_profile_digest:
                        raise Phase16V5CampaignLedgerError(
                            "V5 dispatch profile identity conflicts"
                        )
                    cursor.execute(
                        """SELECT COALESCE(sum(reservation_cny), 0) AS exposure
                             FROM phase16_v5_dispatch_attempts attempt
                             JOIN phase16_v5_runs run ON run.run_id=attempt.run_id
                            WHERE run.campaign_id=%s""",
                        (campaign_id,),
                    )
                    exposure = Decimal(cursor.fetchone()["exposure"])
                    if exposure + reservation_cny > Decimal(budget["total_budget_cny"]):
                        raise Phase16V5CampaignLedgerError("V5 campaign budget exposure exceeded")
                    attempt_id = str(uuid5(NAMESPACE_URL, f"phase16-v5:{run_id}:{claim['case_id']}:{stage.value}"))
                    cursor.execute(
                        """INSERT INTO phase16_v5_dispatch_attempts
                           (attempt_id, run_id, claim_id, stage, profile_digest, internal_request_id, reservation_cny)
                           VALUES (%s::uuid,%s,%s::uuid,%s,%s,%s::uuid,%s)""",
                        (attempt_id, run_id, claim_id, stage.value, profile_digest, internal_request_id, reservation_cny),
                    )
                connection.commit()
        except Phase16V5CampaignLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 dispatch intent failed") from error
        return Phase16V5DispatchAttempt(attempt_id, run_id, claim_id, stage, internal_request_id, reservation_cny)

    def append_receipt(
        self,
        *,
        attempt_id: str,
        success: ModelSuccess,
    ) -> bool:
        """追加脱敏 Provider receipt；价格只从冻结 campaign 读取，调用方不能注入成本事实。"""

        self._require_uuid(attempt_id, "attempt_id")
        provider_digest = None if not success.provider_response_id else sha256(success.provider_response_id.encode("utf-8")).hexdigest()
        usage = success.usage
        latency = success.latency_ms.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT attempt.internal_request_id, attempt.reservation_cny,
                                  campaign.input_cny_per_million, campaign.output_cny_per_million
                             FROM phase16_v5_dispatch_attempts attempt
                             JOIN phase16_v5_runs run ON run.run_id=attempt.run_id
                             JOIN phase16_v5_campaigns campaign ON campaign.campaign_id=run.campaign_id
                            WHERE attempt.attempt_id=%s::uuid FOR UPDATE OF attempt, run, campaign""",
                        (attempt_id,),
                    )
                    attempt = cursor.fetchone()
                    if attempt is None or str(attempt["internal_request_id"]) != success.request_id:
                        raise Phase16V5CampaignLedgerError("V5 receipt identity conflicts")
                    actual_cost = None
                    if usage is not None:
                        # 使用 campaign 行中由 Manifest 冻结并经 SQL CHECK 约束的官方单价；
                        # 任何调用方都无法用零价或自定义单价伪造可接受的实际成本。
                        actual_cost = self._cost(
                            usage.input_tokens,
                            usage.output_tokens,
                            Decimal(attempt["input_cny_per_million"]),
                            Decimal(attempt["output_cny_per_million"]),
                        )
                    complete = (
                        provider_digest is not None
                        and success.finish_reason == "stop"
                        and success.model_id == "deepseek-v4-pro"
                        and usage is not None
                        and actual_cost is not None
                        and actual_cost <= Decimal(attempt["reservation_cny"])
                    )
                    payload = {
                        "attempt_id": attempt_id,
                        "provider_response_id_digest": provider_digest,
                        "finish_reason": success.finish_reason,
                        "model_id": success.model_id,
                        "response_digest": success.response_digest,
                        "input_tokens": None if usage is None else usage.input_tokens,
                        "output_tokens": None if usage is None else usage.output_tokens,
                        "total_tokens": None if usage is None else usage.total_tokens,
                        "latency_ms": str(latency),
                        "actual_cost_cny": None if actual_cost is None else str(actual_cost),
                        "output_digest": canonical_json_sha256(success.output),
                        "receipt_complete": complete,
                    }
                    cursor.execute(
                        """INSERT INTO phase16_v5_provider_receipts
                           (attempt_id, provider_response_id_digest, finish_reason, model_id, response_digest,
                            input_tokens, output_tokens, total_tokens, latency_ms, actual_cost_cny,
                            output_digest, receipt_complete, receipt_auth_tag)
                           VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            attempt_id, provider_digest, success.finish_reason, success.model_id,
                            success.response_digest, None if usage is None else usage.input_tokens,
                            None if usage is None else usage.output_tokens,
                            None if usage is None else usage.total_tokens, latency,
                            actual_cost, canonical_json_sha256(success.output), complete,
                            self._sign("receipt", payload),
                        ),
                    )
                connection.commit()
        except Phase16V5CampaignLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 receipt append failed") from error
        return complete

    def append_validation(
        self,
        *,
        attempt_id: str,
        verdict: Phase16V5ValidationVerdict,
        reason_code: str,
        validation_digest: str,
    ) -> None:
        """为 attempt 追加唯一验证事实，reason code 与 digest 都是封闭安全投影。"""

        self._require_uuid(attempt_id, "attempt_id")
        self._require_reason(reason_code)
        if len(validation_digest) != 64:
            raise ValueError("V5 validation digest is invalid")
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_v5_validation_facts
                           (attempt_id, verdict, reason_code, validation_digest, validation_auth_tag)
                           VALUES (%s::uuid,%s,%s,%s,%s)""",
                        (attempt_id, verdict.value, reason_code, validation_digest, self._sign("validation", {"attempt_id": attempt_id, "verdict": verdict.value, "reason_code": reason_code, "validation_digest": validation_digest})),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 validation append failed") from error

    def close_case(self, *, claim_id: str, status: Phase16V5CaseOutcomeStatus, reason_code: str) -> None:
        """关闭一个 case；PASS 先在 Python 复验 HMAC，SQL trigger 再复验阶段和 receipt 形状。"""

        self._require_uuid(claim_id, "claim_id")
        self._require_reason(reason_code)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT claim.run_id, claim.case_id FROM phase16_v5_case_claims claim
                             WHERE claim.claim_id=%s::uuid FOR UPDATE""",
                        (claim_id,),
                    )
                    claim = cursor.fetchone()
                    if claim is None:
                        raise Phase16V5CampaignLedgerError("V5 case claim is unknown")
                    if status is Phase16V5CaseOutcomeStatus.PASS:
                        self._assert_authenticated_pass(cursor, claim_id=claim_id)
                    digest = canonical_json_sha256({"claim_id": claim_id, "status": status.value, "reason_code": reason_code})
                    cursor.execute(
                        """INSERT INTO phase16_v5_case_outcomes
                           (run_id, case_id, claim_id, status, reason_code, outcome_digest)
                           VALUES (%s,%s,%s::uuid,%s,%s,%s)""",
                        (claim["run_id"], claim["case_id"], claim_id, status.value, reason_code, digest),
                    )
                connection.commit()
        except Phase16V5CampaignLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 case close failed") from error

    def close_run(self, *, run_id: str, status: Phase16V5RunStatus, reason_code: str) -> None:
        """写入唯一 run 终态，正式 PASS 必须具有十个双阶段 PASS case，校准 PASS 必须为一个。"""

        self._require_reason(reason_code)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT run_kind FROM phase16_v5_runs WHERE run_id=%s FOR UPDATE",
                        (run_id,),
                    )
                    run = cursor.fetchone()
                    if run is None:
                        raise Phase16V5CampaignLedgerError("V5 run is unknown")
                    cursor.execute(
                        """SELECT count(*) AS slot_count,
                                  count(outcome.case_id) FILTER (WHERE outcome.status='PASS') AS passed_count
                             FROM phase16_v5_case_slots slot
                             LEFT JOIN phase16_v5_case_outcomes outcome
                               ON outcome.run_id=slot.run_id AND outcome.case_id=slot.case_id
                            WHERE slot.run_id=%s""",
                        (run_id,),
                    )
                    counts = cursor.fetchone()
                    expected = 1 if run["run_kind"] == Phase16V5RunKind.CALIBRATION.value else 10
                    if status is Phase16V5RunStatus.PASS and (
                        int(counts["slot_count"]) != expected or int(counts["passed_count"]) != expected
                    ):
                        raise Phase16V5CampaignLedgerError("V5 PASS run has incomplete case outcomes")
                    digest = canonical_json_sha256({"run_id": run_id, "status": status.value, "reason_code": reason_code})
                    cursor.execute(
                        """INSERT INTO phase16_v5_run_outcomes (run_id, status, reason_code, outcome_digest)
                           VALUES (%s,%s,%s,%s)""",
                        (run_id, status.value, reason_code, digest),
                    )
                connection.commit()
        except Phase16V5CampaignLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 run close failed") from error

    def recover_open_attempts(self) -> tuple[Phase16V5RecoveredAttempt, ...]:
        """把崩溃遗留的未验证 dispatch intent 收口为未知已发送失败。

        intent 已先于 HTTP 写入，因此没有 validation 的 attempt 不能被下次进程当作“从未
        发送”后重试。恢复事务在同一个 run 行锁内依次追加 FAILED validation、FAILED case
        outcome 和 FAILED run outcome；这让后续 CLI 只能报告历史失败而无法再次联网。
        """

        recovered: list[Phase16V5RecoveredAttempt] = []
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    # V5 Runner 串行发送，正常情况下每个 live run 最多只有一个未验证
                    # intent。``SKIP LOCKED`` 让两个恢复进程不会同时为同一 attempt 写终态。
                    cursor.execute(
                        """SELECT attempt.attempt_id, attempt.run_id, attempt.claim_id, claim.case_id
                             FROM phase16_v5_dispatch_attempts attempt
                             JOIN phase16_v5_case_claims claim ON claim.claim_id=attempt.claim_id
                             JOIN phase16_v5_runs run ON run.run_id=attempt.run_id
                             LEFT JOIN phase16_v5_validation_facts validation
                               ON validation.attempt_id=attempt.attempt_id
                             LEFT JOIN phase16_v5_case_outcomes case_outcome
                               ON case_outcome.run_id=attempt.run_id
                              AND case_outcome.case_id=claim.case_id
                             LEFT JOIN phase16_v5_run_outcomes run_outcome
                               ON run_outcome.run_id=attempt.run_id
                            WHERE validation.attempt_id IS NULL
                              AND case_outcome.claim_id IS NULL
                              AND run_outcome.run_id IS NULL
                         ORDER BY attempt.created_at, attempt.attempt_id
                              FOR UPDATE OF attempt, claim, run SKIP LOCKED"""
                    )
                    rows = cursor.fetchall()
                    for row in rows:
                        attempt_id = str(row["attempt_id"])
                        run_id = row["run_id"]
                        claim_id = str(row["claim_id"])
                        reason_code = "UNKNOWN_ATTEMPT_AFTER_RESTART"
                        validation_digest = canonical_json_sha256(
                            {
                                "attempt_id": attempt_id,
                                "verdict": Phase16V5ValidationVerdict.FAILED.value,
                                "reason_code": reason_code,
                            }
                        )
                        cursor.execute(
                            """INSERT INTO phase16_v5_validation_facts
                               (attempt_id, verdict, reason_code, validation_digest, validation_auth_tag)
                               VALUES (%s::uuid,%s,%s,%s,%s)""",
                            (
                                attempt_id,
                                Phase16V5ValidationVerdict.FAILED.value,
                                reason_code,
                                validation_digest,
                                self._sign(
                                    "validation",
                                    {
                                        "attempt_id": attempt_id,
                                        "verdict": Phase16V5ValidationVerdict.FAILED.value,
                                        "reason_code": reason_code,
                                        "validation_digest": validation_digest,
                                    },
                                ),
                            ),
                        )
                        case_digest = canonical_json_sha256(
                            {
                                "claim_id": claim_id,
                                "status": Phase16V5CaseOutcomeStatus.FAILED.value,
                                "reason_code": reason_code,
                            }
                        )
                        cursor.execute(
                            """INSERT INTO phase16_v5_case_outcomes
                               (run_id, case_id, claim_id, status, reason_code, outcome_digest)
                               VALUES (%s,%s,%s::uuid,%s,%s,%s)""",
                            (
                                run_id,
                                row["case_id"],
                                claim_id,
                                Phase16V5CaseOutcomeStatus.FAILED.value,
                                reason_code,
                                case_digest,
                            ),
                        )
                        run_digest = canonical_json_sha256(
                            {
                                "run_id": run_id,
                                "status": Phase16V5RunStatus.FAILED.value,
                                "reason_code": reason_code,
                            }
                        )
                        cursor.execute(
                            """INSERT INTO phase16_v5_run_outcomes
                               (run_id, status, reason_code, outcome_digest)
                               VALUES (%s,%s,%s,%s)""",
                            (
                                run_id,
                                Phase16V5RunStatus.FAILED.value,
                                reason_code,
                                run_digest,
                            ),
                        )
                        recovered.append(
                            Phase16V5RecoveredAttempt(
                                attempt_id=attempt_id,
                                run_id=run_id,
                                case_id=row["case_id"],
                                status=Phase16V5CaseOutcomeStatus.FAILED,
                                reason_code=reason_code,
                            )
                        )
                connection.commit()
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 open attempt recovery failed") from error
        return tuple(recovered)

    def recover_incomplete_cases(self) -> tuple[Phase16V5RecoveredCase, ...]:
        """关闭所有不再可继续的已领取 case，防止崩溃后留下可被误解为可重试的 run。

        本方法必须在 ``recover_open_attempts`` 之后调用。前者已将没有 validation 的网络前
        intent 封口为失败；这里仅处理没有 dispatch intent 的未发送 claim，或所有已有 intent
        均已有 validation、但 case/run outcome 尚未追加的状态。后者可能包含成功的单阶段
        事实，所以用独立 recovery fact 解释 E2E 未闭合，而不能篡改原 validation。
        """

        recovered: list[Phase16V5RecoveredCase] = []
        processed_run_ids: set[str] = set()
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT claim.run_id, claim.claim_id, claim.case_id,
                                  EXISTS(
                                      SELECT 1 FROM phase16_v5_dispatch_attempts attempt
                                       WHERE attempt.claim_id=claim.claim_id
                                  ) AS has_attempt
                             FROM phase16_v5_case_claims claim
                             JOIN phase16_v5_runs run ON run.run_id=claim.run_id
                             LEFT JOIN phase16_v5_case_outcomes case_outcome
                               ON case_outcome.claim_id=claim.claim_id
                             LEFT JOIN phase16_v5_run_outcomes run_outcome
                               ON run_outcome.run_id=claim.run_id
                            WHERE case_outcome.claim_id IS NULL
                              AND run_outcome.run_id IS NULL
                              AND NOT EXISTS(
                                  SELECT 1
                                    FROM phase16_v5_dispatch_attempts attempt
                                    LEFT JOIN phase16_v5_validation_facts validation
                                      ON validation.attempt_id=attempt.attempt_id
                                   WHERE attempt.claim_id=claim.claim_id
                                     AND validation.attempt_id IS NULL
                              )
                         ORDER BY claim.run_id, claim.created_at, claim.claim_id
                              FOR UPDATE OF claim, run SKIP LOCKED"""
                    )
                    rows = cursor.fetchall()
                    for row in rows:
                        run_id = row["run_id"]
                        if run_id in processed_run_ids:
                            # 一个 run 被设计为串行执行；即使异常客户端留下多个 claim，也只能
                            # 由首个恢复事实关闭该 run，不能在同一事务中继续伪造更多 case 终态。
                            continue
                        claim_id = str(row["claim_id"])
                        status = (
                            Phase16V5CaseOutcomeStatus.FAILED
                            if row["has_attempt"]
                            else Phase16V5CaseOutcomeStatus.BLOCKED
                        )
                        reason_code = (
                            "INCOMPLETE_VALIDATED_CASE_AFTER_RESTART"
                            if row["has_attempt"]
                            else "UNSENT_CLAIM_AFTER_RESTART"
                        )
                        recovery_payload = {
                            "run_id": run_id,
                            "claim_id": claim_id,
                            "status": status.value,
                            "reason_code": reason_code,
                        }
                        recovery_digest = canonical_json_sha256(recovery_payload)
                        cursor.execute(
                            """INSERT INTO phase16_v5_recovery_facts
                               (run_id, claim_id, status, reason_code, recovery_digest, recovery_auth_tag)
                               VALUES (%s,%s::uuid,%s,%s,%s,%s)""",
                            (
                                run_id,
                                claim_id,
                                status.value,
                                reason_code,
                                recovery_digest,
                                self._sign(
                                    "recovery",
                                    {**recovery_payload, "recovery_digest": recovery_digest},
                                ),
                            ),
                        )
                        case_digest = canonical_json_sha256(
                            {
                                "claim_id": claim_id,
                                "status": status.value,
                                "reason_code": reason_code,
                            }
                        )
                        cursor.execute(
                            """INSERT INTO phase16_v5_case_outcomes
                               (run_id, case_id, claim_id, status, reason_code, outcome_digest)
                               VALUES (%s,%s,%s::uuid,%s,%s,%s)""",
                            (
                                run_id,
                                row["case_id"],
                                claim_id,
                                status.value,
                                reason_code,
                                case_digest,
                            ),
                        )
                        run_status = (
                            Phase16V5RunStatus.FAILED
                            if status is Phase16V5CaseOutcomeStatus.FAILED
                            else Phase16V5RunStatus.BLOCKED
                        )
                        run_digest = canonical_json_sha256(
                            {
                                "run_id": run_id,
                                "status": run_status.value,
                                "reason_code": reason_code,
                            }
                        )
                        cursor.execute(
                            """INSERT INTO phase16_v5_run_outcomes
                               (run_id, status, reason_code, outcome_digest)
                               VALUES (%s,%s,%s,%s)""",
                            (run_id, run_status.value, reason_code, run_digest),
                        )
                        processed_run_ids.add(run_id)
                        recovered.append(
                            Phase16V5RecoveredCase(
                                run_id=run_id,
                                case_id=row["case_id"],
                                status=status,
                                reason_code=reason_code,
                            )
                        )
                connection.commit()
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 incomplete case recovery failed") from error
        return tuple(recovered)

    def report_summary(self, *, run_id: str) -> dict[str, Any]:
        """读取可公开渲染的计量与摘要事实；不返回原始 provider ID、Prompt 或模型输出。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT run.run_id, run.run_kind, outcome.status, outcome.reason_code,
                                  count(attempt.attempt_id) AS attempt_count,
                                  COALESCE(sum(receipt.actual_cost_cny), 0) AS actual_cost_cny
                             FROM phase16_v5_runs run
                             LEFT JOIN phase16_v5_run_outcomes outcome ON outcome.run_id=run.run_id
                             LEFT JOIN phase16_v5_dispatch_attempts attempt ON attempt.run_id=run.run_id
                             LEFT JOIN phase16_v5_provider_receipts receipt ON receipt.attempt_id=attempt.attempt_id
                            WHERE run.run_id=%s
                         GROUP BY run.run_id, run.run_kind, outcome.status, outcome.reason_code""",
                        (run_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise Phase16V5CampaignLedgerError("V5 report run is unknown")
                    cursor.execute(
                        """SELECT case_id, status, reason_code, outcome_digest
                             FROM phase16_v5_case_outcomes WHERE run_id=%s ORDER BY case_id""",
                        (run_id,),
                    )
                    cases = cursor.fetchall()
        except Phase16V5CampaignLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16V5CampaignLedgerError("V5 report summary is unavailable") from error
        return {
            "run_id": row["run_id"],
            "run_kind": row["run_kind"],
            "status": row["status"],
            "reason_code": row["reason_code"],
            "attempt_count": int(row["attempt_count"]),
            "actual_cost_cny": str(Decimal(row["actual_cost_cny"]).quantize(Decimal("0.000001"))),
            "case_outcomes": [
                {"case_id": item["case_id"], "status": item["status"], "reason_code": item["reason_code"], "outcome_digest": item["outcome_digest"]}
                for item in cases
            ],
        }

    def _lock_live_run(self, cursor: Any, run_id: str) -> str:
        """锁定 run 及其 campaign；终态后任何新 claim 或 attempt 都被 fail-closed 拒绝。"""

        cursor.execute(
            """SELECT run.campaign_id,
                       EXISTS(SELECT 1 FROM phase16_v5_run_outcomes outcome WHERE outcome.run_id=run.run_id)
                           AS terminal
                 FROM phase16_v5_runs run
                 JOIN phase16_v5_campaigns campaign ON campaign.campaign_id=run.campaign_id
                WHERE run.run_id=%s FOR UPDATE OF run, campaign""",
            (run_id,),
        )
        row = cursor.fetchone()
        if row is None or row["terminal"]:
            raise Phase16V5CampaignLedgerError("V5 run is terminal or unavailable")
        return row["campaign_id"]

    def _assert_authenticated_pass(self, cursor: Any, *, claim_id: str) -> None:
        """只有两条完整 receipt 与 HMAC 均正确，Python 才会请求 SQL 写入 PASS outcome。"""

        cursor.execute(
            """SELECT attempt.attempt_id, attempt.stage, receipt.provider_response_id_digest,
                       receipt.finish_reason, receipt.model_id, receipt.response_digest,
                       receipt.input_tokens, receipt.output_tokens, receipt.total_tokens,
                       receipt.latency_ms, receipt.actual_cost_cny, receipt.output_digest,
                       receipt.receipt_complete, receipt.receipt_auth_tag, validation.verdict
                  FROM phase16_v5_dispatch_attempts attempt
                  JOIN phase16_v5_provider_receipts receipt ON receipt.attempt_id=attempt.attempt_id
                  JOIN phase16_v5_validation_facts validation ON validation.attempt_id=attempt.attempt_id
                 WHERE attempt.claim_id=%s::uuid ORDER BY attempt.stage""",
            (claim_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 2 or {row["stage"] for row in rows} != {"ANALYST", "PLANNER"}:
            raise Phase16V5CampaignLedgerError("V5 PASS requires two stage receipts")
        for row in rows:
            if row["verdict"] != Phase16V5ValidationVerdict.PASS.value or not row["receipt_complete"]:
                raise Phase16V5CampaignLedgerError("V5 PASS requires complete validation")
            payload = {
                "attempt_id": str(row["attempt_id"]),
                "provider_response_id_digest": row["provider_response_id_digest"],
                "finish_reason": row["finish_reason"],
                "model_id": row["model_id"],
                "response_digest": row["response_digest"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "total_tokens": row["total_tokens"],
                "latency_ms": str(Decimal(row["latency_ms"]).quantize(Decimal("0.001"))),
                "actual_cost_cny": str(Decimal(row["actual_cost_cny"]).quantize(Decimal("0.000001"))),
                "output_digest": row["output_digest"],
                "receipt_complete": True,
            }
            if not hmac.compare_digest(row["receipt_auth_tag"], self._sign("receipt", payload)):
                raise Phase16V5CampaignLedgerError("V5 provider receipt HMAC is invalid")

    @staticmethod
    def _run_slots(*, run_kind: Phase16V5RunKind, manifest: Any) -> tuple[tuple[str, str], ...]:
        """从 Manifest 显式映射校准或正式 slot，调用方不能通过参数扩展 case 集合。"""

        if run_kind is Phase16V5RunKind.CALIBRATION:
            # 校准的 case 摘要来自独立合成输入，不能借用正式十例的任一摘要；否则一次
            # 校准请求会提前暴露正式 slot，并让后续 10/10 结论带有预演歧义。
            return ((manifest.calibration_case_id, manifest.calibration_case_digest),)
        return tuple((case_id, manifest.formal_case_digests[case_id]) for case_id in manifest.formal_case_ids)

    @staticmethod
    def _cost(input_tokens: int, output_tokens: int, input_price: Decimal, output_price: Decimal) -> Decimal:
        """与 Runner 相同地按 Provider usage 和官方 Pro 价格结算到六位小数。"""

        raw = (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / Decimal("1000000")
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    def _sign(self, domain: str, payload: dict[str, Any]) -> str:
        """使用 V5 独立 domain 认证脱敏事实，不能跨用 V1-V4 的 receipt 标签。"""

        message = f"phase16-v5-controlled-e2e:{domain}:{canonical_json_sha256(payload)}".encode("utf-8")
        return hmac.new(self._hmac_key, message, "sha256").hexdigest()

    @staticmethod
    def _require_uuid(value: str, name: str) -> None:
        """所有内部数据库身份必须是 UUID，避免自由文本被写入审计键列。"""

        try:
            UUID(value)
        except (AttributeError, ValueError) as error:
            raise ValueError(f"{name} must be a UUID") from error

    @staticmethod
    def _require_reason(value: str) -> None:
        """对外 reason code 只能是稳定大写枚举形态，禁止拼接异常正文。"""

        if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in value):
            raise ValueError("V5 reason code is invalid")


def initialize_phase16_v5_controlled_e2e_schema(settings: Any) -> None:
    """执行 V5 专属 DDL；统一迁移和 PostgreSQL 契约测试都调用同一份 SQL。"""

    path = Path(__file__).resolve().parents[2] / "docker" / "init_phase16_v5_controlled_e2e.sql"
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(path.read_text(encoding="utf-8"))
        connection.commit()
