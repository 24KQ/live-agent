"""Phase 15 Task 7 双轨 Promotion 决策与稳定报告。

报告层只消费已经持久化的 TechnicalReleaseDecision、Copilot smoke 报告和真人
Study evidence，不执行模型、不写数据库，也不能把缺失外部证据改写成启用状态。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from src.release_gates.copilot_smoke import CopilotSmokeReport, CopilotSmokeStatus
from src.release_gates.decisions import (
    DecisionSupportPromotionDecision,
    FinalReleaseDecision,
    FinalReleaseStatus,
    PromotionStatus,
    TechnicalReleaseDecision,
    TechnicalReleaseStatus,
)
from src.release_gates.human_study import StudyEvidence, StudyEvidenceStatus
from src.specialist_runtime.models import StrictFrozenModel, canonical_json_sha256


HASH_PATTERN = r"^[0-9a-f]{64}$"


class ReleaseReport(StrictFrozenModel):
    """三份结论和摘要绑定在一起的不可变 Release 报告。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    release_run_id: str = Field(..., min_length=1)
    technical: TechnicalReleaseDecision
    promotion: DecisionSupportPromotionDecision
    final: FinalReleaseDecision
    report_digest: str = ""

    @model_validator(mode="after")
    def _bind_report_identity(self) -> "ReleaseReport":
        """确保报告主体、Final run ID 和摘要不能被报告渲染层静默替换。"""

        if self.final.release_run_id != self.release_run_id:
            raise ValueError("final release run does not match report")
        payload = self.model_dump(mode="json", exclude={"report_digest"})
        calculated = canonical_json_sha256(payload)
        if self.report_digest and self.report_digest != calculated:
            raise ValueError("report_digest does not match release report facts")
        object.__setattr__(self, "report_digest", calculated)
        return self


def build_promotion_decision(
    *,
    model_report: CopilotSmokeReport | None,
    human_evidence: StudyEvidence | None,
    safety_correctness: Decimal = Decimal("0"),
    key_conflict_miss_rate_reduction: Decimal = Decimal("0"),
    decision_median_reduction: Decimal = Decimal("0"),
) -> DecisionSupportPromotionDecision:
    """把模型/真人事实和严格指标编译成唯一 Promotion 状态。"""

    reasons: list[str] = []
    model_complete = False
    human_complete = False
    completed_cases = 0
    severe_count = 0
    if model_report is None:
        reasons.append("MODEL_EVIDENCE_MISSING")
    else:
        completed_cases = model_report.model_call_count + model_report.duplicate_request_count
        severe_count = model_report.severe_violation_count
        model_complete = (
            model_report.status is CopilotSmokeStatus.PASS
            and model_report.promotion_eligible
            and completed_cases == 10
            and model_report.unknown_usage_count == 0
            and model_report.fallback_count == 0
            and model_report.schema_error_count == 0
            and severe_count == 0
        )
        if not model_complete:
            reasons.append("MODEL_EVIDENCE_INCOMPLETE")
    if human_evidence is None:
        reasons.append("HUMAN_EVIDENCE_MISSING")
    else:
        human_complete = human_evidence.status is StudyEvidenceStatus.READY
        if not human_complete:
            reasons.extend(human_evidence.reason_codes or ("HUMAN_EVIDENCE_INCOMPLETE",))
    if not model_complete or not human_complete:
        return DecisionSupportPromotionDecision(
            status=PromotionStatus.BLOCKED,
            reason_codes=tuple(sorted(set(reasons))),
            model_evidence_complete=model_complete,
            human_evidence_complete=human_complete,
            completed_smoke_cases=completed_cases,
            severe_violation_count=severe_count,
            safety_correctness=safety_correctness,
            key_conflict_miss_rate_reduction=key_conflict_miss_rate_reduction,
            decision_median_reduction=decision_median_reduction,
        )
    meets_quality = (
        safety_correctness >= Decimal("0.90")
        and key_conflict_miss_rate_reduction >= Decimal("0.30")
        and decision_median_reduction >= Decimal("0.20")
        and severe_count == 0
    )
    return DecisionSupportPromotionDecision(
        status=PromotionStatus.PROMOTE if meets_quality else PromotionStatus.KEEP_DISABLED,
        reason_codes=() if meets_quality else ("STRICT_AND_GATE_FAILED",),
        model_evidence_complete=True,
        human_evidence_complete=True,
        completed_smoke_cases=completed_cases,
        severe_violation_count=severe_count,
        safety_correctness=safety_correctness,
        key_conflict_miss_rate_reduction=key_conflict_miss_rate_reduction,
        decision_median_reduction=decision_median_reduction,
    )


def build_release_report(
    *,
    technical: TechnicalReleaseDecision,
    promotion: DecisionSupportPromotionDecision,
) -> ReleaseReport:
    """按 Technical 优先规则生成唯一 FinalReleaseDecision。"""

    final_status = (
        FinalReleaseStatus.NOT_RELEASED
        if technical.status is not TechnicalReleaseStatus.PASS
        else FinalReleaseStatus.RELEASED_DECISION_SUPPORT_ENABLED
        if promotion.status is PromotionStatus.PROMOTE
        else FinalReleaseStatus.RELEASED_DECISION_SUPPORT_DISABLED
    )
    final = FinalReleaseDecision(
        release_run_id=technical.release_run_id,
        technical_status=technical.status,
        promotion_status=promotion.status,
        status=final_status,
        reason_codes=tuple(technical.reason_codes) + tuple(promotion.reason_codes),
    )
    return ReleaseReport(
        release_run_id=technical.release_run_id,
        technical=technical,
        promotion=promotion,
        final=final,
    )


def render_release_report_json(report: ReleaseReport) -> str:
    """以排序 key 和紧凑 JSON 输出稳定报告，不引入当前时间。"""

    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def render_release_report_markdown(report: ReleaseReport) -> str:
    """输出面向审计的固定 Markdown，不加入不可复现的环境信息。"""

    return (
        "# Phase 15 Release Report\n\n"
        f"- Report digest: `{report.report_digest}`\n"
        f"- Release run: `{report.release_run_id}`\n"
        f"- Technical status: `{report.technical.status.value}`\n"
        f"- Promotion status: `{report.promotion.status.value}`\n"
        f"- Final status: `{report.final.status.value}`\n"
    )
