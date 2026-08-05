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
                    # capture 增加了 artifact 路径、摘要和状态三项审计字段；下面的
                    # SQL 占位符必须与字段列表及参数元组一一对应，才能在写入账本
                    # 前阻止证据错位或静默丢失。
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
        artifact_path: str | None = None,
        artifact_digest: str | None = None,
        artifact_capture_status: str = "UNAVAILABLE",
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
        if artifact_capture_status not in {"CAPTURED", "UNAVAILABLE", "FAILED"}:
            raise Phase17HoldoutLedgerError("phase17 artifact capture status is invalid")
        if response_digest is not None and (
            len(response_digest) != 64 or any(ch not in _SHA256_HEX for ch in response_digest)
        ):
            raise Phase17HoldoutLedgerError("phase17 holdout attempt response digest must be sha256 hex")
        if artifact_digest is not None and (
            len(artifact_digest) != 64 or any(ch not in _SHA256_HEX for ch in artifact_digest)
        ):
            raise Phase17HoldoutLedgerError("phase17 artifact digest must be sha256 hex")
        if artifact_capture_status == "CAPTURED":
            if not artifact_path or artifact_digest is None or response_digest != artifact_digest:
                raise Phase17HoldoutLedgerError(
                    "phase17 captured artifact must match response digest"
                )
            expected_artifact_path = Path(
                run_id,
                case_id,
                stage,
                f"attempt-{attempt_index}.body",
            ).as_posix()
            if artifact_path != expected_artifact_path:
                raise Phase17HoldoutLedgerError(
                    "phase17 captured artifact path does not match attempt identity"
                )
        elif artifact_path is not None or artifact_digest is not None:
            raise Phase17HoldoutLedgerError(
                "phase17 uncaptured attempt cannot carry artifact identity"
            )
        if artifact_path is not None and (
            artifact_path.startswith(("/", "\\")) or ".." in Path(artifact_path).parts
        ):
            raise Phase17HoldoutLedgerError("phase17 artifact path is unsafe")
        attempt_id = hashlib.sha256(
            f"{run_id}|{case_id}|{stage}|{attempt_index}".encode("utf-8")
        ).hexdigest()
        hmac_value = self._tag(
            domain="attempt",
            payload={
                "run_id": run_id,
                "case_id": case_id,
                "stage": stage,
                "attempt_index": attempt_index,
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
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cost_cny": str(cost_cny),
                "artifact_path": artifact_path,
                "artifact_digest": artifact_digest,
                "artifact_capture_status": artifact_capture_status,
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
                            input_tokens, output_tokens, total_tokens, cost_cny,
                            artifact_path, artifact_digest, artifact_capture_status, receipt_hmac)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            attempt_id, run_id, case_id, stage, attempt_index, request_id,
                            endpoint_host, model_id, outcome, category, response_digest,
                            provider_response_id, http_status, latency_ms, attempts,
                            input_tokens, output_tokens, total_tokens, cost_cny,
                            artifact_path, artifact_digest, artifact_capture_status, hmac_value,
                        ),
                    )
                connection.commit()
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout attempt append failed"
            ) from error

    def record_phase17_safety_review(
        self,
        *,
        run_id: str,
        case_id: str,
        artifact_digest: str,
        verdict: str,
        summary: str,
        reviewer: str,
    ) -> None:
        """写入独立第三方安全审查；SQL 触发器再校验终态与 artifact 归属。"""

        if reviewer != "claude-independent-review":
            raise Phase17HoldoutLedgerError("phase17 safety reviewer identity is invalid")
        if verdict not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise Phase17HoldoutLedgerError("phase17 safety review verdict is invalid")
        if not summary.strip():
            raise Phase17HoldoutLedgerError("phase17 safety review summary is required")
        if len(artifact_digest) != 64 or any(ch not in _SHA256_HEX for ch in artifact_digest):
            raise Phase17HoldoutLedgerError("phase17 safety review artifact digest is invalid")
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase17_holdout_safety_reviews
                           (run_id, case_id, artifact_digest, verdict, summary, reviewer)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (run_id, case_id, artifact_digest, verdict, summary, reviewer),
                    )
                connection.commit()
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 safety review append failed"
            ) from error

    def phase17_attempt_artifacts(
        self,
        *,
        run_ids: tuple[str, ...],
        case_ids: tuple[str, ...],
    ) -> tuple[dict[str, object], ...]:
        """读取聚合门禁需要的逐 attempt artifact 事实，不修改账本。"""

        if not run_ids or not case_ids:
            return ()
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT run_id, case_id, stage, attempt_index,
                                  response_digest, artifact_path, artifact_digest,
                                  artifact_capture_status
                             FROM phase17_holdout_attempts
                            WHERE run_id = ANY(%s) AND case_id = ANY(%s)
                            ORDER BY run_id, case_id, stage, attempt_index""",
                        (list(run_ids), list(case_ids)),
                    )
                    return tuple(dict(row) for row in cursor.fetchall())
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 attempt artifact query failed"
            ) from error

    def phase17_safety_review_records(
        self,
        *,
        run_ids: tuple[str, ...],
        case_ids: tuple[str, ...],
    ) -> tuple[dict[str, object], ...]:
        """读取独立第三方安全审查记录，供 --aggregate 做确定性判定。"""

        if not run_ids or not case_ids:
            return ()
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT run_id, case_id, artifact_digest, verdict,
                                  summary, reviewer
                             FROM phase17_holdout_safety_reviews
                            WHERE run_id = ANY(%s) AND case_id = ANY(%s)
                            ORDER BY run_id, case_id""",
                        (list(run_ids), list(case_ids)),
                    )
                    return tuple(dict(row) for row in cursor.fetchall())
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 safety review query failed"
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

    def phase17_run_ledger_state(self, *, run_id: str) -> dict[str, object]:
        """异常终态化查询（codex 第十八轮 P1-4）：run 存在性、终态与已入账成本。

        未终态 run 的 attempt 成本累计就是实际已发生费用；CLI 据此决定
        settle（>0）或 release（=0），保证真实调用中途异常也有统一终态。
        """

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT campaign_id FROM phase17_holdout_runs WHERE run_id=%s""",
                        (run_id,),
                    )
                    run_row = cursor.fetchone()
                    if run_row is None:
                        return {"run_exists": False}
                    cursor.execute(
                        """SELECT 1 FROM phase17_holdout_run_results WHERE run_id=%s""",
                        (run_id,),
                    )
                    terminal = cursor.fetchone() is not None
                    cursor.execute(
                        """SELECT COALESCE(SUM(cost_cny), 0) AS cost
                             FROM phase17_holdout_attempts WHERE run_id=%s""",
                        (run_id,),
                    )
                    cost_row = cursor.fetchone()
                    return {
                        "run_exists": True,
                        "terminal": terminal,
                        "attempt_cost_cny": Decimal(cost_row["cost"]),
                        "campaign_id": run_row["campaign_id"],
                    }
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout run ledger state query failed"
            ) from error

    def phase17_batch_run_report(
        self, *, contract_digest: str, batch_index: int
    ) -> dict[str, object] | None:
        """读取某 contract 某 batch 的终态 run 事实（--aggregate 输入）。

        返回 None 表示该 batch 尚无终态 run；返回 dict 含全链身份（campaign
        的 candidate / dataset digest）、终态、case 级结果与总成本。case 级
        reason_code 用于重算 critical safety 计数（判定规则确定性可复算）。
        """

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT campaign_id, candidate_digest, dataset_manifest_digest
                             FROM phase17_holdout_campaigns
                            WHERE contract_digest=%s AND batch_index=%s""",
                        (contract_digest, batch_index),
                    )
                    campaign_row = cursor.fetchone()
                    if campaign_row is None:
                        return None
                    cursor.execute(
                        """SELECT run_id FROM phase17_holdout_runs
                            WHERE campaign_id=%s""",
                        (campaign_row["campaign_id"],),
                    )
                    run_rows = cursor.fetchall()
                    for run_row in run_rows:
                        cursor.execute(
                            """SELECT status, reason_code, evaluation_digest
                                 FROM phase17_holdout_run_results
                                WHERE run_id=%s""",
                            (run_row["run_id"],),
                        )
                        terminal = cursor.fetchone()
                        if terminal is None:
                            continue
                        cursor.execute(
                            """SELECT case_id, outcome, reason_code,
                                      receipt_count, cost_cny
                                 FROM phase17_holdout_case_results
                                WHERE run_id=%s""",
                            (run_row["run_id"],),
                        )
                        case_rows = cursor.fetchall()
                        return {
                            "run_id": run_row["run_id"],
                            "campaign_id": campaign_row["campaign_id"],
                            "candidate_digest": campaign_row["candidate_digest"],
                            "dataset_manifest_digest": campaign_row["dataset_manifest_digest"],
                            "status": terminal["status"],
                            "reason_code": terminal["reason_code"],
                            "cases": [
                                {
                                    "case_id": case_row["case_id"],
                                    "outcome": case_row["outcome"],
                                    "reason_code": case_row["reason_code"],
                                    "receipt_count": case_row["receipt_count"],
                                    "cost_cny": Decimal(case_row["cost_cny"]),
                                }
                                for case_row in case_rows
                            ],
                        }
                    return None
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout batch run report query failed"
            ) from error

    def record_phase17_qualification(
        self,
        *,
        qualification_id: str,
        run1_id: str,
        run2_id: str,
        contract_digest: str,
        candidate_digest: str,
        dataset_manifest_digest: str,
        status: str,
        reason_code: str,
        total_pass: int,
        total_cases: int,
        pass_min: int,
        critical_safety_failures: int,
        evaluation_digest: str,
    ) -> None:
        """27/30 聚合结论只入账一次（codex 第十八轮 P1-4 持久化）。

        两 run 必须分属 batch 1 / batch 2 且都已终态；身份与判定由 CLI 在
        内存聚合时校验，本方法做 SQL 层最后防线（run 存在且终态、digest
        形状），UNIQUE(run1_id, run2_id) 兜底重复记录。
        """

        if status not in {"QUALIFIED", "FAILED", "BLOCKED"}:
            raise Phase17HoldoutLedgerError("phase17 holdout qualification status is invalid")
        if not qualification_id or not run1_id or not run2_id:
            raise Phase17HoldoutLedgerError("phase17 holdout qualification ids are required")
        for digest in (contract_digest, candidate_digest, dataset_manifest_digest, evaluation_digest):
            if len(digest) != 64 or any(ch not in _SHA256_HEX for ch in digest):
                raise Phase17HoldoutLedgerError(
                    "phase17 holdout qualification digest must be sha256 hex"
                )
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    for run_id, expected_batch in ((run1_id, 1), (run2_id, 2)):
                        cursor.execute(
                            """SELECT cam.batch_index FROM phase17_holdout_runs run
                                JOIN phase17_holdout_campaigns cam
                                  ON cam.campaign_id = run.campaign_id
                               WHERE run.run_id=%s""",
                            (run_id,),
                        )
                        row = cursor.fetchone()
                        if row is None or row["batch_index"] != expected_batch:
                            raise Phase17HoldoutLedgerError(
                                "phase17 holdout qualification run batch identity mismatch"
                            )
                        cursor.execute(
                            """SELECT 1 FROM phase17_holdout_run_results
                                WHERE run_id=%s""",
                            (run_id,),
                        )
                        if cursor.fetchone() is None:
                            raise Phase17HoldoutLedgerError(
                                "phase17 holdout qualification requires terminal runs"
                            )
                    cursor.execute(
                        """INSERT INTO phase17_holdout_qualifications
                           (qualification_id, run1_id, run2_id, contract_digest,
                            candidate_digest, dataset_manifest_digest, status,
                            reason_code, total_pass, total_cases, pass_min,
                            critical_safety_failures, evaluation_digest)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            qualification_id, run1_id, run2_id, contract_digest,
                            candidate_digest, dataset_manifest_digest, status,
                            reason_code, total_pass, total_cases, pass_min,
                            critical_safety_failures, evaluation_digest,
                        ),
                    )
                connection.commit()
        except Phase17HoldoutLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout qualification record failed"
            ) from error

    def phase17_qualification_records(
        self, *, contract_digest: str
    ) -> tuple[dict[str, object], ...]:
        """某 contract 已入账的聚合结论（--aggregate 幂等提示用）。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT qualification_id, run1_id, run2_id, status,
                                  reason_code, total_pass, total_cases, pass_min
                             FROM phase17_holdout_qualifications
                            WHERE contract_digest=%s
                            ORDER BY created_at""",
                        (contract_digest,),
                    )
                    return tuple(
                        {
                            "qualification_id": row["qualification_id"],
                            "run1_id": row["run1_id"],
                            "run2_id": row["run2_id"],
                            "status": row["status"],
                            "reason_code": row["reason_code"],
                            "total_pass": row["total_pass"],
                            "total_cases": row["total_cases"],
                            "pass_min": row["pass_min"],
                        }
                        for row in cursor.fetchall()
                    )
        except psycopg.Error as error:
            raise Phase17HoldoutLedgerError(
                "phase17 holdout qualification records query failed"
            ) from error


def phase17_holdout_schema_ready(settings: Any) -> bool:
    """检查 phase17 表族是否已建（CLI 准入；建表走统一 migration 入口）。

    codex 第十八轮 P1-4：CLI 不再直接执行 ``initialize_phase17_holdout_schema``
    （避免绕过 ``scripts/run_db_migrations.py`` 的双路径），未就绪时提示
    先跑 migration。
    """

    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT 1 FROM information_schema.tables
                    WHERE table_schema=current_schema()
                      AND table_name='phase17_holdout_attempts'"""
            )
            return cursor.fetchone() is not None


def initialize_phase17_holdout_schema(settings: Any) -> None:
    """执行 phase17 独立 DDL；测试与部署走同一份 append-only 约束。"""

    path = Path(__file__).resolve().parents[2] / "docker" / "init_phase17_holdout_ledger.sql"
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(path.read_text(encoding="utf-8"))
        connection.commit()
