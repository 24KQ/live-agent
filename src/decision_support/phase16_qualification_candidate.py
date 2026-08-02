"""Phase 16 qualification candidate 的受限 Analyst/Planner 输出契约。

V8 的 Profile、Manifest 和真实 receipt 均为历史事实，不能修改。本模块创建新的 candidate
Profile identity，只改善模型遵守已有 Schema/semantic guardrail 的可执行指令；不放宽任何
Schema、风险覆盖、Evidence binding、权限、预算、温度或重试限制。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.decision_support.multi_agent import (
    _SMOKE_V2_CONFLICT_ANALYSIS_RESULT_SCHEMA,
    _SMOKE_V2_LIVE_DECISION_PLANNING_RESULT_SCHEMA,
    _build_profile,
)
from src.decision_support.models import ConflictRiskCode
from src.specialist_runtime.models import SpecialistTaskKind
from src.specialist_runtime.profiles import FinalEvidenceBindingMode, SpecialistProfile


PHASE16_QUALIFICATION_PROFILE_VERSION = "2.0.0"
PHASE16_QUALIFICATION_ANALYST_PROFILE_ID = "phase16-qualification-v2-evidence-analyst"
PHASE16_QUALIFICATION_PLANNER_PROFILE_ID = "phase16-qualification-v2-decision-planner"
PHASE16_QUALIFICATION_MAX_EXPLANATION_CHARACTERS = 360
PHASE16_QUALIFICATION_STAGE_RESERVATION_CNY = "0.100000"
PHASE16_QUALIFICATION_MAX_TOTAL_TOKENS = 8000
PHASE16_QUALIFICATION_MAX_OUTPUT_TOKENS = 2800
#: 整条渠道链的总 deadline 兜底(秒)。每尝试的独立窗口由 V5 adapter 固定为 90s
#: (3 渠道 × 2 尝试 × 90s = 540s 上限),此处只作罕见总上限,不参与正常窗口计算。
PHASE16_QUALIFICATION_DEADLINE_SECONDS = 600

#: Phase 17 契约身份固定的 per-attempt deadline（与 identity_requirements 一致）。
PHASE17_HOLDOUT_DEADLINE_SECONDS = 90

_ANALYST_PROMPT_PREFIX = (
    "You are EvidenceAnalystAgent for a controlled, auditable qualification. "
    "你只能分析给定证据；不得提出经营动作、调用 Skill、选择路由、执行命令或声明权限。"
    "finding_codes 与完整 EvidenceRef 均由系统管理，禁止输出它们；evidence_ids 只能选择"
    "输入证据包中可见的 ID。只输出一个 JSON 对象，不得输出 Markdown、代码块、前缀或推理过程。"
    "在输出前逐项核对 constraint_codes、risk_codes 和 evidence_ids 都是 JSON Schema 允许的值。"
    f"explanation 必须是一段简明、基于证据的说明，最多 {PHASE16_QUALIFICATION_MAX_EXPLANATION_CHARACTERS} "
    "个 Unicode 字符；不得用冗长复述、列表或尾随空白补充说明。500 字符 Schema 上限仍然"
    "是不可放宽的最终拒绝边界。无真实数据的形状示例："
    '{"kind":"FINAL","final_output":{"constraint_codes":[],"risk_codes":[],"explanation":"brief evidence-grounded explanation","evidence_ids":["bundle-evidence-id"]}}. '
)

_PLANNER_PROMPT_PREFIX = (
    "You are DecisionPlannerAgent for a controlled, auditable qualification. "
    "只返回一到三个供人工审阅的受限 option；不得调用 Skill、选择路由、执行命令、"
    "写入 Store 或声称权限。每个 evidence_ids 只能选择输入证据包内可见的 ID。"
    "只输出一个 JSON 对象，不得输出 Markdown、代码块、前缀或推理过程。"
    "每个 option 都必须执行下列不可省略的逐项检查：先把输入 analysis.risk_codes 的每个"
    "字符串逐字复制到该 option.risk_flags；然后确认没有漏项、概括、替换或只在另一 option 中出现。"
    "每个 option 还必须含 HUMAN_CONFIRMATION_REQUIRED，因为 option 永远只供人工确认，"
    "不得自动执行。若 product_strategy 为 SWITCH_TO_BACKUP，必须提供输入证据中确实可用的"
    "backup_product_id，并额外含 BACKUP_PRODUCT_REQUIRES_CONFIRMATION；其他策略的"
    "backup_product_id 必须为 null。risk_flags 上限为 8，且闭合枚举恰有 8 个值，"
    "故完整覆盖始终可表达。无真实数据的形状示例（risk_flags 仅为占位；实际必须按输入"
    "analysis.risk_codes 逐项计算）："
    '{"kind":"FINAL","final_output":{"options":[{"option_id":"placeholder-option","product_strategy":"HOLD_AND_ESCALATE","backup_product_id":null,"host_prompt":"placeholder text","timing":"AFTER_OPERATOR_CONFIRMATION","risk_flags":["HUMAN_CONFIRMATION_REQUIRED","SIDE_EFFECT_UNKNOWN"],"evidence_ids":["bundle-evidence-id"]}]}}. '
)


def _qualification_profile(
    *,
    profile_id: str,
    task_kind: SpecialistTaskKind,
    prompt_prefix: str,
    result_schema: dict[str, object],
    deadline_seconds: int = PHASE16_QUALIFICATION_DEADLINE_SECONDS,
    max_case_cost_cny: str = PHASE16_QUALIFICATION_STAGE_RESERVATION_CNY,
) -> SpecialistProfile:
    """沿用正式 Profile builder 的协议、权限、温度、模型和系统管理 Evidence ID 边界。

    deadline_seconds / max_case_cost_cny 参数化：v2 保持 600s/0.1 兜底口径；
    Phase 17 契约身份固定为 90s/0.1（identity_requirements 冻结值）。
    """

    from decimal import Decimal

    return _build_profile(
        profile_id=profile_id,
        profile_version=PHASE16_QUALIFICATION_PROFILE_VERSION,
        task_kind=task_kind,
        prompt_prefix=prompt_prefix,
        result_schema=result_schema,
        max_total_tokens=PHASE16_QUALIFICATION_MAX_TOTAL_TOKENS,
        max_output_tokens=PHASE16_QUALIFICATION_MAX_OUTPUT_TOKENS,
        max_case_cost_cny=Decimal(max_case_cost_cny),
        deadline_seconds=deadline_seconds,
        model_id="gpt-5.6-luna",
        endpoint_host="synapse-ai.uk",
        final_envelope_instruction='FINAL envelope: {"kind":"FINAL","final_output":<RESULT>}. ',
        final_evidence_binding_mode=FinalEvidenceBindingMode.SYSTEM_MANAGED_IDS,
    )


def build_phase16_qualification_analyst_profile() -> SpecialistProfile:
    """构造 V8 之后的新 Analyst candidate，不改变 500 字符的拒绝边界。"""

    return _qualification_profile(
        profile_id=PHASE16_QUALIFICATION_ANALYST_PROFILE_ID,
        task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
        prompt_prefix=_ANALYST_PROMPT_PREFIX,
        result_schema=_SMOKE_V2_CONFLICT_ANALYSIS_RESULT_SCHEMA,
    )


def build_phase16_qualification_planner_profile() -> SpecialistProfile:
    """构造 V8 之后的新 Planner candidate，强制逐项核对 Analyst 风险覆盖。"""

    return _qualification_profile(
        profile_id=PHASE16_QUALIFICATION_PLANNER_PROFILE_ID,
        task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING,
        prompt_prefix=_PLANNER_PROMPT_PREFIX,
        result_schema=_SMOKE_V2_LIVE_DECISION_PLANNING_RESULT_SCHEMA,
    )


def build_phase17_holdout_profiles() -> tuple[SpecialistProfile, SpecialistProfile]:
    """Phase 17 契约身份专用 profiles（codex 第十七轮 P0-2 全链身份绑定）。

    与 v2 candidate 使用完全相同的 Prompt/Schema 内容（防止语义漂移），但
    deadline / 阶段预留对齐 Phase 17 契约 identity_requirements 冻结值
    （per_attempt_deadline_seconds=90、max_case_cost_cny=0.100000）；
    v2 的 600s 兜底 deadline 不满足 phase17 身份，runner 构造会 fail-closed。
    """

    return (
        _qualification_profile(
            profile_id="phase17-holdout-v1-evidence-analyst",
            task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
            prompt_prefix=_ANALYST_PROMPT_PREFIX,
            result_schema=_SMOKE_V2_CONFLICT_ANALYSIS_RESULT_SCHEMA,
            deadline_seconds=PHASE17_HOLDOUT_DEADLINE_SECONDS,
            max_case_cost_cny=PHASE16_QUALIFICATION_STAGE_RESERVATION_CNY,
        ),
        _qualification_profile(
            profile_id="phase17-holdout-v1-decision-planner",
            task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING,
            prompt_prefix=_PLANNER_PROMPT_PREFIX,
            result_schema=_SMOKE_V2_LIVE_DECISION_PLANNING_RESULT_SCHEMA,
            deadline_seconds=PHASE17_HOLDOUT_DEADLINE_SECONDS,
            max_case_cost_cny=PHASE16_QUALIFICATION_STAGE_RESERVATION_CNY,
        ),
    )


def qualification_adapter_digest(*, repository_root: Path) -> str:
    """把实际发送 Adapter 的源文件摘要绑定进 candidate，防止换实现仍复用 profile 成绩。"""

    path = repository_root / "src" / "decision_support" / "controlled_e2e_adapter_v5.py"
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise ValueError("qualification adapter source must be UTF-8 LF without BOM")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("qualification adapter source is not valid UTF-8") from exc
    return hashlib.sha256(raw).hexdigest()


def required_risk_code_vocabulary() -> frozenset[str]:
    """公开闭合风险词表，供测试确认 Checklist 不要求模型生成不可表达的新码。"""

    return frozenset(item.value for item in ConflictRiskCode)
