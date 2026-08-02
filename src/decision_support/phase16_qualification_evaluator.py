"""Phase 16 三层资格体系的纯评估、candidate admission 与 V8 只读证据适配器。

此模块将历史观察、确定性安全、validation 性能和密封 holdout 分开评价。它不发送模型、
不修改 V1–V8 表，也不会把模型正文、Prompt、密钥或 release plaintext 带入 assessment。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
import hmac
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.decision_support.controlled_e2e_v5 import (
    PHASE16_V5_CAMPAIGN_ID,
    PHASE16_V5_FORMAL_RUN_ID,
    load_phase16_v5_manifest,
)
from src.decision_support.phase16_qualification import (
    HoldoutReleaseState,
    Phase16QualificationPolicy,
    classify_v8_reason_code,
)
from src.decision_support.phase16_qualification_candidate import (
    build_phase16_qualification_analyst_profile,
    build_phase16_qualification_planner_profile,
    qualification_adapter_digest,
)
from src.decision_support.phase16_qualification_ledger import (
    QualificationCampaign,
    QualificationCampaignKind,
    QualificationCandidate,
    QualificationCorpusIdentity,
    QualificationMetricFact,
    QualificationRunStatus,
    SourceEvidenceIntegrity,
    build_qualification_candidate,
)
from src.specialist_runtime.models import canonical_json_sha256
from src.specialist_runtime.profiles import FinalEvidenceBindingMode, SpecialistProfile


_REQUIRED_E2E_METRIC = "E2E_MULTI_AGENT_READY"
_REQUIRED_SAFETY_METRIC = "HARD_SAFETY_CONFORMANCE"


class QualificationAssessmentStatus(StrEnum):
    """assessment 的证据等级；没有任何值可暗示生产 activation。"""

    NOT_QUALIFIED = "NOT_QUALIFIED"
    ENGINEERING_SAFETY_CONFORMANCE = "ENGINEERING_SAFETY_CONFORMANCE"
    DEVELOPMENT_DIAGNOSTIC = "DEVELOPMENT_DIAGNOSTIC"
    VALIDATION_PERFORMANCE = "VALIDATION_PERFORMANCE"
    HOLDOUT_BATCH_PASS = "HOLDOUT_BATCH_PASS"
    HOLDOUT_QUALIFICATION = "HOLDOUT_QUALIFICATION"


@dataclass(frozen=True)
class V8HistoricalObservation:
    """从 V8 append-only ledger 读取出的最小、可公开历史观察。"""

    source_campaign_id: str
    run_id: str
    source_manifest_digest: str
    integrity_status: SourceEvidenceIntegrity
    run_status: str
    run_reason_code: str
    attempted_stage_count: int
    case_pass_count: int
    case_total_count: int
    failure_categories: tuple[tuple[str, int], ...]
    observation_digest: str

    @property
    def qualification_eligible(self) -> bool:
        """V8 使用 development/validation case，故永远不能成为 validation/holdout 成功样本。"""

        return False


@dataclass(frozen=True)
class CandidateProfileBundle:
    """冻结 candidate 的两个 Profile 和可由源文件重建的 Adapter 身份。"""

    candidate: QualificationCandidate
    analyst_profile: SpecialistProfile
    planner_profile: SpecialistProfile


@dataclass(frozen=True)
class QualificationCampaignAdmission:
    """开始新 dispatch 前的纯本地 admission 结论。"""

    allowed: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class QualificationAssessment:
    """可写入新 ledger 的非敏感派生 assessment。"""

    campaign_id: str
    policy_digest: str
    corpus_digest: str
    candidate_digest: str
    campaign_kind: QualificationCampaignKind
    batch_index: int
    status: QualificationAssessmentStatus
    run_status: QualificationRunStatus
    reason_codes: tuple[str, ...]
    metric_facts: tuple[QualificationMetricFact, ...]
    assessment_digest: str


@dataclass(frozen=True)
class HoldoutQualificationAssessment:
    """两个独立 15-case batch 的聚合结论；单批 PASS 不能单独宣称 30/30 qualification。"""

    policy_digest: str
    corpus_digest: str
    candidate_digest: str
    status: QualificationAssessmentStatus
    reason_codes: tuple[str, ...]
    batch_assessment_digests: tuple[str, ...]
    aggregate_e2e_numerator: int
    aggregate_e2e_denominator: int
    assessment_digest: str


@dataclass(frozen=True)
class EngineeringSafetyAssessment:
    """确定性 guardrail conformance 的独立结论，绝不从模型通过率推导。"""

    policy_digest: str
    corpus_digest: str
    status: QualificationAssessmentStatus
    reason_codes: tuple[str, ...]
    metric_facts: tuple[QualificationMetricFact, ...]
    assessment_digest: str


class V8ReadOnlyEvidenceAdapter:
    """读取、认证并投影 V8；认证失败保留历史观察但把完整性降为 UNVERIFIABLE。"""

    def __init__(self, settings: Any, *, hmac_key: bytes, repository_root: Path) -> None:
        if len(hmac_key) < 32:
            raise ValueError("V8 source verification key must contain at least 256 bits")
        self._settings = settings
        self._hmac_key = hmac_key
        self._repository_root = Path(repository_root)

    def _sign_v5(self, domain: str, payload: dict[str, object]) -> str:
        message = f"phase16-v5-controlled-e2e:{domain}:{canonical_json_sha256(payload)}".encode("utf-8")
        return hmac.new(self._hmac_key, message, "sha256").hexdigest()

    @staticmethod
    def _decimal_string(value: object, precision: str) -> str:
        return str(Decimal(value).quantize(Decimal(precision), rounding=ROUND_HALF_UP))

    def read_v8_formal(self) -> V8HistoricalObservation:
        """在 READ ONLY 事务中认证 V8 receipt/validation facts 并生成历史观察摘要。"""

        manifest = load_phase16_v5_manifest(repository_root=self._repository_root)
        try:
            with psycopg.connect(
                **self._settings.postgres_connection_kwargs, row_factory=dict_row
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """SELECT campaign.manifest_digest, outcome.status, outcome.reason_code
                             FROM phase16_v5_runs run
                             JOIN phase16_v5_campaigns campaign ON campaign.campaign_id=run.campaign_id
                             JOIN phase16_v5_run_outcomes outcome ON outcome.run_id=run.run_id
                            WHERE run.run_id=%s AND run.campaign_id=%s""",
                        (PHASE16_V5_FORMAL_RUN_ID, PHASE16_V5_CAMPAIGN_ID),
                    )
                    run = cursor.fetchone()
                    if run is None:
                        raise ValueError("V8 formal source run is unavailable")
                    cursor.execute(
                        """SELECT case_id, status, reason_code
                             FROM phase16_v5_case_outcomes WHERE run_id=%s ORDER BY case_id""",
                        (PHASE16_V5_FORMAL_RUN_ID,),
                    )
                    case_rows = cursor.fetchall()
                    cursor.execute(
                        """SELECT attempt.attempt_id, receipt.provider_response_id_digest,
                                  receipt.finish_reason, receipt.model_id, receipt.response_digest,
                                  receipt.input_tokens, receipt.output_tokens, receipt.total_tokens,
                                  receipt.latency_ms, receipt.actual_cost_cny, receipt.output_digest,
                                  receipt.receipt_complete, receipt.receipt_auth_tag,
                                  validation.verdict, validation.reason_code, validation.validation_digest,
                                  validation.validation_auth_tag
                             FROM phase16_v5_dispatch_attempts attempt
                             JOIN phase16_v5_provider_receipts receipt ON receipt.attempt_id=attempt.attempt_id
                             JOIN phase16_v5_validation_facts validation ON validation.attempt_id=attempt.attempt_id
                            WHERE attempt.run_id=%s ORDER BY attempt.created_at""",
                        (PHASE16_V5_FORMAL_RUN_ID,),
                    )
                    stage_rows = cursor.fetchall()
        except (psycopg.Error, ValueError) as error:
            raise ValueError("V8 source evidence cannot be read") from error

        authenticated = run["manifest_digest"] == manifest.manifest_digest
        for row in stage_rows:
            receipt_payload = {
                "attempt_id": str(row["attempt_id"]),
                "provider_response_id_digest": row["provider_response_id_digest"],
                "finish_reason": row["finish_reason"],
                "model_id": row["model_id"],
                "response_digest": row["response_digest"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "total_tokens": row["total_tokens"],
                "latency_ms": self._decimal_string(row["latency_ms"], "0.001"),
                "actual_cost_cny": self._decimal_string(row["actual_cost_cny"], "0.000001"),
                "output_digest": row["output_digest"],
                "receipt_complete": bool(row["receipt_complete"]),
            }
            validation_payload = {
                "attempt_id": str(row["attempt_id"]),
                "verdict": row["verdict"],
                "reason_code": row["reason_code"],
                "validation_digest": row["validation_digest"],
            }
            authenticated = authenticated and hmac.compare_digest(
                row["receipt_auth_tag"], self._sign_v5("receipt", receipt_payload)
            )
            authenticated = authenticated and hmac.compare_digest(
                row["validation_auth_tag"], self._sign_v5("validation", validation_payload)
            )
        failure_categories = Counter(
            classify_v8_reason_code(row["reason_code"])
            for row in stage_rows
            if row["verdict"] != "PASS"
        )
        payload = {
            "source_campaign_id": PHASE16_V5_CAMPAIGN_ID,
            "run_id": PHASE16_V5_FORMAL_RUN_ID,
            "source_manifest_digest": run["manifest_digest"],
            "integrity_status": (
                SourceEvidenceIntegrity.AUTHENTICATED.value
                if authenticated
                else SourceEvidenceIntegrity.UNVERIFIABLE.value
            ),
            "run_status": run["status"],
            "run_reason_code": run["reason_code"],
            "attempted_stage_count": len(stage_rows),
            "case_pass_count": sum(row["status"] == "PASS" for row in case_rows),
            "case_total_count": len(case_rows),
            "failure_categories": sorted(failure_categories.items()),
        }
        observation_payload = {
            **payload,
            "integrity_status": SourceEvidenceIntegrity(payload["integrity_status"]),
            "failure_categories": tuple(payload["failure_categories"]),
            "observation_digest": canonical_json_sha256(payload),
        }
        return V8HistoricalObservation(**observation_payload)


def build_phase16_qualification_candidate_bundle(
    *, policy: Phase16QualificationPolicy, repository_root: Path
) -> CandidateProfileBundle:
    """从新 Profile 与 Adapter 源码构造 candidate；V8 Profile 绝不被替换。"""

    analyst = build_phase16_qualification_analyst_profile()
    planner = build_phase16_qualification_planner_profile()
    candidate = build_qualification_candidate(
        candidate_id="phase16-qualification-v2-candidate-output-contract-001",
        policy=policy,
        analyst_profile_digest=analyst.profile_digest,
        planner_profile_digest=planner.profile_digest,
        adapter_digest=qualification_adapter_digest(repository_root=repository_root),
    )
    return CandidateProfileBundle(candidate=candidate, analyst_profile=analyst, planner_profile=planner)


def admit_qualification_campaign(
    *,
    policy: Phase16QualificationPolicy,
    corpus: QualificationCorpusIdentity,
    candidate_bundle: CandidateProfileBundle,
    campaign_kind: QualificationCampaignKind,
) -> QualificationCampaignAdmission:
    """纯本地检查 candidate 与 corpus；任何身份漂移都在联网前 fail-closed。"""

    reasons: list[str] = []
    candidate = candidate_bundle.candidate
    analyst = candidate_bundle.analyst_profile
    planner = candidate_bundle.planner_profile
    if candidate.policy_digest != policy.policy_digest or corpus.policy_digest != policy.policy_digest:
        reasons.append("POLICY_IDENTITY_MISMATCH")
    for profile, expected_digest, expected_task_kind in (
        (analyst, candidate.analyst_profile_digest, "CONFLICT_ANALYSIS"),
        (planner, candidate.planner_profile_digest, "LIVE_DECISION_PLANNING"),
    ):
        if (
            profile.profile_digest != expected_digest
            or profile.task_kind.value != expected_task_kind
            # V9 矩阵配置起，模型 / 主端点不再是 policy 冻结字段：profile 保持冻结
            # 默认身份（candidate digest 绑定），运行时模型/端点由 campaign 声明，
            # 经共享 runner 的 env 覆写注入请求、由 receipt 与 HARD_SAFETY 身份事实核对。
            or profile.temperature != Decimal("0")
            or profile.allowed_skill_ids
            or profile.max_skill_calls != 0
            or profile.max_model_calls != 1
            or profile.final_evidence_binding_mode is not FinalEvidenceBindingMode.SYSTEM_MANAGED_IDS
        ):
            reasons.append("CANDIDATE_PROFILE_CONTRACT_MISMATCH")
            break
    if campaign_kind is QualificationCampaignKind.HOLDOUT:
        if corpus.holdout_release_state not in {
            HoldoutReleaseState.COMMITTED,
            HoldoutReleaseState.RELEASED,
        } or corpus.holdout_commitment_digest is None:
            reasons.append("HOLDOUT_COMMITMENT_REQUIRED")
        if corpus.holdout_high_conflict_e2e_case_count < policy.holdout_high_conflict_case_count:
            reasons.append("HOLDOUT_E2E_COVERAGE_INSUFFICIENT")
    return QualificationCampaignAdmission(allowed=not reasons, reason_codes=tuple(sorted(set(reasons))))


class QualificationEvaluator:
    """把完整性、stage/E2E 指标和三层 claim 组合成不可混淆的纯 assessment。"""

    def assess_engineering_safety(
        self,
        *,
        policy: Phase16QualificationPolicy,
        corpus: QualificationCorpusIdentity,
        metric_facts: Sequence[QualificationMetricFact],
    ) -> EngineeringSafetyAssessment:
        """只以预注册的 hard-safety metrics 证明确定性 conformance，不查看模型 E2E 成绩。"""

        if corpus.policy_digest != policy.policy_digest:
            raise ValueError("engineering safety corpus policy identity is invalid")
        metrics = {item.metric_code: item for item in metric_facts}
        if len(metrics) != len(metric_facts):
            raise ValueError("engineering safety metrics must be unique")
        failures = tuple(
            f"{requirement}_FAILED"
            for requirement in policy.hard_safety_requirements
            if (fact := metrics.get(requirement)) is None or fact.numerator != fact.denominator
        )
        status = (
            QualificationAssessmentStatus.ENGINEERING_SAFETY_CONFORMANCE
            if not failures
            else QualificationAssessmentStatus.NOT_QUALIFIED
        )
        payload = {
            "policy_digest": policy.policy_digest,
            "corpus_digest": corpus.corpus_digest,
            "status": status.value,
            "reason_codes": list(failures) or ["ENGINEERING_SAFETY_CONFORMANCE_COMPLETE"],
            "metric_digests": [item.metric_digest for item in sorted(metric_facts, key=lambda item: item.metric_code)],
        }
        return EngineeringSafetyAssessment(
            policy_digest=policy.policy_digest or "",
            corpus_digest=corpus.corpus_digest,
            status=status,
            reason_codes=tuple(payload["reason_codes"]),
            metric_facts=tuple(sorted(metric_facts, key=lambda item: item.metric_code)),
            assessment_digest=canonical_json_sha256(payload),
        )

    def assess_holdout_batches(
        self,
        *,
        policy: Phase16QualificationPolicy,
        corpus: QualificationCorpusIdentity,
        candidate: QualificationCandidate,
        batches: Sequence[QualificationAssessment],
    ) -> HoldoutQualificationAssessment:
        """仅当两个预注册 batch 都 15/15 时，才聚合为最终 30/30 holdout qualification。"""

        if corpus.policy_digest != policy.policy_digest or candidate.policy_digest != policy.policy_digest:
            raise ValueError("holdout aggregation parent identity is invalid")
        expected_indexes = set(range(1, policy.holdout_batch_count + 1))
        indexes = {item.batch_index for item in batches}
        parent_match = all(
            item.policy_digest == policy.policy_digest
            and item.corpus_digest == corpus.corpus_digest
            and item.candidate_digest == candidate.candidate_digest
            and item.campaign_kind is QualificationCampaignKind.HOLDOUT
            and item.status is QualificationAssessmentStatus.HOLDOUT_BATCH_PASS
            for item in batches
        )
        total_numerator = sum(
            next(metric.numerator for metric in item.metric_facts if metric.metric_code == _REQUIRED_E2E_METRIC)
            for item in batches
        )
        total_denominator = sum(
            next(metric.denominator for metric in item.metric_facts if metric.metric_code == _REQUIRED_E2E_METRIC)
            for item in batches
        )
        failures: list[str] = []
        if len(batches) != policy.holdout_batch_count or indexes != expected_indexes:
            failures.append("HOLDOUT_BATCH_SET_INCOMPLETE")
        if not parent_match:
            failures.append("HOLDOUT_BATCH_IDENTITY_OR_PERFORMANCE_INVALID")
        if total_numerator != policy.holdout_required_e2e_pass_count or total_denominator != policy.holdout_high_conflict_case_count:
            failures.append("HOLDOUT_E2E_THRESHOLD_NOT_MET")
        status = (
            QualificationAssessmentStatus.HOLDOUT_QUALIFICATION
            if not failures
            else QualificationAssessmentStatus.NOT_QUALIFIED
        )
        payload = {
            "policy_digest": policy.policy_digest,
            "corpus_digest": corpus.corpus_digest,
            "candidate_digest": candidate.candidate_digest,
            "status": status.value,
            "reason_codes": failures or ["HOLDOUT_QUALIFICATION_COMPLETE"],
            "batch_assessment_digests": sorted(item.assessment_digest for item in batches),
            "aggregate_e2e_numerator": total_numerator,
            "aggregate_e2e_denominator": total_denominator,
        }
        return HoldoutQualificationAssessment(
            policy_digest=policy.policy_digest or "",
            corpus_digest=corpus.corpus_digest,
            candidate_digest=candidate.candidate_digest or "",
            status=status,
            reason_codes=tuple(payload["reason_codes"]),
            batch_assessment_digests=tuple(payload["batch_assessment_digests"]),
            aggregate_e2e_numerator=total_numerator,
            aggregate_e2e_denominator=total_denominator,
            assessment_digest=canonical_json_sha256(payload),
        )

    def assess(
        self,
        *,
        campaign: QualificationCampaign,
        policy: Phase16QualificationPolicy,
        corpus: QualificationCorpusIdentity,
        candidate: QualificationCandidate,
        metric_facts: Sequence[QualificationMetricFact],
        release_verified: bool = False,
    ) -> QualificationAssessment:
        if campaign.policy_digest != policy.policy_digest or campaign.corpus_digest != corpus.corpus_digest:
            raise ValueError("qualification assessment campaign identity is invalid")
        if campaign.candidate_digest != candidate.candidate_digest or candidate.policy_digest != policy.policy_digest:
            raise ValueError("qualification assessment candidate identity is invalid")
        metrics = {item.metric_code: item for item in metric_facts}
        if len(metrics) != len(metric_facts):
            raise ValueError("qualification assessment metrics must be unique")
        if any(item.run_id == "" for item in metric_facts):
            raise ValueError("qualification assessment metric run identity is invalid")
        reasons: list[str] = []
        safety = metrics.get(_REQUIRED_SAFETY_METRIC)
        e2e = metrics.get(_REQUIRED_E2E_METRIC)
        if safety is None or safety.numerator != safety.denominator:
            reasons.append("HARD_SAFETY_CONFORMANCE_FAILED")
        for required in policy.semantic_metric_requirements:
            fact = metrics.get(required)
            if fact is None or fact.numerator != fact.denominator:
                reasons.append(f"{required}_FAILED")
        required_e2e_count = (
            policy.holdout_high_conflict_cases_per_batch
            if campaign.campaign_kind is QualificationCampaignKind.HOLDOUT
            else policy.validation_required_e2e_pass_count
        )
        if e2e is None or e2e.denominator != required_e2e_count or e2e.numerator != required_e2e_count:
            reasons.append("E2E_PERFORMANCE_THRESHOLD_NOT_MET")
        if campaign.campaign_kind is QualificationCampaignKind.HOLDOUT and (
            not release_verified
            or corpus.holdout_release_state not in {HoldoutReleaseState.COMMITTED, HoldoutReleaseState.RELEASED}
            or corpus.holdout_commitment_digest is None
        ):
            reasons.append("HOLDOUT_RELEASE_NOT_VERIFIED")
        if reasons:
            status = QualificationAssessmentStatus.NOT_QUALIFIED
            run_status = QualificationRunStatus.FAILED
        elif campaign.campaign_kind is QualificationCampaignKind.DEVELOPMENT:
            status = QualificationAssessmentStatus.DEVELOPMENT_DIAGNOSTIC
            run_status = QualificationRunStatus.PASS
        elif campaign.campaign_kind is QualificationCampaignKind.VALIDATION:
            status = QualificationAssessmentStatus.VALIDATION_PERFORMANCE
            run_status = QualificationRunStatus.PASS
        else:
            status = QualificationAssessmentStatus.HOLDOUT_BATCH_PASS
            run_status = QualificationRunStatus.PASS
        payload = {
            "campaign_id": campaign.campaign_id,
            "policy_digest": policy.policy_digest,
            "corpus_digest": corpus.corpus_digest,
            "candidate_digest": candidate.candidate_digest,
            "campaign_kind": campaign.campaign_kind.value,
            "batch_index": campaign.batch_index,
            "status": status.value,
            "run_status": run_status.value,
            "reason_codes": sorted(set(reasons)) or [f"{status.value}_COMPLETE"],
            "metric_digests": [item.metric_digest for item in sorted(metric_facts, key=lambda item: item.metric_code)],
        }
        return QualificationAssessment(
            campaign_id=campaign.campaign_id,
            policy_digest=policy.policy_digest or "",
            corpus_digest=corpus.corpus_digest,
            candidate_digest=candidate.candidate_digest or "",
            campaign_kind=campaign.campaign_kind,
            batch_index=campaign.batch_index,
            status=status,
            run_status=run_status,
            reason_codes=tuple(payload["reason_codes"]),
            metric_facts=tuple(sorted(metric_facts, key=lambda item: item.metric_code)),
            assessment_digest=canonical_json_sha256(payload),
        )
