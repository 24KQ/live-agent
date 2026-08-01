"""Phase 16 qualification 的独立 PostgreSQL append-only 审计账本。

该账本只记录新版 policy/corpus/candidate/campaign 的身份、经验证的历史摘要、密封 release
事实、可复算指标和最终资格结论。它不读取或修改 V1–V8 的 ledger row，不保存模型正文、
Prompt、密钥、密文或经营建议。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import hmac
from pathlib import Path
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.decision_support.phase16_qualification import (
    HoldoutReleaseState,
    Phase16QualificationManifest,
    Phase16QualificationPolicy,
)
from src.specialist_runtime.models import canonical_json_sha256
from src.specialist_runtime.profiles import (
    FORMAL_ENDPOINT_HOSTS,
    FORMAL_MODEL_IDS,
    FORMAL_REASONING_EFFORTS,
    normalize_endpoint_host,
)


_HASH_PATTERN = r"^[0-9a-f]{64}$"
_REASON_CODE_PATTERN = r"^[A-Z][A-Z0-9_]*$"


class Phase16QualificationLedgerError(RuntimeError):
    """账本稳定失败码，不向调用方泄漏 SQL、密钥或模型正文。"""


class QualificationCampaignKind(StrEnum):
    """candidate 只能按预注册用途进入三种独立 campaign。"""

    DEVELOPMENT = "DEVELOPMENT"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"


def qualification_campaign_id(
    *,
    kind: QualificationCampaignKind,
    candidate_digest: str,
    declared_model_id: str,
    declared_reasoning_effort: str | None,
    declared_endpoint_hosts: tuple[str, ...],
    batch_index: int = 1,
) -> str:
    """canonical campaign 身份 = kind + candidate digest + 运行时声明组合。

    矩阵配置拍板语义：白名单内切模型/强度/渠道零成本——同一 digest 下不同声明
    组合是不同 campaign 身份，各占一次 dev/validation 名额；同一组合重复声明
    命中同一 campaign_id，由 UNIQUE(campaign_id) 与终态检查拒绝。batch_index
    只对 HOLDOUT 有意义（batch 1/2），统一纳入组合串保持身份唯一。
    """

    combo = "|".join(
        (
            str(batch_index),
            declared_model_id,
            declared_reasoning_effort or "",
            ",".join(declared_endpoint_hosts),
        )
    )
    suffix = sha256(combo.encode("utf-8")).hexdigest()[:16]
    return f"phase16-{kind.value.lower()}-{candidate_digest[:16]}-{suffix}"


class QualificationRunStatus(StrEnum):
    """终态只写一次；FAILED/BLOCKED 不能被后续 result 改写。"""

    PASS = "PASS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class SourceEvidenceIntegrity(StrEnum):
    """历史观察的认证范围；未验证或不完整事实绝不能支撑高信任 PASS。"""

    AUTHENTICATED = "AUTHENTICATED"
    UNVERIFIABLE = "UNVERIFIABLE"
    INCOMPLETE = "INCOMPLETE"


class QualificationCandidate(BaseModel):
    """冻结 candidate 身份；Prompt/Profile 或 Adapter 改动都必须生成新 digest。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(..., min_length=1)
    policy_digest: str = Field(..., pattern=_HASH_PATTERN)
    model_id: str = Field(default="gpt-5.6-luna", min_length=1)
    endpoint_host: str = Field(default="synapse-ai.uk", min_length=1)
    analyst_profile_digest: str = Field(..., pattern=_HASH_PATTERN)
    planner_profile_digest: str = Field(..., pattern=_HASH_PATTERN)
    adapter_digest: str = Field(..., pattern=_HASH_PATTERN)
    candidate_digest: str | None = Field(default=None, pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def _validate_identity(self) -> "QualificationCandidate":
        # candidate digest 自校验，model_id / endpoint_host 已改为运行时配置
        if self.candidate_digest is not None:
            expected = canonical_json_sha256(
                self.model_dump(mode="json", exclude={"candidate_digest"})
            )
            if self.candidate_digest != expected:
                raise ValueError("qualification candidate digest does not match payload")
        return self


def build_qualification_candidate(
    *,
    candidate_id: str,
    policy: Phase16QualificationPolicy,
    analyst_profile_digest: str,
    planner_profile_digest: str,
    adapter_digest: str,
) -> QualificationCandidate:
    """建立新 candidate 的自认证身份；调用方不可自行提供预计算 digest。"""

    candidate = QualificationCandidate(
        candidate_id=candidate_id,
        policy_digest=policy.policy_digest or "",
        analyst_profile_digest=analyst_profile_digest,
        planner_profile_digest=planner_profile_digest,
        adapter_digest=adapter_digest,
    )
    return candidate.model_copy(
        update={
            "candidate_digest": canonical_json_sha256(
                candidate.model_dump(mode="json", exclude={"candidate_digest"})
            )
        }
    )


class QualificationCorpusIdentity(BaseModel):
    """账本引用的 corpus 身份；PENDING corpus 不能用于 holdout campaign。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_id: str = Field(..., min_length=1)
    corpus_version: str = Field(..., min_length=1)
    policy_digest: str = Field(..., pattern=_HASH_PATTERN)
    corpus_digest: str = Field(..., pattern=_HASH_PATTERN)
    holdout_release_state: HoldoutReleaseState
    holdout_commitment_digest: str | None = Field(default=None, pattern=_HASH_PATTERN)
    holdout_case_count: int = Field(..., ge=30)
    holdout_high_conflict_e2e_case_count: int = Field(..., ge=30)

    @model_validator(mode="after")
    def _validate_identity(self) -> "QualificationCorpusIdentity":
        if self.holdout_high_conflict_e2e_case_count > self.holdout_case_count:
            raise ValueError("holdout E2E count cannot exceed corpus count")
        if self.holdout_release_state is HoldoutReleaseState.PENDING_INDEPENDENT_COMMITMENT:
            if self.holdout_commitment_digest is not None:
                raise ValueError("pending corpus cannot claim holdout commitment")
        elif self.holdout_commitment_digest is None:
            raise ValueError("committed or released corpus requires holdout commitment")
        return self


def corpus_identity_from_manifest(manifest: Phase16QualificationManifest) -> QualificationCorpusIdentity:
    """把公开 corpus Manifest 限制为 PENDING 身份，禁止它被误用作 holdout。"""

    return QualificationCorpusIdentity(
        corpus_id=manifest.corpus_id,
        corpus_version=manifest.corpus_version,
        policy_digest=manifest.policy_digest,
        corpus_digest=manifest.manifest_digest or "",
        holdout_release_state=manifest.holdout_release_state,
        holdout_commitment_digest=manifest.holdout_commitment_digest,
        holdout_case_count=manifest.holdout_case_count,
        holdout_high_conflict_e2e_case_count=manifest.holdout_high_conflict_e2e_case_count,
    )


class QualificationCampaign(BaseModel):
    """新的 campaign 身份；同一 candidate/phase 不能靠重跑挑选一次成功。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: str = Field(..., min_length=1)
    campaign_kind: QualificationCampaignKind
    policy_digest: str = Field(..., pattern=_HASH_PATTERN)
    corpus_digest: str = Field(..., pattern=_HASH_PATTERN)
    candidate_digest: str = Field(..., pattern=_HASH_PATTERN)
    manifest_digest: str = Field(..., pattern=_HASH_PATTERN)
    # 宽松兜底（与 DDL 范围一致）；真正的上界是 policy 的 campaign_budget_cny，
    # 由 ensure_campaign 在插入时对照 policy 行强制。
    reservation_cny: Decimal = Field(..., gt=Decimal("0"), le=Decimal("10.000000"))
    batch_index: int = Field(default=1, ge=1, le=2)
    # V9 矩阵配置：模型/思考强度/渠道列表不再冻结进 policy digest，改由 campaign
    # 声明运行时组合（白名单集合由 profiles.py 闭包认证，运行时只能在集合内挑选）。
    # receipt 行记录实际组合，核查以声明值为基准；防伪链仍是 receipt HMAC。
    declared_model_id: str = Field(default="gpt-5.6-luna", min_length=1)
    declared_reasoning_effort: str | None = Field(default=None, min_length=1)
    declared_endpoint_hosts: tuple[str, ...] = Field(default=("synapse-ai.uk",))

    @field_validator("declared_model_id")
    @classmethod
    def _validate_declared_model_id(cls, value: str) -> str:
        if value not in FORMAL_MODEL_IDS:
            raise ValueError(f"declared_model_id must be one of {sorted(FORMAL_MODEL_IDS)}")
        return value

    @field_validator("declared_reasoning_effort")
    @classmethod
    def _validate_declared_reasoning_effort(cls, value: str | None) -> str | None:
        if value is not None and value not in FORMAL_REASONING_EFFORTS:
            raise ValueError(
                f"declared_reasoning_effort must be one of {sorted(FORMAL_REASONING_EFFORTS)}"
            )
        return value

    @field_validator("declared_endpoint_hosts")
    @classmethod
    def _validate_declared_endpoint_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("declared_endpoint_hosts must be non-empty with unique hosts")
        normalized = tuple(normalize_endpoint_host(host) for host in value)
        if any(host not in FORMAL_ENDPOINT_HOSTS for host in normalized):
            raise ValueError(
                f"declared_endpoint_hosts must be within {sorted(FORMAL_ENDPOINT_HOSTS)}"
            )
        return normalized

    @model_validator(mode="after")
    def _validate_batch_identity(self) -> "QualificationCampaign":
        if self.campaign_kind is not QualificationCampaignKind.HOLDOUT and self.batch_index != 1:
            raise ValueError("only holdout campaigns may use a non-default batch index")
        return self


class QualificationSourceObservation(BaseModel):
    """历史事实的最小引用；不复制 receipt/outcome，也不允许自由模型文本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: str = Field(..., min_length=1)
    source_campaign_id: str = Field(..., min_length=1)
    source_manifest_digest: str = Field(..., pattern=_HASH_PATTERN)
    observation_digest: str = Field(..., pattern=_HASH_PATTERN)
    integrity_status: SourceEvidenceIntegrity


class QualificationReleaseEvent(BaseModel):
    """独立 release owner 的持久化摘要事实；密钥与 plaintext 均不进入该对象。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_digest: str = Field(..., pattern=_HASH_PATTERN)
    commitment_digest: str = Field(..., pattern=_HASH_PATTERN)
    plaintext_digest: str = Field(..., pattern=_HASH_PATTERN)
    release_owner_id_digest: str = Field(..., pattern=_HASH_PATTERN)
    release_auth_tag: str | None = Field(default=None, pattern=_HASH_PATTERN)


class QualificationMetricFact(BaseModel):
    """可复算的 aggregate metric；不落 case 文本、模型正文或标签原文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    metric_code: str = Field(..., pattern=_REASON_CODE_PATTERN)
    numerator: int = Field(..., ge=0)
    denominator: int = Field(..., gt=0)
    metric_digest: str | None = Field(default=None, pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def _validate_metric(self) -> "QualificationMetricFact":
        if self.numerator > self.denominator:
            raise ValueError("qualification metric numerator cannot exceed denominator")
        if self.metric_digest is not None:
            expected = canonical_json_sha256(
                self.model_dump(mode="json", exclude={"metric_digest"})
            )
            if self.metric_digest != expected:
                raise ValueError("qualification metric digest does not match payload")
        return self


def build_qualification_metric(
    *, run_id: str, metric_code: str, numerator: int, denominator: int
) -> QualificationMetricFact:
    """由账本拥有 metric digest 的构造，拒绝调用方随意拼摘要。"""

    metric = QualificationMetricFact(
        run_id=run_id,
        metric_code=metric_code,
        numerator=numerator,
        denominator=denominator,
    )
    return metric.model_copy(
        update={
            "metric_digest": canonical_json_sha256(
                metric.model_dump(mode="json", exclude={"metric_digest"})
            )
        }
    )


@dataclass(frozen=True)
class QualificationRunReport:
    """安全报告投影；认证失败的 PASS 必须在读取端 fail-closed。"""

    run_id: str
    campaign_id: str
    campaign_kind: QualificationCampaignKind
    status: QualificationRunStatus
    reason_code: str
    evaluation_digest: str
    authenticated: bool
    metrics: tuple[QualificationMetricFact, ...]


class PostgresPhase16QualificationLedger:
    """隐藏事务、身份冲突、项目预算与 HMAC 细节的 qualification 账本深模块。"""

    def __init__(self, settings: Any, *, hmac_key: bytes) -> None:
        if len(hmac_key) < 32:
            raise ValueError("qualification ledger HMAC key must contain at least 256 bits")
        self._settings = settings
        self._hmac_key = hmac_key

    def _connection(self):
        return psycopg.connect(
            **self._settings.postgres_connection_kwargs,
            row_factory=dict_row,
        )

    def _tag(self, *, domain: str, payload: dict[str, object]) -> str:
        digest = canonical_json_sha256(payload)
        message = f"phase16-qualification-v1:{domain}:{digest}".encode("utf-8")
        return hmac.new(self._hmac_key, message, "sha256").hexdigest()

    def _verify_tag(self, *, domain: str, payload: dict[str, object], tag: str) -> bool:
        return hmac.compare_digest(self._tag(domain=domain, payload=payload), tag)

    @staticmethod
    def _source_closure_digest(policy: Phase16QualificationPolicy) -> str:
        return canonical_json_sha256(dict(sorted(policy.source_file_digests.items())))

    def ensure_policy(self, policy: Phase16QualificationPolicy) -> None:
        """插入或锁定 policy；相同 digest 之外的任何身份漂移均失败。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_qualification_policies
                           (policy_digest, policy_id, policy_version, project_budget_cny,
                            campaign_budget_cny, holdout_batch_count, holdout_e2e_cases_per_batch,
                            stage_reservation_cny, source_closure_digest)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (policy_digest) DO NOTHING""",
                        (
                            policy.policy_digest,
                            policy.policy_id,
                            policy.policy_version,
                            policy.project_budget_cny,
                            policy.campaign_budget_cny,
                            policy.holdout_batch_count,
                            policy.holdout_high_conflict_cases_per_batch,
                            policy.stage_reservation_cny,
                            self._source_closure_digest(policy),
                        ),
                    )
                    cursor.execute(
                        """SELECT policy_id, policy_version, project_budget_cny, campaign_budget_cny,
                                  holdout_batch_count, holdout_e2e_cases_per_batch, stage_reservation_cny,
                                  source_closure_digest
                             FROM phase16_qualification_policies
                            WHERE policy_digest=%s FOR UPDATE""",
                        (policy.policy_digest,),
                    )
                    row = cursor.fetchone()
                    if row is None or (
                        row["policy_id"],
                        row["policy_version"],
                        Decimal(row["project_budget_cny"]),
                        Decimal(row["campaign_budget_cny"]),
                        row["holdout_batch_count"],
                        row["holdout_e2e_cases_per_batch"],
                        Decimal(row["stage_reservation_cny"]),
                        row["source_closure_digest"],
                    ) != (
                        policy.policy_id,
                        policy.policy_version,
                        policy.project_budget_cny,
                        policy.campaign_budget_cny,
                        policy.holdout_batch_count,
                        policy.holdout_high_conflict_cases_per_batch,
                        policy.stage_reservation_cny,
                        self._source_closure_digest(policy),
                    ):
                        raise Phase16QualificationLedgerError("qualification policy identity conflicts")
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification policy initialization failed") from error

    def ensure_corpus(self, corpus: QualificationCorpusIdentity) -> None:
        """插入或精确复验 corpus；commitment state 的变化必须是新 corpus digest。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_qualification_corpora
                           (corpus_digest, corpus_id, corpus_version, policy_digest,
                            holdout_release_state, holdout_commitment_digest, holdout_case_count,
                            holdout_high_conflict_e2e_case_count)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (corpus_digest) DO NOTHING""",
                        (
                            corpus.corpus_digest,
                            corpus.corpus_id,
                            corpus.corpus_version,
                            corpus.policy_digest,
                            corpus.holdout_release_state.value,
                            corpus.holdout_commitment_digest,
                            corpus.holdout_case_count,
                            corpus.holdout_high_conflict_e2e_case_count,
                        ),
                    )
                    cursor.execute(
                        """SELECT corpus_id, corpus_version, policy_digest, holdout_release_state,
                                  holdout_commitment_digest, holdout_case_count,
                                  holdout_high_conflict_e2e_case_count
                             FROM phase16_qualification_corpora WHERE corpus_digest=%s FOR UPDATE""",
                        (corpus.corpus_digest,),
                    )
                    row = cursor.fetchone()
                    expected = (
                        corpus.corpus_id,
                        corpus.corpus_version,
                        corpus.policy_digest,
                        corpus.holdout_release_state.value,
                        corpus.holdout_commitment_digest,
                        corpus.holdout_case_count,
                        corpus.holdout_high_conflict_e2e_case_count,
                    )
                    actual = None if row is None else (
                        row["corpus_id"],
                        row["corpus_version"],
                        row["policy_digest"],
                        row["holdout_release_state"],
                        row["holdout_commitment_digest"],
                        row["holdout_case_count"],
                        row["holdout_high_conflict_e2e_case_count"],
                    )
                    if actual != expected:
                        raise Phase16QualificationLedgerError("qualification corpus identity conflicts")
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification corpus initialization failed") from error

    def ensure_candidate(self, candidate: QualificationCandidate) -> None:
        """candidate digest 绑定所有模型可执行输出契约身份，不能同名替换。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_qualification_candidates
                           (candidate_digest, policy_digest, candidate_id, model_id, endpoint_host,
                            analyst_profile_digest, planner_profile_digest, adapter_digest)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (candidate_digest) DO NOTHING""",
                        (
                            candidate.candidate_digest,
                            candidate.policy_digest,
                            candidate.candidate_id,
                            candidate.model_id,
                            candidate.endpoint_host,
                            candidate.analyst_profile_digest,
                            candidate.planner_profile_digest,
                            candidate.adapter_digest,
                        ),
                    )
                    cursor.execute(
                        """SELECT policy_digest, candidate_id, model_id, endpoint_host,
                                  analyst_profile_digest, planner_profile_digest, adapter_digest
                             FROM phase16_qualification_candidates
                            WHERE candidate_digest=%s FOR UPDATE""",
                        (candidate.candidate_digest,),
                    )
                    row = cursor.fetchone()
                    expected = (
                        candidate.policy_digest,
                        candidate.candidate_id,
                        candidate.model_id,
                        candidate.endpoint_host,
                        candidate.analyst_profile_digest,
                        candidate.planner_profile_digest,
                        candidate.adapter_digest,
                    )
                    actual = None if row is None else tuple(row[name] for name in (
                        "policy_digest", "candidate_id", "model_id", "endpoint_host",
                        "analyst_profile_digest", "planner_profile_digest", "adapter_digest",
                    ))
                    if actual != expected:
                        raise Phase16QualificationLedgerError("qualification candidate identity conflicts")
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification candidate initialization failed") from error

    def ensure_campaign(self, campaign: QualificationCampaign) -> None:
        """建立一次性 campaign，并在 policy 行锁内预留不超过 3 CNY 的总项目敞口。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    # 身份规则冻结在闭包内：campaign_id 必须是声明组合的 canonical
                    # 渲染，脚本层不能用手工 id 绕过防刷分。
                    canonical = qualification_campaign_id(
                        kind=campaign.campaign_kind,
                        candidate_digest=campaign.candidate_digest,
                        declared_model_id=campaign.declared_model_id,
                        declared_reasoning_effort=campaign.declared_reasoning_effort,
                        declared_endpoint_hosts=campaign.declared_endpoint_hosts,
                        batch_index=campaign.batch_index,
                    )
                    if canonical != campaign.campaign_id:
                        raise Phase16QualificationLedgerError(
                            "qualification campaign id does not match declared identity"
                        )
                    # 同一 digest 下同一声明组合只能有一个 campaign（防刷分核心）：
                    # 切换组合才开新名额；campaign_id 格式迁移不能重开已跑过的组合。
                    cursor.execute(
                        """SELECT campaign_id FROM phase16_qualification_campaigns
                            WHERE campaign_kind=%s AND candidate_digest=%s
                              AND declared_model_id=%s
                              AND declared_reasoning_effort IS NOT DISTINCT FROM %s
                              AND declared_endpoint_hosts=%s""",
                        (
                            campaign.campaign_kind.value,
                            campaign.candidate_digest,
                            campaign.declared_model_id,
                            campaign.declared_reasoning_effort,
                            ",".join(campaign.declared_endpoint_hosts),
                        ),
                    )
                    existing = cursor.fetchone()
                    if existing is not None and existing["campaign_id"] != campaign.campaign_id:
                        raise Phase16QualificationLedgerError(
                            "qualification campaign identity conflicts"
                        )
                    cursor.execute(
                        """SELECT project_budget_cny, campaign_budget_cny, holdout_batch_count
                             FROM phase16_qualification_policies
                            WHERE policy_digest=%s FOR UPDATE""",
                        (campaign.policy_digest,),
                    )
                    policy = cursor.fetchone()
                    if policy is None or campaign.reservation_cny > Decimal(policy["campaign_budget_cny"]):
                        raise Phase16QualificationLedgerError("qualification campaign policy is unavailable")
                    cursor.execute(
                        """SELECT policy_digest FROM phase16_qualification_corpora WHERE corpus_digest=%s""",
                        (campaign.corpus_digest,),
                    )
                    corpus = cursor.fetchone()
                    cursor.execute(
                        """SELECT policy_digest FROM phase16_qualification_candidates WHERE candidate_digest=%s""",
                        (campaign.candidate_digest,),
                    )
                    candidate = cursor.fetchone()
                    if (
                        corpus is None
                        or candidate is None
                        or corpus["policy_digest"] != campaign.policy_digest
                        or candidate["policy_digest"] != campaign.policy_digest
                    ):
                        raise Phase16QualificationLedgerError("qualification campaign parent identity is invalid")
                    if campaign.campaign_kind is QualificationCampaignKind.HOLDOUT:
                        if campaign.batch_index > policy["holdout_batch_count"]:
                            raise Phase16QualificationLedgerError("qualification holdout batch is outside policy")
                        cursor.execute(
                            """SELECT holdout_release_state FROM phase16_qualification_corpora
                                 WHERE corpus_digest=%s""",
                            (campaign.corpus_digest,),
                        )
                        state = cursor.fetchone()
                        if state is None or state["holdout_release_state"] not in {
                            HoldoutReleaseState.COMMITTED.value,
                            HoldoutReleaseState.RELEASED.value,
                        }:
                            raise Phase16QualificationLedgerError("qualification holdout corpus is not committed")
                    cursor.execute(
                        """INSERT INTO phase16_qualification_campaigns
                           (campaign_id, campaign_kind, batch_index, policy_digest, corpus_digest, candidate_digest,
                            manifest_digest, reservation_cny, declared_model_id, declared_reasoning_effort,
                            declared_endpoint_hosts)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (campaign_id) DO NOTHING""",
                        (
                            campaign.campaign_id,
                            campaign.campaign_kind.value,
                            campaign.batch_index,
                            campaign.policy_digest,
                            campaign.corpus_digest,
                            campaign.candidate_digest,
                            campaign.manifest_digest,
                            campaign.reservation_cny,
                            campaign.declared_model_id,
                            campaign.declared_reasoning_effort,
                            ",".join(campaign.declared_endpoint_hosts),
                        ),
                    )
                    cursor.execute(
                        """SELECT campaign_kind, batch_index, policy_digest, corpus_digest, candidate_digest,
                                  manifest_digest, reservation_cny, declared_model_id, declared_reasoning_effort,
                                  declared_endpoint_hosts
                             FROM phase16_qualification_campaigns
                            WHERE campaign_id=%s FOR UPDATE""",
                        (campaign.campaign_id,),
                    )
                    row = cursor.fetchone()
                    expected = (
                        campaign.campaign_kind.value,
                        campaign.batch_index,
                        campaign.policy_digest,
                        campaign.corpus_digest,
                        campaign.candidate_digest,
                        campaign.manifest_digest,
                        campaign.reservation_cny,
                        campaign.declared_model_id,
                        campaign.declared_reasoning_effort,
                        ",".join(campaign.declared_endpoint_hosts),
                    )
                    actual = None if row is None else (
                        row["campaign_kind"], row["batch_index"], row["policy_digest"], row["corpus_digest"],
                        row["candidate_digest"], row["manifest_digest"], Decimal(row["reservation_cny"]),
                        row["declared_model_id"], row["declared_reasoning_effort"], row["declared_endpoint_hosts"],
                    )
                    if actual != expected:
                        raise Phase16QualificationLedgerError("qualification campaign identity conflicts")
                    cursor.execute(
                        """SELECT COALESCE(SUM(
                               CASE
                                   WHEN run.run_id IS NULL THEN campaign.reservation_cny
                                   WHEN result.run_id IS NULL THEN campaign.reservation_cny
                                   ELSE COALESCE(spent.spent_cny, 0)
                               END
                           ), 0) AS committed
                             FROM phase16_qualification_campaigns campaign
                             LEFT JOIN phase16_qualification_runs run ON run.campaign_id = campaign.campaign_id
                             LEFT JOIN phase16_qualification_results result ON result.run_id = run.run_id
                             LEFT JOIN (
                                 SELECT run.campaign_id, SUM(receipt.actual_cost_cny) AS spent_cny
                                   FROM phase16_qualification_provider_receipts receipt
                                   JOIN phase16_qualification_dispatch_attempts attempt
                                     ON attempt.attempt_id = receipt.attempt_id
                                   JOIN phase16_qualification_runs run ON run.run_id = attempt.run_id
                                  GROUP BY run.campaign_id
                             ) spent ON spent.campaign_id = campaign.campaign_id
                            WHERE campaign.policy_digest = %s""",
                        (campaign.policy_digest,),
                    )
                    if Decimal(cursor.fetchone()["committed"]) > Decimal(policy["project_budget_cny"]):
                        raise Phase16QualificationLedgerError("qualification project budget is exhausted")
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification campaign initialization failed") from error

    def record_source_observation(self, observation: QualificationSourceObservation) -> None:
        """追加一个 V1–V8 的摘要引用；同一来源不可被新摘要或完整性状态替换。"""

        self._insert_exact(
            table="phase16_qualification_source_evidence",
            conflict_columns=("campaign_id", "source_campaign_id"),
            values={
                "campaign_id": observation.campaign_id,
                "source_campaign_id": observation.source_campaign_id,
                "source_manifest_digest": observation.source_manifest_digest,
                "observation_digest": observation.observation_digest,
                "integrity_status": observation.integrity_status.value,
            },
            error="qualification source observation conflicts",
        )

    def record_release_event(self, event: QualificationReleaseEvent) -> QualificationReleaseEvent:
        """追加独立 owner release；账本重签固定摘要，不接受调用方给出的 auth tag。"""

        payload = {
            "corpus_digest": event.corpus_digest,
            "commitment_digest": event.commitment_digest,
            "plaintext_digest": event.plaintext_digest,
            "release_owner_id_digest": event.release_owner_id_digest,
        }
        signed = event.model_copy(update={"release_auth_tag": self._tag(domain="release", payload=payload)})
        self._insert_exact(
            table="phase16_qualification_release_events",
            conflict_columns=("corpus_digest",),
            values={**payload, "release_auth_tag": signed.release_auth_tag},
            error="qualification release event conflicts",
        )
        return signed

    def begin_run(self, *, run_id: str, campaign_id: str) -> None:
        """开始唯一 run；已有终态时 fail-closed，防止 CLI 重入被当成重试。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT 1 FROM phase16_qualification_campaigns WHERE campaign_id=%s FOR UPDATE",
                        (campaign_id,),
                    )
                    if cursor.fetchone() is None:
                        raise Phase16QualificationLedgerError("qualification campaign is not initialized")
                    cursor.execute(
                        """INSERT INTO phase16_qualification_runs (run_id, campaign_id)
                           VALUES (%s,%s) ON CONFLICT (run_id) DO NOTHING""",
                        (run_id, campaign_id),
                    )
                    cursor.execute(
                        """SELECT campaign_id FROM phase16_qualification_runs
                             WHERE run_id=%s FOR UPDATE""",
                        (run_id,),
                    )
                    row = cursor.fetchone()
                    if row is None or row["campaign_id"] != campaign_id:
                        raise Phase16QualificationLedgerError("qualification run identity conflicts")
                    cursor.execute("SELECT 1 FROM phase16_qualification_results WHERE run_id=%s", (run_id,))
                    if cursor.fetchone() is not None:
                        raise Phase16QualificationLedgerError("qualification run is already terminal")
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification run initialization failed") from error

    def append_metric(self, metric: QualificationMetricFact) -> QualificationMetricFact:
        """追加一个不可替换 metric；run 终态后不再接受新事实。"""

        self._ensure_live_run(metric.run_id)
        self._insert_exact(
            table="phase16_qualification_metric_facts",
            conflict_columns=("run_id", "metric_code"),
            values={
                "run_id": metric.run_id,
                "metric_code": metric.metric_code,
                "numerator": metric.numerator,
                "denominator": metric.denominator,
                "metric_digest": metric.metric_digest,
            },
            error="qualification metric fact conflicts",
        )
        return metric

    def close_run(
        self,
        *,
        run_id: str,
        status: QualificationRunStatus,
        reason_code: str,
        evaluation_digest: str,
    ) -> QualificationRunReport:
        """以结果摘要/HMAC 终态化 run；SQL 另外校验 PASS 所需 metric/release。"""

        if not re.fullmatch(_REASON_CODE_PATTERN, reason_code):
            raise ValueError("qualification result reason code is invalid")
        if not re.fullmatch(_HASH_PATTERN, evaluation_digest):
            raise ValueError("qualification evaluation digest is invalid")
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT run.campaign_id, campaign.campaign_kind
                             FROM phase16_qualification_runs run
                             JOIN phase16_qualification_campaigns campaign ON campaign.campaign_id=run.campaign_id
                            WHERE run.run_id=%s FOR UPDATE""",
                        (run_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise Phase16QualificationLedgerError("qualification run is unavailable")
                    cursor.execute("SELECT 1 FROM phase16_qualification_results WHERE run_id=%s", (run_id,))
                    if cursor.fetchone() is not None:
                        raise Phase16QualificationLedgerError("qualification run is already terminal")
                    payload = {
                        "run_id": run_id,
                        "campaign_id": row["campaign_id"],
                        "campaign_kind": row["campaign_kind"],
                        "status": status.value,
                        "reason_code": reason_code,
                        "evaluation_digest": evaluation_digest,
                    }
                    result_digest = canonical_json_sha256(payload)
                    auth_tag = self._tag(domain="result", payload=payload)
                    cursor.execute(
                        """INSERT INTO phase16_qualification_results
                           (run_id, status, reason_code, evaluation_digest, result_digest, result_auth_tag)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (run_id, status.value, reason_code, evaluation_digest, result_digest, auth_tag),
                    )
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification run close failed") from error
        return self.report(run_id=run_id)

    def report(self, *, run_id: str) -> QualificationRunReport:
        """从只读投影重建 report；认证失败绝不返回可信 PASS。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT run.campaign_id, campaign.campaign_kind, result.status, result.reason_code,
                                  result.evaluation_digest, result.result_digest, result.result_auth_tag
                             FROM phase16_qualification_runs run
                             JOIN phase16_qualification_campaigns campaign ON campaign.campaign_id=run.campaign_id
                             JOIN phase16_qualification_results result ON result.run_id=run.run_id
                            WHERE run.run_id=%s""",
                        (run_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise Phase16QualificationLedgerError("qualification terminal result is unavailable")
                    payload = {
                        "run_id": run_id,
                        "campaign_id": row["campaign_id"],
                        "campaign_kind": row["campaign_kind"],
                        "status": row["status"],
                        "reason_code": row["reason_code"],
                        "evaluation_digest": row["evaluation_digest"],
                    }
                    authenticated = (
                        row["result_digest"] == canonical_json_sha256(payload)
                        and self._verify_tag(domain="result", payload=payload, tag=row["result_auth_tag"])
                    )
                    cursor.execute(
                        """SELECT metric_code, numerator, denominator, metric_digest
                             FROM phase16_qualification_metric_facts
                            WHERE run_id=%s ORDER BY metric_code""",
                        (run_id,),
                    )
                    metrics = tuple(
                        QualificationMetricFact(
                            run_id=run_id,
                            metric_code=item["metric_code"],
                            numerator=item["numerator"],
                            denominator=item["denominator"],
                            metric_digest=item["metric_digest"],
                        )
                        for item in cursor.fetchall()
                    )
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification report is unavailable") from error
        status = QualificationRunStatus(row["status"])
        reason_code = row["reason_code"]
        if status is QualificationRunStatus.PASS and not authenticated:
            status = QualificationRunStatus.BLOCKED
            reason_code = "RESULT_AUTHENTICATION_FAILED"
        return QualificationRunReport(
            run_id=run_id,
            campaign_id=row["campaign_id"],
            campaign_kind=QualificationCampaignKind(row["campaign_kind"]),
            status=status,
            reason_code=reason_code,
            evaluation_digest=row["evaluation_digest"],
            authenticated=authenticated,
            metrics=metrics,
        )

    def _ensure_live_run(self, run_id: str) -> None:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM phase16_qualification_runs WHERE run_id=%s FOR UPDATE", (run_id,))
                    if cursor.fetchone() is None:
                        raise Phase16QualificationLedgerError("qualification run is unavailable")
                    cursor.execute("SELECT 1 FROM phase16_qualification_results WHERE run_id=%s", (run_id,))
                    if cursor.fetchone() is not None:
                        raise Phase16QualificationLedgerError("qualification run is already terminal")
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as error:
            raise Phase16QualificationLedgerError("qualification run state is unavailable") from error

    def _insert_exact(
        self,
        *,
        table: str,
        conflict_columns: tuple[str, ...],
        values: dict[str, object],
        error: str,
    ) -> None:
        """统一做 INSERT-if-absent 后锁读精确比对，杜绝同 ID 换内容。"""

        columns = tuple(values)
        placeholders = ",".join("%s" for _ in columns)
        where = " AND ".join(f"{name}=%s" for name in conflict_columns)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
                        f"ON CONFLICT ({','.join(conflict_columns)}) DO NOTHING",
                        tuple(values[name] for name in columns),
                    )
                    cursor.execute(
                        f"SELECT {','.join(columns)} FROM {table} WHERE {where} FOR UPDATE",
                        tuple(values[name] for name in conflict_columns),
                    )
                    row = cursor.fetchone()
                    if row is None or any(row[name] != values[name] for name in columns):
                        raise Phase16QualificationLedgerError(error)
                connection.commit()
        except Phase16QualificationLedgerError:
            raise
        except psycopg.Error as database_error:
            raise Phase16QualificationLedgerError(error) from database_error


def initialize_phase16_qualification_schema(settings: Any) -> None:
    """执行 qualification 专属 DDL；测试与部署走同一份 append-only 约束。"""

    path = Path(__file__).resolve().parents[2] / "docker" / "init_phase16_qualification_ledger.sql"
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(path.read_text(encoding="utf-8"))
        connection.commit()
