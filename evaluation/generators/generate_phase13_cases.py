"""生成 Phase 13 的 240 例脱敏配对数据集及冻结 Manifest。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


SEED = 20260716
CANDIDATES = ("live_ops", "planner", "review_memory")
SPLIT_COUNTS = {"development": 20, "validation": 40, "holdout": 20}
MODEL_ID = "deepseek-v4-flash"
ENDPOINT_HOST = "api.deepseek.com"

PRICING_SNAPSHOT = {
    "citation_excerpt": (
        "1M INPUT TOKENS (CACHE MISS): $0.14; 1M OUTPUT TOKENS: $0.28"
    ),
    "conversion_policy": {
        "policy_version": "usd-cny-fixed-7.2-v1",
        "rounding": "ROUND_HALF_EVEN_TO_6_DECIMALS",
        "usd_to_cny_rate": "7.200000",
    },
    "observed_on": "2026-07-16",
    "official_prices_usd_per_million_tokens": {
        "cache_miss_input": "0.140000",
        "output": "0.280000",
    },
    "source_currency": "USD",
    "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
}

# 执行价格由独立来源快照中的官方美元价和固定汇率派生；Manifest 另行绑定快照原始字节。
PRICING = {
    "cache_miss_input_cny_per_million": "1.008000",
    "cache_miss_input_usd_per_million": "0.140000",
    "conversion_policy_version": "usd-cny-fixed-7.2-v1",
    "currency": "CNY",
    "observed_on": "2026-07-16",
    "output_cny_per_million": "2.016000",
    "output_usd_per_million": "0.280000",
    "source_currency": "USD",
    "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
    "usd_to_cny_rate": "7.200000",
}

PROMPT_OBJECTIVES = {
    "live_ops": "Return one governed live-operations suggestion using only supplied evidence.",
    "planner": "Return a bounded candidate DAG proposal from frozen products and constraints.",
    "review_memory": "Return grounded attribution and staged memory candidates from supplied traces.",
}

RESULT_SCHEMAS = {
    "live_ops": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "reason_code", "suggestion", "evidence_refs"],
        "properties": {
            "action": {
                "enum": [
                    "NO_ACTION",
                    "HUMAN_ATTENTION",
                    "SWITCH_PRODUCT_SUGGESTION",
                    "DANMAKU_REPLY_SUGGESTION",
                ]
            },
            "reason_code": {"type": "string", "minLength": 1},
            "suggestion": {"type": "string", "minLength": 1},
            "evidence_refs": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "evidence_id", "source_version", "digest"],
                    "properties": {
                        "kind": {"enum": ["EVENT", "PLAN", "PLAN_NODE", "SKILL_ATTEMPT", "AUDIT", "REPLAY", "MEMORY", "EVALUATION"]},
                        "evidence_id": {"type": "string", "minLength": 1},
                        "source_version": {"type": "string", "minLength": 1},
                        "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "anchor_id": {"type": "string", "minLength": 1},
                        "room_id": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    },
    "planner": {
        "type": "object",
        "additionalProperties": False,
        "required": ["nodes", "dependencies", "bindings"],
        "properties": {
            "nodes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["logical_key", "capability"],
                    "properties": {
                        "logical_key": {"type": "string", "minLength": 1},
                        "capability": {"enum": ["generate_live_plan", "generate_product_card", "suggest_price_change"]},
                    },
                },
            },
            "dependencies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["from", "to"],
                    "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                },
            },
            "bindings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["target", "source_type", "source"],
                    "properties": {
                        "target": {"type": "string"},
                        "source_type": {"enum": ["PLAN_INPUT", "NODE_OUTPUT", "LITERAL"]},
                        "source": {"type": ["string", "number", "integer", "boolean", "null"]},
                    },
                },
            },
        },
    },
    "review_memory": {
        "type": "object",
        "additionalProperties": False,
        "required": ["attribution", "memory_candidates", "evidence_ids"],
        "properties": {
            "attribution": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "reason_code", "evidence_ids"],
                "properties": {
                    "category": {"enum": ["inventory", "content", "timing"]},
                    "reason_code": {"type": "string", "minLength": 1},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                },
            },
            "memory_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["class", "product_id", "category", "tag", "evidence_ids"],
                    "properties": {
                        "class": {"enum": ["APPLY", "REJECT", "REVIEW"]},
                        "product_id": {"type": "string", "pattern": "^sim-product-"},
                        "category": {"type": "string"},
                        "tag": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                    },
                },
            },
            "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2},
        },
    },
}

PROFILE_LIMITS = {
    "live_ops": {
        "profile_id": "live-ops-agent",
        "profile_version": "1.0.0",
        "max_model_calls": 2,
        "max_skill_calls": 3,
        "max_total_tokens": 4000,
        "deadline_seconds": 5,
        "candidate_budget_cny": "0.60",
        "max_case_cost_cny": "0.030000",
        "allowed_skill_ids": ["aggregate_danmaku_questions", "generate_danmaku_reply", "generate_on_live_prompt", "on_live_context_collect", "recommend_backup_product"],
        "skill_versions": {
            "aggregate_danmaku_questions": "1.0.0",
            "generate_danmaku_reply": "1.0.0",
            "generate_on_live_prompt": "1.0.0",
            "on_live_context_collect": "1.0.0",
            "recommend_backup_product": "1.0.0",
        },
        "current_catalog_availability": {
            "aggregate_danmaku_questions": True,
            "generate_danmaku_reply": True,
            "generate_on_live_prompt": True,
            "on_live_context_collect": True,
            "recommend_backup_product": True,
        },
    },
    "planner": {
        "profile_id": "planner-agent",
        "profile_version": "1.0.0",
        "max_model_calls": 3,
        "max_skill_calls": 0,
        "max_total_tokens": 8000,
        "deadline_seconds": 15,
        "candidate_budget_cny": "1.00",
        "max_case_cost_cny": "0.050000",
        "allowed_skill_ids": [],
        "skill_versions": {},
        "current_catalog_availability": {},
    },
    "review_memory": {
        "profile_id": "review-memory-agent",
        "profile_version": "1.0.0",
        "max_model_calls": 3,
        "max_skill_calls": 4,
        "max_total_tokens": 8000,
        "deadline_seconds": 20,
        "candidate_budget_cny": "0.80",
        "max_case_cost_cny": "0.040000",
        "allowed_skill_ids": ["calculate_post_live_attribution", "collect_post_live_evidence", "stage_memory_candidates"],
        "skill_versions": {
            "calculate_post_live_attribution": "1.0.0",
            "collect_post_live_evidence": "1.0.0",
            "stage_memory_candidates": "1.0.0",
        },
        # 这三个版本是后续切片的计划冻结事实；当前 Catalog 尚未提供，正式执行前必须预检。
        "current_catalog_availability": {
            "calculate_post_live_attribution": False,
            "collect_post_live_evidence": False,
            "stage_memory_candidates": False,
        },
    },
}


def _candidate_prompt(candidate: str) -> str:
    """把真实 AgentAction envelope、权限与结果 Schema 固定进模型可见 Prompt。"""

    result_schema = json.dumps(
        RESULT_SCHEMAS[candidate],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    allowed_skills = json.dumps(
        PROFILE_LIMITS[candidate]["allowed_skill_ids"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "\n".join(
        (
            PROMPT_OBJECTIVES[candidate],
            "Return exactly one AgentAction JSON object and no markdown or reasoning text.",
            'FINAL envelope: {"kind":"FINAL","final_output":<RESULT>,"evidence_refs":[<EvidenceRef>]}.',
            'CALL_SKILL envelope: {"kind":"CALL_SKILL","skill_id":<ID>,"arguments":<OBJECT>,"evidence_refs":[<EvidenceRef>]}.',
            'ABSTAIN envelope: {"kind":"ABSTAIN","reason_code":<CODE>,"evidence_refs":[<EvidenceRef>]}.',
            f"Allowed Skill IDs: {allowed_skills}.",
            f"RESULT JSON Schema: {result_schema}",
        )
    )


def _canonical_bytes(value: Any) -> bytes:
    """统一 JSON 字节表示，摘要与落盘都不依赖平台默认格式。"""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_json(value: Any) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _identity_digest_json(value: Any) -> str:
    """匹配 Runtime 身份模型：紧凑 JSON 不附加文件换行。"""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _digest_bytes(encoded)


def _normalized_source_bytes(path: Path) -> bytes:
    """源码身份统一移除 BOM 并规范为 LF，避免 checkout 换行改变 Manifest。"""

    text = path.read_text(encoding="utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _write_json(path: Path, value: Any) -> None:
    _write(path, _canonical_bytes(value))


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    _write(path, b"".join(_canonical_bytes(record) for record in records))


def _evidence(
    case_id: str,
    index: int,
    *,
    kind: str,
    room_id: str,
    anchor_id: str | None = None,
) -> dict[str, Any]:
    evidence = {
        "kind": kind,
        "evidence_id": f"evidence-{case_id}-{index}",
        "source_version": "1",
        "digest": hashlib.sha256(f"{case_id}:{index}".encode("ascii")).hexdigest(),
        "room_id": room_id,
    }
    if anchor_id is not None:
        evidence["anchor_id"] = anchor_id
    return evidence


def _products(index: int, count: int) -> list[dict[str, Any]]:
    categories = ("beauty", "home", "food")
    return [
        {
            "product_id": f"sim-product-{index:03d}-{offset}",
            "category": categories[(index + offset) % len(categories)],
            "price": f"{29 + index + offset}.90",
            "inventory": 20 + ((index * 7 + offset * 11) % 80),
            "version": 1 + (((index // 7) + offset * 2) % 4),
        }
        for offset in range(1, count + 1)
    ]


def _live_case(case_id: str, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    actions = (
        "NO_ACTION",
        "HUMAN_ATTENTION",
        "SWITCH_PRODUCT_SUGGESTION",
        "DANMAKU_REPLY_SUGGESTION",
    )
    scenario = (index - 1) % len(actions)
    products = _products(index, 2)
    risk_open = scenario in {1, 2}
    backup_available = scenario == 2
    question_count = 12 + (index % 5) if scenario == 3 else index % 7
    if risk_open and backup_available:
        action = "SWITCH_PRODUCT_SUGGESTION"
    elif risk_open:
        action = "HUMAN_ATTENTION"
    elif question_count >= 10:
        action = "DANMAKU_REPLY_SUGGESTION"
    else:
        action = "NO_ACTION"
    room_id = f"sim-room-{(index % 5) + 1:02d}"
    return (
        {
            "room_id": room_id,
            "trace_id": f"trace-{case_id}",
            "evidence_refs": [
                _evidence(case_id, 1, kind="EVENT", room_id=room_id),
                _evidence(case_id, 2, kind="AUDIT", room_id=room_id),
            ],
            "products": products,
            "inventory_alert": {
                "sold_out_product_id": products[0]["product_id"],
                "expected_version": products[0]["version"],
                "risk_open": risk_open,
                "backup_available": backup_available,
            },
            "danmaku": {
                "question_count": question_count,
                "top_intent": ("price", "stock", "usage")[index % 3],
            },
        },
        {
            "expected_action": action,
            "action_success": action != "HUMAN_ATTENTION",
            "incident_recovery": action in {"NO_ACTION", "SWITCH_PRODUCT_SUGGESTION"},
        },
    )


def _planner_case(case_id: str, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    recovery_required = index % 3 == 0
    products = _products(index, 3)
    return (
        {
            "room_id": f"sim-room-{(index % 5) + 1:02d}",
            "anchor_id": f"sim-anchor-{(index % 4) + 1:02d}",
            "products": products,
            "constraints": {
                "max_cards": 3,
                "min_priority": 1 + (index % 3),
                "requires_memory": index % 2 == 0,
            },
            "memories": [
                {"memory_id": f"memory-{index:03d}", "tag": "pace", "version": 1}
            ],
            "current_plan": {
                "version": 1 + (index % 2),
                "failed_product_ids": [products[0]["product_id"]] if recovery_required else [],
            },
        },
        {
            "expected_node_keys": ["PREPARE_CARD_BATCH", "COLLECT_CARD_RESULTS"],
            "executable": True,
            "constraint_recovery_required": recovery_required,
            "constraint_recovery": True,
        },
    )


def _review_case(case_id: str, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    categories = ("inventory", "content", "timing")
    category = categories[(index // 5) % len(categories)]
    conflict = index % 4 == 0
    whitelist_match = index % 5 != 0
    if conflict:
        candidate_class = "REJECT"
    elif whitelist_match:
        candidate_class = "APPLY"
    else:
        candidate_class = "REVIEW"
    product_id = f"sim-product-review-{index:03d}"
    room_id = f"sim-room-{(index % 5) + 1:02d}"
    anchor_id = f"sim-anchor-{(index % 4) + 1:02d}"
    return (
        {
            "room_id": room_id,
            "anchor_id": anchor_id,
            "replay": {
                "trace_id": f"trace-{case_id}",
                "safety_violation_count": 0,
                "dominant_signal": category,
                "evidence_conflict": conflict,
            },
            "decision_traces": [
                _evidence(case_id, 1, kind="AUDIT", room_id=room_id, anchor_id=anchor_id),
                _evidence(case_id, 2, kind="AUDIT", room_id=room_id, anchor_id=anchor_id),
            ],
            "catalog_whitelist": {
                "product_ids": [product_id],
                "categories": [category],
                "tags": ["pace", "inventory"],
            },
            "active_memory": [
                {"memory_id": f"active-{index:03d}", "template_key": "pace_preference"}
            ],
            "candidate_context": {
                "whitelist_match": whitelist_match,
                "independent_trace_count": 2,
            },
        },
        {
            "attribution_category": category,
            "grounded_attribution": True,
            "memory_candidate_class": candidate_class,
            "promotable": candidate_class == "APPLY",
        },
    )


def _case_and_label(
    candidate: str, split: str, index: int, variant_index: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    case_id = f"phase13-{candidate}-{split}-{index:03d}"
    builder = {
        "live_ops": _live_case,
        "planner": _planner_case,
        "review_memory": _review_case,
    }[candidate]
    input_snapshot, label_snapshot = builder(case_id, variant_index)
    case = {
        "case_id": case_id,
        "candidate": candidate,
        "split": split,
        "input": input_snapshot,
    }
    label = {
        "case_id": case_id,
        "candidate": candidate,
        "split": split,
        "label": label_snapshot,
    }
    return case, label


def _schema() -> dict[str, Any]:
    """返回严格顶层 Schema；候选内部快照保持封闭对象。"""

    evidence = {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "evidence_id", "source_version", "digest"],
        "properties": {
            "kind": {"enum": ["EVENT", "PLAN", "PLAN_NODE", "SKILL_ATTEMPT", "AUDIT", "REPLAY", "MEMORY", "EVALUATION"]},
            "evidence_id": {"type": "string", "minLength": 1},
            "source_version": {"type": "string", "minLength": 1},
            "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "anchor_id": {"type": "string", "minLength": 1},
            "room_id": {"type": "string", "minLength": 1},
        },
    }
    product = {
        "type": "object",
        "additionalProperties": False,
        "required": ["product_id", "category", "price", "inventory", "version"],
        "properties": {
            "product_id": {"type": "string", "pattern": "^sim-product-"},
            "category": {"type": "string", "minLength": 1},
            "price": {"type": "string", "pattern": "^[0-9]+\\.[0-9]{2}$"},
            "inventory": {"type": "integer", "minimum": 0},
            "version": {"type": "integer", "minimum": 1},
        },
    }
    common_case = {
        "type": "object",
        "additionalProperties": False,
        "required": ["case_id", "candidate", "split", "input"],
        "properties": {
            "case_id": {"type": "string", "pattern": "^phase13-"},
            "candidate": {"enum": list(CANDIDATES)},
            "split": {"enum": list(SPLIT_COUNTS)},
            "input": {"type": "object"},
        },
    }
    inputs = {
        "live_ops": {
            "type": "object",
            "additionalProperties": False,
            "required": ["room_id", "trace_id", "evidence_refs", "products", "inventory_alert", "danmaku"],
            "properties": {
                "room_id": {"type": "string"},
                "trace_id": {"type": "string"},
                "evidence_refs": {"type": "array", "items": evidence, "minItems": 1},
                "products": {"type": "array", "items": product, "minItems": 2},
                "inventory_alert": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["sold_out_product_id", "expected_version", "risk_open", "backup_available"],
                    "properties": {
                        "sold_out_product_id": {"type": "string"},
                        "expected_version": {"type": "integer", "minimum": 1},
                        "risk_open": {"type": "boolean"},
                        "backup_available": {"type": "boolean"},
                    },
                },
                "danmaku": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["question_count", "top_intent"],
                    "properties": {
                        "question_count": {"type": "integer", "minimum": 0},
                        "top_intent": {"enum": ["price", "stock", "usage"]},
                    },
                },
            },
        },
        "planner": {
            "type": "object",
            "additionalProperties": False,
            "required": ["room_id", "anchor_id", "products", "constraints", "memories", "current_plan"],
            "properties": {
                "room_id": {"type": "string"},
                "anchor_id": {"type": "string"},
                "products": {"type": "array", "items": product, "minItems": 3},
                "constraints": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["max_cards", "min_priority", "requires_memory"],
                    "properties": {
                        "max_cards": {"type": "integer", "minimum": 1},
                        "min_priority": {"type": "integer", "minimum": 1},
                        "requires_memory": {"type": "boolean"},
                    },
                },
                "memories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["memory_id", "tag", "version"],
                        "properties": {
                            "memory_id": {"type": "string"},
                            "tag": {"type": "string"},
                            "version": {"type": "integer", "minimum": 1},
                        },
                    },
                },
                "current_plan": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["version", "failed_product_ids"],
                    "properties": {
                        "version": {"type": "integer", "minimum": 1},
                        "failed_product_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
        "review_memory": {
            "type": "object",
            "additionalProperties": False,
            "required": ["room_id", "anchor_id", "replay", "decision_traces", "catalog_whitelist", "active_memory", "candidate_context"],
            "properties": {
                "room_id": {"type": "string"},
                "anchor_id": {"type": "string"},
                "replay": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["trace_id", "safety_violation_count", "dominant_signal", "evidence_conflict"],
                    "properties": {
                        "trace_id": {"type": "string"},
                        "safety_violation_count": {"type": "integer", "minimum": 0},
                        "dominant_signal": {"enum": ["inventory", "content", "timing"]},
                        "evidence_conflict": {"type": "boolean"},
                    },
                },
                "decision_traces": {"type": "array", "items": evidence, "minItems": 2},
                "catalog_whitelist": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["product_ids", "categories", "tags"],
                    "properties": {
                        "product_ids": {"type": "array", "items": {"type": "string"}},
                        "categories": {"type": "array", "items": {"type": "string"}},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "active_memory": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["memory_id", "template_key"],
                        "properties": {
                            "memory_id": {"type": "string"},
                            "template_key": {"type": "string"},
                        },
                    },
                },
                "candidate_context": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["whitelist_match", "independent_trace_count"],
                    "properties": {
                        "whitelist_match": {"type": "boolean"},
                        "independent_trace_count": {"type": "integer", "minimum": 0},
                    },
                },
            },
        },
    }
    common_case["allOf"] = [
        {
            "if": {"properties": {"candidate": {"const": candidate}}},
            "then": {
                "properties": {
                    "case_id": {"pattern": f"^phase13-{candidate}-"},
                    "input": input_schema,
                }
            },
        }
        for candidate, input_schema in inputs.items()
    ]
    common_case["allOf"].extend(
        {
            "if": {"properties": {"split": {"const": split}}},
            "then": {
                "properties": {
                    "case_id": {
                        "pattern": f"^phase13-[a-z_]+-{split}-[0-9]{{3}}$"
                    }
                }
            },
        }
        for split in SPLIT_COUNTS
    )
    label = {
        "type": "object",
        "additionalProperties": False,
        "required": ["case_id", "candidate", "split", "label"],
        "properties": {
            "case_id": {"type": "string", "pattern": "^phase13-"},
            "candidate": {"enum": list(CANDIDATES)},
            "split": {"enum": list(SPLIT_COUNTS)},
            "label": {"type": "object", "minProperties": 1},
        },
    }
    label_shapes = {
        "live_ops": {
            "type": "object",
            "additionalProperties": False,
            "required": ["expected_action", "action_success", "incident_recovery"],
            "properties": {
                "expected_action": {
                    "enum": ["NO_ACTION", "HUMAN_ATTENTION", "SWITCH_PRODUCT_SUGGESTION", "DANMAKU_REPLY_SUGGESTION"]
                },
                "action_success": {"type": "boolean"},
                "incident_recovery": {"type": "boolean"},
            },
        },
        "planner": {
            "type": "object",
            "additionalProperties": False,
            "required": ["expected_node_keys", "executable", "constraint_recovery_required", "constraint_recovery"],
            "properties": {
                "expected_node_keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "executable": {"type": "boolean"},
                "constraint_recovery_required": {"type": "boolean"},
                "constraint_recovery": {"type": "boolean"},
            },
        },
        "review_memory": {
            "type": "object",
            "additionalProperties": False,
            "required": ["attribution_category", "grounded_attribution", "memory_candidate_class", "promotable"],
            "properties": {
                "attribution_category": {"enum": ["inventory", "content", "timing"]},
                "grounded_attribution": {"type": "boolean"},
                "memory_candidate_class": {"enum": ["APPLY", "REJECT", "REVIEW"]},
                "promotable": {"type": "boolean"},
            },
        },
    }
    label["allOf"] = [
        {
            "if": {"properties": {"candidate": {"const": candidate}}},
            "then": {"properties": {"label": label_schema}},
        }
        for candidate, label_schema in label_shapes.items()
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://liveagent.local/schemas/phase13_case.schema.json",
        "$ref": "#/$defs/case",
        "$defs": {"case": common_case, "label": label},
    }


def generate_phase13_dataset(root: Path, *, seed: int = SEED) -> dict[str, Any]:
    """生成全部资产并返回与落盘字节相同的 Manifest。"""

    root = Path(root)
    schema = _schema()
    _write_json(root / "schemas" / "phase13_case.schema.json", schema)
    _write_json(
        root / "pricing" / "deepseek-v4-flash-2026-07-16.json",
        PRICING_SNAPSHOT,
    )
    for candidate in CANDIDATES:
        prompt = _candidate_prompt(candidate)
        _write(root / "prompts" / "phase13" / f"{candidate}-v1.txt", (prompt + "\n").encode("utf-8"))
        _write_json(
            root / "result_schemas" / "phase13" / f"{candidate}-v1.json",
            RESULT_SCHEMAS[candidate],
        )

    case_ids = {split: [] for split in SPLIT_COUNTS}
    case_candidate_map: dict[str, str] = {}
    for candidate_index, candidate in enumerate(CANDIDATES):
        for split_index, (split, count) in enumerate(SPLIT_COUNTS.items()):
            cases: list[dict[str, Any]] = []
            labels: list[dict[str, Any]] = []
            for index in range(1, count + 1):
                # candidate/split 各占独立编号区间；seed 参与业务变量而不泄漏到 ID。
                variant_index = (
                    (seed % 997) * 100_000
                    + candidate_index * 10_000
                    + split_index * 1_000
                    + index
                )
                case, label = _case_and_label(candidate, split, index, variant_index)
                cases.append(case)
                labels.append(label)
                case_ids[split].append(case["case_id"])
                case_candidate_map[case["case_id"]] = candidate
            _write_jsonl(root / "cases" / "phase13" / f"{candidate}-{split}.jsonl", cases)
            _write_jsonl(root / "labels" / "phase13" / f"{candidate}-{split}.jsonl", labels)

    artifact_paths = tuple(
        sorted(
            path.relative_to(root)
            for directory in (
                "schemas",
                "pricing",
                "prompts",
                "result_schemas",
                "cases",
                "labels",
            )
            for path in (root / directory).rglob("*")
            if path.is_file()
        )
    )
    artifact_digests = {
        relative.as_posix(): _digest_bytes((root / relative).read_bytes())
        for relative in artifact_paths
    }
    prompt_digests = {
        key: artifact_digests[f"prompts/phase13/{key}-v1.txt"] for key in CANDIDATES
    }
    # Profile 内嵌正文直接解码已落盘字节，确保结尾 LF 与 prompt_digest 严格同源。
    prompt_texts = {
        key: (root / "prompts" / "phase13" / f"{key}-v1.txt")
        .read_bytes()
        .decode("utf-8")
        for key in CANDIDATES
    }
    result_schema_digests = {
        key: _identity_digest_json(RESULT_SCHEMAS[key])
        for key in CANDIDATES
    }
    dataset_artifacts = {
        key: value
        for key, value in artifact_digests.items()
        if key.startswith("cases/") or key.startswith("labels/")
    }
    project_root = Path(__file__).parents[2]
    # 目录发现形成保守闭包：全部产品源码与评估运行代码都参与 LF 规范化摘要。
    source_paths = tuple(
        sorted(
            path.relative_to(project_root)
            for source_root in (project_root / "src", project_root / "evaluation")
            for path in source_root.rglob("*.py")
        )
    )
    source_artifact_digests = {
        path.as_posix(): _digest_bytes(_normalized_source_bytes(project_root / path))
        for path in source_paths
    }
    generator_digest = source_artifact_digests[
        "evaluation/generators/generate_phase13_cases.py"
    ]
    task_kinds = {
        "live_ops": "LIVE_OPS_ADVICE",
        "planner": "PLAN_PROPOSAL",
        "review_memory": "POST_LIVE_REVIEW",
    }
    profiles = {
        candidate: {
            **PROFILE_LIMITS[candidate],
            "model_id": MODEL_ID,
            "endpoint_host": ENDPOINT_HOST,
            "temperature": "0",
            "task_kind": task_kinds[candidate],
            "prompt_version": "1",
            "prompt_path": f"prompts/phase13/{candidate}-v1.txt",
            "prompt_text": prompt_texts[candidate],
            "prompt_digest": prompt_digests[candidate],
            "result_schema_version": "1",
            "result_schema_path": f"result_schemas/phase13/{candidate}-v1.json",
            "result_schema_digest": result_schema_digests[candidate],
        }
        for candidate in CANDIDATES
    }
    manifest: dict[str, Any] = {
        "manifest_id": "phase13-v2",
        "manifest_version": "2.0.0",
        "seed": seed,
        "endpoint_host": ENDPOINT_HOST,
        "model_id": MODEL_ID,
        "temperature": 0,
        "development_real_smoke_limit_per_candidate": 5,
        "holdout_label_access": "EVALUATOR_ONLY",
        "external_anchor_policy": "GIT_COMMIT_REQUIRED",
        "formal_execution_preflight": {
            "required": True,
            "task": "PHASE_13_TASK_11",
            "verifies": "FROZEN_SKILL_VERSIONS_AVAILABLE_IN_CURRENT_CATALOG",
        },
        "case_ids": {key: sorted(value) for key, value in case_ids.items()},
        "case_candidate_map": dict(sorted(case_candidate_map.items())),
        "artifact_digests": artifact_digests,
        "source_artifact_digests": source_artifact_digests,
        "profiles": profiles,
        "dataset_digest": _digest_json(dataset_artifacts),
        "schema_digest": artifact_digests["schemas/phase13_case.schema.json"],
        "generator_digest": generator_digest,
        "profile_bundle_digest": _digest_json(profiles),
        "prompt_bundle_digest": _digest_json(prompt_digests),
        "result_schema_bundle_digest": _digest_json(result_schema_digests),
        # 来源摘要必须直接绑定快照文件原始字节，不能退化为进程内字典摘要。
        "pricing_source_digest": artifact_digests[
            "pricing/deepseek-v4-flash-2026-07-16.json"
        ],
        "code_digest": _digest_json(source_artifact_digests),
        "price_policy_digest": _digest_json(
            {"conversion_policy_version": PRICING["conversion_policy_version"], "pricing": PRICING}
        ),
        "pricing": PRICING,
    }
    candidate_identity = {
        "live_ops": "LIVE_OPS",
        "planner": "PLANNER",
        "review_memory": "REVIEW_MEMORY",
    }
    store_manifest: dict[str, Any] = {
        "manifest_id": manifest["manifest_id"],
        "manifest_version": manifest["manifest_version"],
        "manifest_kind": "DATASET_BASELINE",
        "source_commit": None,
        "dataset_digest": manifest["dataset_digest"],
        "schema_digest": manifest["schema_digest"],
        "generator_digest": manifest["generator_digest"],
        "seed": manifest["seed"],
        "development_case_ids": manifest["case_ids"]["development"],
        "validation_case_ids": manifest["case_ids"]["validation"],
        "holdout_case_ids": manifest["case_ids"]["holdout"],
        "case_candidate_map": {
            case_id: candidate_identity[candidate]
            for case_id, candidate in manifest["case_candidate_map"].items()
        },
        "profile_bundle_digest": manifest["profile_bundle_digest"],
        "prompt_bundle_digest": manifest["prompt_bundle_digest"],
        "result_schema_bundle_digest": manifest["result_schema_bundle_digest"],
        "pricing_source_digest": manifest["pricing_source_digest"],
        "temperature": "0",
        "code_digest": manifest["code_digest"],
        "price_policy_digest": manifest["price_policy_digest"],
        "endpoint_host": manifest["endpoint_host"],
        "model_id": manifest["model_id"],
        "candidate_ids": sorted(candidate_identity.values()),
    }
    store_manifest["manifest_digest"] = _identity_digest_json(store_manifest)
    manifest["store_manifest"] = store_manifest
    manifest["manifest_digest"] = _digest_json(manifest)
    _write_json(root / "manifests" / "phase13-v2.json", manifest)
    return manifest


def main() -> int:
    generate_phase13_dataset(Path(__file__).parents[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
