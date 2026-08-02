"""Phase 17 holdout 独立执行账本（append-only 事件记账，与 v2 表族物理隔离）。

预算池是纯事件记账（``phase17_holdout_budget_events``，表族内无 UPDATE）：
- RESERVE：campaign 建立时在 contract 行锁内预留 reservation_cny；
- SETTLE：run 结算实际成本（usage 未知按最坏情况以 reservation 全额入账）；
- RELEASE：未产生成本的 campaign 释放预留。

不变式（contract 行锁内强制 + SQL 后置）：
    reserved + settled <= forward_budget_remaining_cny

identity：campaign_id 复用 v2 的 canonical 渲染（kind=HOLDOUT + 声明组合 + batch），
确保同一声明组合 + 同一 batch 只能有一个 campaign（防刷分）；contract_digest
必须是已批准注册的 Phase 17 契约，v2/v3 policy digest 在此永远找不到行。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.decision_support.phase16_qualification_ledger import (
    Phase16QualificationLedgerError,
    QualificationCampaignKind,
    qualification_campaign_id,
)

_SHA256_HEX = frozenset("0123456789abcdef")


def canonical_json_sha256(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class Phase17HoldoutLedgerError(ValueError):
    """Phase 17 账本事务失败；任何失败都必须在模型调用前 fail-closed。"""


@dataclass(frozen=True)
class Phase17HoldoutCampaign:
    """phase17 holdout campaign 声明；campaign_id 必须是声明组合的 canonical 渲染。"""

    campaign_id: str
    contract_digest: str
    batch_index: int
    candidate_digest: str
    dataset_manifest_digest: str
    reservation_cny: Decimal
    declared_model_id: str
    declared_reasoning_effort: str | None
    declared_endpoint_hosts: tuple[str, ...]


class PostgresPhase17HoldoutLedger:
    """隐藏事务与预算池细节的 phase17 账本深模块（独立表族）。"""

    def __init__(self, settings: Any, *, hmac_key: bytes) -> None:
        if len(hmac_key) < 32:
            raise ValueError("phase17 holdout ledger HMAC key must contain at least 256 bits")
        self._settings = settings
        self._hmac_key = hmac_key

    def _connection(self):
        return psycopg.connect(
            **self._settings.postgres_connection_kwargs,
            row_factory=dict_row,
        )

    def _tag(self, *, domain: str, payload: dict[str, object]) -> str:
        digest = canonical_json_sha256(payload)
        message = f"phase17-holdout-v1:{domain}:{digest}".encode("utf-8")
        return hmac.new(self._hmac_key, message, "sha256").hexdigest()

    def ensure_phase17_contract(self, contract: Any) -> None:
        """插入或锁定 contract 行；digest 之外的任何身份漂移均失败。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase17_holdout_contracts
                           (contract_digest, contract_id, project_budget_cny,
                            retrospective_budget_actual_cny, forward_budget_remaining_cny)
                           VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (contract_digest) DO NOTHING""",
                        (
                            contract.contract_digest,
                            contract.contract_id,
                            contract.project_budget_cny,
                            contract.retrospective_budget_actual_cny,
                            contract.forward_budget_remaining_cny,
                        ),
                    )
                    cursor.execute(
                        """SELECT contract_id, project_budget_cny,
                                  retrospective_budget_actual_cny, forward_budget_remaining_cny
                             FROM phase17_holdout_contracts
                            WHERE contract_digest=%s FOR UPDATE""",
                        (contract.contract_digest,),
                    )
                    row = cursor.fetchone()
                    expected = (
                        contract.contract_id,
                        contract.project_budget_cny,
                        contract.retrospective_budget_actual_cny,
                        contract.forward_budget_remaining_cny,
                    )
                    actual = None if row is None else (
                        row["contract_id"],
                        Decimal(row["project_budget_cny"]),
                        Decimal(row["retrospective_budget_actual_cny"]),
                        Decimal(row["forward_budget_remaining_cny"]),
                    )
                    if actual != expected:
                        raise Phase17HoldoutLedgerError(
                            "phase17 holdout contract identity conflicts"
                        )
                connection.commit()
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout contract initialization failed"
            ) from error

    def ensure_phase17_campaign(self, campaign: Phase17HoldoutCampaign) -> None:
        """建立一次性 campaign 并在 contract 行锁内预留预算池金额。

        预算池隔离：v2/v3 policy digest 在此表族中永远没有 contract 行 →
        fail-closed；phase17 预算池由本表族独立维护，无法被历史 campaign 借用。
        """

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    canonical = qualification_campaign_id(
                        kind=QualificationCampaignKind.HOLDOUT,
                        candidate_digest=campaign.candidate_digest,
                        declared_model_id=campaign.declared_model_id,
                        declared_reasoning_effort=campaign.declared_reasoning_effort,
                        declared_endpoint_hosts=campaign.declared_endpoint_hosts,
                        batch_index=campaign.batch_index,
                    )
                    if canonical != campaign.campaign_id:
                        raise Phase17HoldoutLedgerError(
                            "phase17 holdout campaign id does not match declared identity"
                        )
                    # contract 行锁：同一 digest 的 reservation/settlement 串行化点。
                    cursor.execute(
                        """SELECT contract_id, project_budget_cny,
                                  retrospective_budget_actual_cny, forward_budget_remaining_cny
                             FROM phase17_holdout_contracts
                            WHERE contract_digest=%s FOR UPDATE""",
                        (campaign.contract_digest,),
                    )
                    contract_row = cursor.fetchone()
                    if contract_row is None:
                        raise Phase17HoldoutLedgerError(
                            "phase17 holdout contract is not registered"
                        )
                    cursor.execute(
                        """SELECT campaign_id FROM phase17_holdout_campaigns
                            WHERE contract_digest=%s AND batch_index=%s""",
                        (campaign.contract_digest, campaign.batch_index),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        # 幂等：同 contract + 同 batch 已建 → 复验 identity，不重复预留。
                        if existing["campaign_id"] != campaign.campaign_id:
                            raise Phase17HoldoutLedgerError(
                                "phase17 holdout campaign identity conflicts"
                            )
                        cursor.execute(
                            """SELECT contract_digest, batch_index, candidate_digest,
                                      dataset_manifest_digest, reservation_cny,
                                      declared_model_id, declared_reasoning_effort,
                                      declared_endpoint_hosts
                                 FROM phase17_holdout_campaigns
                                WHERE campaign_id=%s FOR UPDATE""",
                            (campaign.campaign_id,),
                        )
                        row = cursor.fetchone()
                        expected = (
                            campaign.contract_digest,
                            campaign.batch_index,
                            campaign.candidate_digest,
                            campaign.dataset_manifest_digest,
                            campaign.reservation_cny,
                            campaign.declared_model_id,
                            campaign.declared_reasoning_effort,
                            ",".join(campaign.declared_endpoint_hosts),
                        )
                        actual = None if row is None else (
                            row["contract_digest"], row["batch_index"], row["candidate_digest"],
                            row["dataset_manifest_digest"], Decimal(row["reservation_cny"]),
                            row["declared_model_id"], row["declared_reasoning_effort"],
                            row["declared_endpoint_hosts"],
                        )
                        if actual != expected:
                            raise Phase17HoldoutLedgerError(
                                "phase17 holdout campaign identity conflicts"
                            )
                        connection.commit()
                        return
                    # 预算池检查：reserved + reservation <= forward（行锁内串行）。
                    reserved, settled = self._budget_commitments(
                        cursor, campaign.contract_digest
                    )
                    forward = Decimal(contract_row["forward_budget_remaining_cny"])
                    if reserved + settled + campaign.reservation_cny > forward:
                        raise Phase17HoldoutLedgerError(
                            "phase17 holdout budget pool is exhausted"
                        )
                    cursor.execute(
                        """INSERT INTO phase17_holdout_campaigns
                           (campaign_id, contract_digest, batch_index, candidate_digest,
                            dataset_manifest_digest, reservation_cny, declared_model_id,
                            declared_reasoning_effort, declared_endpoint_hosts)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            campaign.campaign_id,
                            campaign.contract_digest,
                            campaign.batch_index,
                            campaign.candidate_digest,
                            campaign.dataset_manifest_digest,
                            campaign.reservation_cny,
                            campaign.declared_model_id,
                            campaign.declared_reasoning_effort,
                            ",".join(campaign.declared_endpoint_hosts),
                        ),
                    )
                    cursor.execute(
                        """INSERT INTO phase17_holdout_budget_events
                           (contract_digest, campaign_id, event_type, amount_cny)
                           VALUES (%s,%s,'RESERVE',%s)""",
                        (campaign.contract_digest, campaign.campaign_id, campaign.reservation_cny),
                    )
                connection.commit()
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout campaign initialization failed"
            ) from error

    @staticmethod
    def _budget_commitments(cursor, contract_digest: str) -> tuple[Decimal, Decimal]:
        """事件聚合：reserved = Σ RESERVE - Σ RELEASE；settled = Σ SETTLE。"""

        cursor.execute(
            """SELECT COALESCE(SUM(
                   CASE WHEN event_type='RELEASE' THEN -amount_cny
                        WHEN event_type='RESERVE' THEN amount_cny ELSE 0 END
               ), 0) AS reserved,
               COALESCE(SUM(amount_cny) FILTER (WHERE event_type='SETTLE'), 0) AS settled
                 FROM phase17_holdout_budget_events
                WHERE contract_digest=%s""",
            (contract_digest,),
        )
        row = cursor.fetchone()
        return Decimal(row["reserved"]), Decimal(row["settled"])

    def budget_pool_state(self, contract_digest: str) -> dict[str, Decimal]:
        """预算池权威快照；可用余额 = forward - reserved - settled。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT project_budget_cny, retrospective_budget_actual_cny,
                                  forward_budget_remaining_cny
                             FROM phase17_holdout_contracts
                            WHERE contract_digest=%s""",
                        (contract_digest,),
                    )
                    contract_row = cursor.fetchone()
                    if contract_row is None:
                        raise Phase17HoldoutLedgerError(
                            "phase17 holdout contract is not registered"
                        )
                    reserved, settled = self._budget_commitments(cursor, contract_digest)
                forward = Decimal(contract_row["forward_budget_remaining_cny"])
                return {
                    "project_budget_cny": Decimal(contract_row["project_budget_cny"]),
                    "retrospective_budget_actual_cny": Decimal(
                        contract_row["retrospective_budget_actual_cny"]
                    ),
                    "forward_budget_remaining_cny": forward,
                    "reserved_cny": reserved,
                    "settled_cny": settled,
                    "available_cny": forward - reserved - settled,
                }
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout budget pool query failed"
            ) from error

    def begin_phase17_run(
        self, *, run_id: str, campaign_id: str, case_ids: tuple[str, ...]
    ) -> None:
        """run slot 集合一次性冻结；非空、唯一、无重复。"""

        if not case_ids or len(set(case_ids)) != len(case_ids):
            raise Phase17HoldoutLedgerError("phase17 holdout run slots must be unique and non-empty")
        for case_id in case_ids:
            if not case_id:
                raise Phase17HoldoutLedgerError("phase17 holdout run slot case ids are required")
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase17_holdout_runs (run_id, campaign_id, case_ids)
                           VALUES (%s,%s,%s)""",
                        (run_id, campaign_id, list(case_ids)),
                    )
                    cursor.execute(
                        """SELECT campaign_id, case_ids FROM phase17_holdout_runs
                            WHERE run_id=%s FOR UPDATE""",
                        (run_id,),
                    )
                    row = cursor.fetchone()
                    actual = None if row is None else (row["campaign_id"], tuple(row["case_ids"]))
                    if actual != (campaign_id, case_ids):
                        raise Phase17HoldoutLedgerError(
                            "phase17 holdout run slot identity conflicts"
                        )
                connection.commit()
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError("phase17 holdout run initialization failed") from error

    def record_phase17_case_result(
        self,
        *,
        run_id: str,
        case_id: str,
        input_digest: str,
        outcome: str,
        reason_code: str,
        receipt_count: int,
        cost_cny: Decimal,
    ) -> None:
        """case 结论只追加一次；SQL 触发器兜底 slot 归属与 run 未终态。"""

        if outcome not in {"PASS", "FAILED", "BLOCKED"}:
            raise Phase17HoldoutLedgerError("phase17 holdout case outcome is invalid")
        if len(input_digest) != 64 or any(ch not in _SHA256_HEX for ch in input_digest):
            raise Phase17HoldoutLedgerError("phase17 holdout case input digest must be sha256 hex")
        if receipt_count < 0 or cost_cny < 0:
            raise Phase17HoldoutLedgerError("phase17 holdout case result counters are invalid")
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase17_holdout_case_results
                           (run_id, case_id, input_digest, outcome, reason_code,
                            receipt_count, cost_cny)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (run_id, case_id, input_digest, outcome, reason_code, receipt_count, cost_cny),
                    )
                connection.commit()
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout case result append failed"
            ) from error

    def record_phase17_attempt(
        self,
        *,
        run_id: str,
        case_id: str,
        stage: str,
        attempt_index: int,
        request_id: str,
        endpoint_host: str,
        model_id: str,
        outcome: str,
        category: str | None,
        response_digest: str | None,
        provider_response_id: str | None,
        http_status: int | None,
        latency_ms: Decimal,
        attempts: int,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        cost_cny: Decimal,
    ) -> None:
        """逐 attempt 证据只追加一次（codex 第十七轮 P0-3）。

        receipt_hmac 由账本用 ``_tag`` 内部计算（HMAC key 不外传），覆盖
        请求/响应摘要与成本事实；审计时可用同一 key 重算校验，attempt 事实
        与 case 级聚合（``receipt_count``/``cost_cny``）一一对账。
        """

        if stage not in {"ANALYST", "PLANNER"}:
            raise Phase17HoldoutLedgerError("phase17 holdout attempt stage is invalid")
        if outcome not in {"PASS", "FAILED"}:
            raise Phase17HoldoutLedgerError("phase17 holdout attempt outcome is invalid")
        if attempt_index < 1 or attempts < 1:
            raise Phase17HoldoutLedgerError("phase17 holdout attempt counters are invalid")
        if cost_cny < 0 or latency_ms < 0:
            raise Phase17HoldoutLedgerError("phase17 holdout attempt cost/latency are invalid")
        if response_digest is not None and (
            len(response_digest) != 64 or any(ch not in _SHA256_HEX for ch in response_digest)
        ):
            raise Phase17HoldoutLedgerError("phase17 holdout attempt response digest must be sha256 hex")
        attempt_id = hashlib.sha256(
            f"{run_id}|{case_id}|{stage}|{attempt_index}".encode("utf-8")
        ).hexdigest()
        hmac_value = self._tag(
            domain="attempt",
            payload={
                "request_id": request_id,
                "endpoint_host": endpoint_host,
                "model_id": model_id,
                "outcome": outcome,
                "category": category,
                "response_digest": response_digest,
                "provider_response_id": provider_response_id,
                "http_status": http_status,
                "latency_ms": str(latency_ms),
                "attempts": attempts,
                "cost_cny": str(cost_cny),
            },
        )
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase17_holdout_attempts
                           (attempt_id, run_id, case_id, stage, attempt_index, request_id,
                            endpoint_host, model_id, outcome, category, response_digest,
                            provider_response_id, http_status, latency_ms, attempts,
                            input_tokens, output_tokens, total_tokens, cost_cny, receipt_hmac)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            attempt_id, run_id, case_id, stage, attempt_index, request_id,
                            endpoint_host, model_id, outcome, category, response_digest,
                            provider_response_id, http_status, latency_ms, attempts,
                            input_tokens, output_tokens, total_tokens, cost_cny, hmac_value,
                        ),
                    )
                connection.commit()
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout attempt append failed"
            ) from error

    def close_phase17_run(
        self,
        *,
        run_id: str,
        status: str,
        reason_code: str,
        payload: dict[str, object],
    ) -> None:
        """run 终态一次性写入（run_results 唯一一行）。"""

        if status not in {"PASS", "FAILED", "BLOCKED"}:
            raise Phase17HoldoutLedgerError("phase17 holdout run status is invalid")
        evaluation_digest = canonical_json_sha256(payload)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase17_holdout_run_results
                           (run_id, status, reason_code, evaluation_digest)
                           VALUES (%s,%s,%s,%s)""",
                        (run_id, status, reason_code, evaluation_digest),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout run terminalization failed"
            ) from error

    def settle_phase17_campaign(self, *, campaign_id: str, actual_cny: Decimal) -> None:
        """run 结束后按实际成本结算；usage 未知时调用方以 reservation 全额入账。

        结算 = 预留转实际：同一事务内 RELEASE(reservation) + SETTLE(actual)，
        使每个 campaign 在池中至多占用一次（未结算占 reservation、已结算占
        actual，与 v2 committed 口径一致）；available = forward - reserved - settled。
        """

        if actual_cny <= 0:
            raise Phase17HoldoutLedgerError("phase17 holdout settlement amount must be positive")
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT contract_digest, reservation_cny FROM phase17_holdout_campaigns
                            WHERE campaign_id=%s FOR UPDATE""",
                        (campaign_id,),
                    )
                    campaign_row = cursor.fetchone()
                    if campaign_row is None:
                        raise Phase17HoldoutLedgerError("phase17 holdout campaign is unknown")
                    contract_digest = campaign_row["contract_digest"]
                    reservation_cny = Decimal(campaign_row["reservation_cny"])
                    cursor.execute(
                        """SELECT forward_budget_remaining_cny
                             FROM phase17_holdout_contracts
                            WHERE contract_digest=%s FOR UPDATE""",
                        (contract_digest,),
                    )
                    contract_row = cursor.fetchone()
                    cursor.execute(
                        """SELECT 1 FROM phase17_holdout_budget_events
                            WHERE campaign_id=%s AND event_type='SETTLE'""",
                        (campaign_id,),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase17HoldoutLedgerError(
                            "phase17 holdout campaign is already settled"
                        )
                    reserved, settled = self._budget_commitments(cursor, contract_digest)
                    forward = Decimal(contract_row["forward_budget_remaining_cny"])
                    # 结算后该 campaign 不再占 reservation，改占 actual。
                    if reserved - reservation_cny + settled + actual_cny > forward:
                        raise Phase17HoldoutLedgerError(
                            "phase17 holdout settlement exceeds budget pool"
                        )
                    cursor.execute(
                        """INSERT INTO phase17_holdout_budget_events
                           (contract_digest, campaign_id, event_type, amount_cny)
                           VALUES (%s,%s,'RELEASE',%s)""",
                        (contract_digest, campaign_id, reservation_cny),
                    )
                    cursor.execute(
                        """INSERT INTO phase17_holdout_budget_events
                           (contract_digest, campaign_id, event_type, amount_cny)
                           VALUES (%s,%s,'SETTLE',%s)""",
                        (contract_digest, campaign_id, actual_cny),
                    )
                connection.commit()
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError("phase17 holdout settlement failed") from error

    def release_phase17_campaign(self, *, campaign_id: str) -> None:
        """未产生成本的 campaign 释放预留（预算不足中止等场景）。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT contract_digest, reservation_cny FROM phase17_holdout_campaigns
                            WHERE campaign_id=%s FOR UPDATE""",
                        (campaign_id,),
                    )
                    campaign_row = cursor.fetchone()
                    if campaign_row is None:
                        raise Phase17HoldoutLedgerError("phase17 holdout campaign is unknown")
                    reservation_cny = Decimal(campaign_row["reservation_cny"])
                    cursor.execute(
                        """SELECT 1 FROM phase17_holdout_budget_events
                            WHERE campaign_id=%s AND event_type='SETTLE'""",
                        (campaign_id,),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase17HoldoutLedgerError(
                            "phase17 holdout settled campaign cannot be released"
                        )
                    cursor.execute(
                        """SELECT 1 FROM phase17_holdout_budget_events
                            WHERE campaign_id=%s AND event_type='RELEASE'""",
                        (campaign_id,),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase17HoldoutLedgerError(
                            "phase17 holdout campaign is already released"
                        )
                    cursor.execute(
                        """INSERT INTO phase17_holdout_budget_events
                           (contract_digest, campaign_id, event_type, amount_cny)
                           VALUES (%s,%s,'RELEASE',%s)""",
                        (campaign_row["contract_digest"], campaign_id, reservation_cny),
                    )
                connection.commit()
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError("phase17 holdout campaign release failed") from error


def initialize_phase17_holdout_schema(settings: Any) -> None:
    """执行 phase17 独立 DDL；测试与部署走同一份 append-only 约束。"""

    path = Path(__file__).resolve().parents[2] / "docker" / "init_phase17_holdout_ledger.sql"
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(path.read_text(encoding="utf-8"))
        connection.commit()
