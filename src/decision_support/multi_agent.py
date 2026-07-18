"""Phase 16 受控双 Agent 的启动冻结 Profile 协议。

本模块在 Task 3 只负责固定模型输入/输出边界与预算身份；Task 5 才会在同一模块中加入
确定性升级选择器和协调器。这里没有 Store、Skill、命令或网络调用能力。
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json

from src.decision_support.models import (
    ConflictAnalysisCode,
    ConflictConstraintCode,
    ConflictRiskCode,
)
from src.specialist_runtime.models import (
    EvidenceKind,
    SpecialistTaskKind,
    canonical_json_sha256,
)
from src.specialist_runtime.profiles import (
    FORMAL_ENDPOINT_HOST,
    FORMAL_MODEL_ID,
    SpecialistProfile,
)


EVIDENCE_ANALYST_PROFILE_ID = "evidence_analyst"
DECISION_PLANNER_PROFILE_ID = "decision_planner"
CONTROLLED_MULTI_AGENT_PROFILE_VERSION = "1.0.0"


# Agent 只能返回引用身份，不接收正文解析、自由工具参数或任意额外字段。
_EVIDENCE_REF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"enum": [kind.value for kind in EvidenceKind]},
        "evidence_id": {"type": "string", "minLength": 1},
        "source_version": {"type": "string", "minLength": 1},
        "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "anchor_id": {"type": ["string", "null"]},
        "room_id": {"type": ["string", "null"]},
    },
    "required": [
        "kind",
        "evidence_id",
        "source_version",
        "digest",
        "anchor_id",
        "room_id",
    ],
}


# Analyst 不能输出商品排序、策略、Prompt、Skill 或执行字段；Coordinator 才会补齐父事实。
_CONFLICT_ANALYSIS_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "finding_codes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "uniqueItems": True,
            "items": {"enum": [code.value for code in ConflictAnalysisCode]},
        },
        "constraint_codes": {
            "type": "array",
            "maxItems": 3,
            "uniqueItems": True,
            "items": {"enum": [code.value for code in ConflictConstraintCode]},
        },
        "risk_codes": {
            "type": "array",
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"enum": [code.value for code in ConflictRiskCode]},
        },
        "explanation": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            # 标准 JSON Schema 无法便携表示 Unicode category C；先拒绝前后 Unicode
            # 空白和 ASCII 控制字符，Pydantic 再对全部 C 类做最终 fail-closed 校验。
            "pattern": "^(?!\\s)(?!.*\\s$)[^\\x00-\\x1F\\x7F]+$",
        },
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": _EVIDENCE_REF_SCHEMA,
        },
    },
    "required": [
        "finding_codes",
        "constraint_codes",
        "risk_codes",
        "explanation",
        "evidence_refs",
    ],
}


# Planner 只返回候选 option；升级、分析、Bundle、Profile、时间和最终 Proposal ID
# 均由确定性 Coordinator 注入，防止模型伪造上游事实或取得路由控制权。
_LIVE_DECISION_PLANNING_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "options": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "option_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 80,
                        "pattern": "^[a-z0-9][a-z0-9-]*$",
                    },
                    "product_strategy": {
                        "enum": [
                            "KEEP_CURRENT",
                            "SWITCH_TO_BACKUP",
                            "HOLD_AND_ESCALATE",
                            "REPLY_DANMAKU",
                        ]
                    },
                    "backup_product_id": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "host_prompt": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 300,
                        # 与上方 explanation 相同：Schema 预筛可表达的展示危险字符，
                        # 领域 Pydantic 模型保留 Unicode category C 的完整权威检查。
                        "pattern": "^(?!\\s)(?!.*\\s$)[^\\x00-\\x1F\\x7F]+$",
                    },
                    "timing": {
                        "enum": [
                            "NOW",
                            "NEXT_BEAT",
                            "AFTER_OPERATOR_CONFIRMATION",
                            "AFTER_RECONCILIATION",
                        ]
                    },
                    "risk_flags": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "uniqueItems": True,
                        "items": {
                            "enum": [code.value for code in ConflictRiskCode]
                        },
                    },
                    "evidence_refs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 12,
                        "items": _EVIDENCE_REF_SCHEMA,
                    },
                },
                "required": [
                    "option_id",
                    "product_strategy",
                    "backup_product_id",
                    "host_prompt",
                    "timing",
                    "risk_flags",
                    "evidence_refs",
                ],
                # JSON Schema 先筛除备品策略与备品 ID 的明显矛盾；后续 Pydantic 仍会
                # 执行相同语义和更严格的 Unicode 控制字符检查，不让模型输出走旁路。
                "allOf": [
                    {
                        "if": {
                            "properties": {
                                "product_strategy": {"const": "SWITCH_TO_BACKUP"}
                            }
                        },
                        "then": {
                            "properties": {
                                "backup_product_id": {"type": "string", "minLength": 1}
                            }
                        },
                        "else": {
                            "properties": {"backup_product_id": {"type": "null"}}
                        },
                    }
                ],
            },
        }
    },
    "required": ["options"],
}


def _build_profile(
    *,
    profile_id: str,
    task_kind: SpecialistTaskKind,
    prompt_prefix: str,
    result_schema: dict[str, object],
    max_total_tokens: int,
    max_case_cost_cny: Decimal,
) -> SpecialistProfile:
    """统一构造温度零、单次调用、零 Skill 和两秒 deadline 的精确 Profile。"""

    # Runner 先解析 AgentAction，再只对 FINAL 的 final_output 校验 result_schema；Prompt
    # 必须同时固定两层形状，否则模型即使遵守结果 Schema 也会被 Runner 判为 INVALID_ACTION。
    prompt_text = (
        prompt_prefix
        + " Return exactly one AgentAction FINAL envelope and no markdown or reasoning. "
        + 'FINAL envelope: {"kind":"FINAL","final_output":<RESULT>,"evidence_refs":[<EvidenceRef>]}. '
        + "The final_output must match this RESULT JSON Schema: "
        + json.dumps(result_schema, sort_keys=True, separators=(",", ":"))
    )
    return SpecialistProfile(
        profile_id=profile_id,
        profile_version=CONTROLLED_MULTI_AGENT_PROFILE_VERSION,
        task_kind=task_kind,
        model_id=FORMAL_MODEL_ID,
        endpoint_host=FORMAL_ENDPOINT_HOST,
        temperature=Decimal("0"),
        prompt_text=prompt_text,
        prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        result_schema_hash=canonical_json_sha256(result_schema),
        result_schema=result_schema,
        allowed_skill_ids=(),
        skill_versions={},
        max_model_calls=1,
        max_skill_calls=0,
        max_total_tokens=max_total_tokens,
        deadline_seconds=2,
        max_case_cost_cny=max_case_cost_cny,
    )


def build_evidence_analyst_profile() -> SpecialistProfile:
    """返回只读 ConflictAnalysis Profile，预算固定为 2 秒、1200 token、0.03 CNY。"""

    return _build_profile(
        profile_id=EVIDENCE_ANALYST_PROFILE_ID,
        task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
        prompt_prefix=(
            "You are EvidenceAnalystAgent for a live-commerce conflict. "
            "Do not rank products, propose actions, call Skills, or claim authority."
        ),
        result_schema=_CONFLICT_ANALYSIS_RESULT_SCHEMA,
        max_total_tokens=1200,
        max_case_cost_cny=Decimal("0.030000"),
    )


def build_decision_planner_profile() -> SpecialistProfile:
    """返回只读 Planner Profile，预算固定为 2 秒、2800 token、0.07 CNY。"""

    return _build_profile(
        profile_id=DECISION_PLANNER_PROFILE_ID,
        task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING,
        prompt_prefix=(
            "You are DecisionPlannerAgent for a human-operated live-commerce incident. "
            "Return options only; never call Skills, select a route, or execute a command."
        ),
        result_schema=_LIVE_DECISION_PLANNING_RESULT_SCHEMA,
        max_total_tokens=2800,
        max_case_cost_cny=Decimal("0.070000"),
    )
