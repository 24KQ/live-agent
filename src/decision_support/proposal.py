"""Phase 14 播中 Copilot 的封闭方案协议。

本模块只表达可供运营比较的结构化建议，不表达审批、SkillCall、PlanCommand 或
任何自动经营授权。方案中的自然语言仅是主播提示展示内容，不能被下游当作执行指令。
"""

from __future__ import annotations

from enum import StrEnum
import unicodedata

from pydantic import ConfigDict, Field, field_validator, model_validator

from src.decision_support.models import MultiAgentProposalLineage
from src.specialist_runtime.models import EvidenceRef, StrictFrozenModel


class ProposalStatus(StrEnum):
    """Copilot 对外暴露的两个安全终态。"""

    READY = "READY"
    DEGRADED = "DEGRADED"


class ProposalOrigin(StrEnum):
    """区分兼容单 Copilot 与必须携带上游事实的受控双 Agent 方案。"""

    SINGLE_COPILOT = "SINGLE_COPILOT"
    MULTI_AGENT = "MULTI_AGENT"


class ProductStrategy(StrEnum):
    """运营可比较的封闭商品策略，不包含执行动作。"""

    KEEP_CURRENT = "KEEP_CURRENT"
    SWITCH_TO_BACKUP = "SWITCH_TO_BACKUP"
    HOLD_AND_ESCALATE = "HOLD_AND_ESCALATE"
    REPLY_DANMAKU = "REPLY_DANMAKU"


class DecisionTiming(StrEnum):
    """建议时机只表示人工决策上下文，不表示自动调度。"""

    NOW = "NOW"
    NEXT_BEAT = "NEXT_BEAT"
    AFTER_OPERATOR_CONFIRMATION = "AFTER_OPERATOR_CONFIRMATION"
    AFTER_RECONCILIATION = "AFTER_RECONCILIATION"


# 风险字段是工作台与审计报表之间的稳定协议。模型不能通过任意大写字符串
# 临时扩展风险语义；新增风险必须先修改版本化协议、Schema 和对应验收测试。
LIVE_DECISION_RISK_FLAGS = frozenset(
    {
        "BACKUP_PRODUCT_REQUIRES_CONFIRMATION",
        "DANMAKU_HIGH_NOISE",
        "HUMAN_CONFIRMATION_REQUIRED",
        "INVENTORY_CONFLICT_REQUIRES_REVIEW",
        "RECONCILIATION_REQUIRED",
        "RHYTHM_PAUSE_REQUIRED",
        "SIDE_EFFECT_UNKNOWN",
        "STALE_EVIDENCE",
    }
)


def _validate_safe_display_text(value: str) -> str:
    """阻断控制字符，确保提示内容不会伪造协议或污染工作台审计文本。"""

    if value != value.strip() or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError("display text contains unsafe control characters")
    return value


class DecisionOption(StrictFrozenModel):
    """单个封闭建议选项；没有工具名、SQL、参数或执行授权字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    option_id: str = Field(..., min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    product_strategy: ProductStrategy
    backup_product_id: str | None = Field(default=None, min_length=1, max_length=128)
    host_prompt: str = Field(..., min_length=1, max_length=300)
    timing: DecisionTiming
    risk_flags: tuple[str, ...] = Field(..., min_length=1, max_length=8)
    evidence_refs: tuple[EvidenceRef, ...] = Field(..., min_length=1, max_length=12)

    @field_validator("host_prompt")
    @classmethod
    def _prompt_is_display_safe(cls, value: str) -> str:
        return _validate_safe_display_text(value)

    @field_validator("risk_flags")
    @classmethod
    def _risk_flags_are_closed(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not flag or flag != flag.upper() or any(not character.isalnum() and character != "_" for character in flag)
            for flag in value
        ):
            raise ValueError("risk_flags must be uppercase codes")
        if any(flag not in LIVE_DECISION_RISK_FLAGS for flag in value):
            raise ValueError("risk_flags contains an unknown code")
        if len(value) != len(set(value)):
            raise ValueError("risk_flags must be unique")
        return value

    @model_validator(mode="after")
    def _strategy_requires_explicit_backup(self) -> "DecisionOption":
        if self.product_strategy is ProductStrategy.SWITCH_TO_BACKUP and not self.backup_product_id:
            raise ValueError("SWITCH_TO_BACKUP requires backup_product_id")
        if self.product_strategy is not ProductStrategy.SWITCH_TO_BACKUP and self.backup_product_id:
            raise ValueError("backup_product_id is only allowed for SWITCH_TO_BACKUP")
        return self


class LiveDecisionProposal(StrictFrozenModel):
    """绑定单一 EvidenceBundle 的可审计播中方案或降级事实摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str = Field(..., min_length=1, max_length=128)
    live_session_id: str = Field(..., min_length=1)
    incident_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    evidence_bundle_id: str = Field(..., min_length=1)
    evidence_bundle_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    proposal_origin: ProposalOrigin = ProposalOrigin.SINGLE_COPILOT
    status: ProposalStatus
    options: tuple[DecisionOption, ...] = Field(default=(), max_length=3)
    evidence_refs: tuple[EvidenceRef, ...] = Field(..., min_length=1, max_length=12)
    fact_summary: str | None = Field(default=None, max_length=1000)
    degraded_reason: str | None = Field(default=None, min_length=1, max_length=80)
    multi_agent_lineage: MultiAgentProposalLineage | None = None

    @field_validator("fact_summary")
    @classmethod
    def _summary_is_display_safe(cls, value: str | None) -> str | None:
        return None if value is None else _validate_safe_display_text(value)

    @model_validator(mode="after")
    def _close_proposal_state(self) -> "LiveDecisionProposal":
        if self.status is ProposalStatus.READY:
            if not 1 <= len(self.options) <= 3:
                raise ValueError("READY proposal requires one to three options")
            if self.degraded_reason is not None:
                raise ValueError("READY proposal cannot carry degraded_reason")
            if any(
                tuple(option.evidence_refs) != self.evidence_refs
                for option in self.options
            ):
                raise ValueError("proposal option evidence must close over the full bundle")
        else:
            if self.options:
                raise ValueError("DEGRADED proposal cannot carry options")
            if not self.degraded_reason or not self.fact_summary:
                raise ValueError("DEGRADED proposal requires reason and fact_summary")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("proposal evidence_refs must be unique")
        if self.proposal_origin is ProposalOrigin.MULTI_AGENT:
            # 多 Agent 方案不能借用其他 Bundle 的分析结果；其余父事实由后续 Store 以
            # escalation/analysis append-only 记录验证，避免把查询权限交给模型输出。
            if self.status is not ProposalStatus.READY or self.multi_agent_lineage is None:
                raise ValueError("multi-agent lineage requires READY proposal")
            if self.evidence_bundle_digest is None:
                raise ValueError("multi-agent proposal requires evidence_bundle_digest")
            if (
                self.multi_agent_lineage.evidence_bundle_id
                != self.evidence_bundle_id
            ):
                raise ValueError("proposal lineage evidence_bundle_id does not match proposal")
            if (
                self.multi_agent_lineage.evidence_bundle_digest
                != self.evidence_bundle_digest
            ):
                raise ValueError("proposal lineage evidence_bundle_digest does not match proposal")
            if self.multi_agent_lineage.evidence_refs != self.evidence_refs:
                raise ValueError("proposal lineage evidence_refs do not match proposal")
        elif self.multi_agent_lineage is not None:
            raise ValueError("single-copilot proposal cannot carry multi-agent lineage")
        return self
