"""Phase 11A Skill Catalog。

13 个 SkillManifest 的唯一事实源。Catalog 在应用启动时统一校验所有 Manifest，
非法 Schema、重复 ID 或空版本导致 Catalog 构建失败。

ToolRegistry 通过本 Catalog 的只读投影生成，不再维护独立元数据。
"""

from __future__ import annotations

from typing import Sequence

from jsonschema import Draft202012Validator, SchemaError as JsonSchemaError

from src.core.security_hooks import GateDecision
from src.state.models import LifecycleStage, RiskLevel
from src.skill_runtime.models import (
    SkillManifest,
)

# ── 生命周期缩写 ──────────────────────────────────────────────────────

_PRE = {LifecycleStage.PRE_LIVE}
_ON = {LifecycleStage.ON_LIVE}
_BOTH = {LifecycleStage.PRE_LIVE, LifecycleStage.ON_LIVE}


# ── 四个核心 Handler 的显式 Schema ──────────────────────────────────

# 完整商品快照 Schema 与 CatalogProduct.model_dump(mode="json") 对齐。
# 所有字段都由查询结果或调用方冻结快照提供，拒绝额外字段可避免 LLM
# 在商品对象中夹带未治理的业务参数。
_CATALOG_PRODUCT_SCHEMA: dict = {
    "type": "object",
    "required": [
        "product_id",
        "name",
        "category",
        "price",
        "inventory",
        "conversion_rate",
        "commission_rate",
        "tags",
        "selling_points",
        "is_active",
    ],
    "properties": {
        "product_id": {"type": "string"},
        "name": {"type": "string"},
        "category": {"type": "string"},
        "price": {"type": "string"},
        "inventory": {"type": "integer"},
        "conversion_rate": {"type": "string"},
        "commission_rate": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "selling_points": {"type": "array", "items": {"type": "string"}},
        "is_active": {"type": "boolean"},
    },
    "additionalProperties": False,
}


# query_products：无业务参数，房间信息来自可信上下文
_QUERY_PRODUCTS_SCHEMA: dict = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

# generate_live_plan：接收不可变商品快照列表
_GENERATE_LIVE_PLAN_SCHEMA: dict = {
    "type": "object",
    "required": ["products"],
    "properties": {
        "products": {
            "type": "array",
            "items": _CATALOG_PRODUCT_SCHEMA,
        },
    },
    "additionalProperties": False,
}

# generate_product_card：接收单个不可变商品快照
_GENERATE_PRODUCT_CARD_SCHEMA: dict = {
    "type": "object",
    "required": ["product"],
    "properties": {
        "product": _CATALOG_PRODUCT_SCHEMA,
    },
    "additionalProperties": False,
}

# setup_live_session：接收不可变计划快照
_SETUP_LIVE_SESSION_SCHEMA: dict = {
    "type": "object",
    "required": ["plan"],
    "properties": {
        "plan": {
            "type": "object",
            "required": ["room_id", "trace_id", "items"],
            "properties": {
                "room_id": {"type": "string"},
                "trace_id": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["rank", "product_id", "product_name", "role", "reason"],
                        "properties": {
                            "rank": {"type": "integer", "minimum": 1},
                            "product_id": {"type": "string"},
                            "product_name": {"type": "string"},
                            "role": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}


# ── 编译时 Manifest 列表 ──────────────────────────────────────────────

_MANIFESTS: tuple[SkillManifest, ...] = (
    SkillManifest(
        skill_id="query_products",
        description="查询播前模拟商品货盘",
        lifecycle=_PRE,
        risk_level=RiskLevel.LOW,
        parameter_schema=_QUERY_PRODUCTS_SCHEMA,
        gate_decision=GateDecision.AUTO,
        requires_idempotency_key=False,
        compatibility_note="参数限制为只读查询不需要额外字段",
    ),
    SkillManifest(
        skill_id="suggest_price_change",
        description="生成播前改价建议，不直接修改状态",
        lifecycle=_PRE,
        risk_level=RiskLevel.MEDIUM,
        parameter_schema={
            "type": "object",
            "required": ["product_id", "suggested_price"],
            "properties": {
                "product_id": {"type": "string"},
                "suggested_price": {"type": "string"},
            },
            "additionalProperties": False,
        },
        gate_decision=GateDecision.SOFT_GATE,
    ),
    SkillManifest(
        skill_id="set_product_price",
        description="执行商品改价",
        lifecycle=_PRE,
        risk_level=RiskLevel.HIGH,
        parameter_schema={
            "type": "object",
            "required": ["product_id", "price"],
            "properties": {
                "product_id": {"type": "string"},
                "price": {"type": "string"},
            },
            "additionalProperties": False,
        },
        gate_decision=GateDecision.HARD_GATE,
        requires_idempotency_key=True,
    ),
    SkillManifest(
        skill_id="create_live_plan_draft",
        description="生成播前排品草案，不执行建播写操作",
        lifecycle=_PRE,
        risk_level=RiskLevel.MEDIUM,
        parameter_schema={
            "type": "object",
            "properties": {"room_id": {"type": "string"}},
            "additionalProperties": False,
        },
        gate_decision=GateDecision.SOFT_GATE,
    ),
    SkillManifest(
        skill_id="generate_live_plan",
        description="基于本地样例货盘生成确定性播前排品方案",
        lifecycle=_PRE,
        risk_level=RiskLevel.MEDIUM,
        parameter_schema=_GENERATE_LIVE_PLAN_SCHEMA,
        gate_decision=GateDecision.SOFT_GATE,
        compatibility_note="输入从旧 room_id 改为不可变商品快照列表",
    ),
    SkillManifest(
        skill_id="generate_product_card",
        description="为指定商品生成确定性主播讲解手卡",
        lifecycle=_PRE,
        risk_level=RiskLevel.MEDIUM,
        parameter_schema=_GENERATE_PRODUCT_CARD_SCHEMA,
        gate_decision=GateDecision.SOFT_GATE,
        compatibility_note="输入从旧 product_id 改为不可变商品对象",
    ),
    SkillManifest(
        skill_id="setup_live_session",
        description="根据播前方案模拟建播配置写入",
        lifecycle=_PRE,
        risk_level=RiskLevel.HIGH,
        parameter_schema=_SETUP_LIVE_SESSION_SCHEMA,
        gate_decision=GateDecision.HARD_GATE,
        requires_idempotency_key=True,
        compatibility_note="输入从旧 plan_item_ids 改为不可变计划快照",
    ),
    SkillManifest(
        skill_id="handle_sold_out_event",
        description="处理播中售罄事件，调用 Reducer 下架售罄商品",
        lifecycle=_ON,
        risk_level=RiskLevel.HIGH,
        parameter_schema={
            "type": "object",
            "required": ["room_id", "product_id", "trace_id", "idempotency_key"],
            "properties": {
                "room_id": {"type": "string"},
                "product_id": {"type": "string"},
                "trace_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "additionalProperties": False,
        },
        gate_decision=GateDecision.AUTO,
        requires_idempotency_key=True,
    ),
    SkillManifest(
        skill_id="recommend_backup_product",
        description="播中售罄后推荐仍可讲解的备选商品",
        lifecycle=_ON,
        risk_level=RiskLevel.MEDIUM,
        parameter_schema={
            "type": "object",
            "required": ["room_id", "sold_out_product_id"],
            "properties": {
                "room_id": {"type": "string"},
                "sold_out_product_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
        gate_decision=GateDecision.AUTO,
    ),
    SkillManifest(
        skill_id="generate_on_live_prompt",
        description="生成播中主播提示文案，不直接修改状态",
        lifecycle=_ON,
        risk_level=RiskLevel.LOW,
        parameter_schema={
            "type": "object",
            "required": ["room_id", "sold_out_product_id"],
            "properties": {
                "room_id": {"type": "string"},
                "sold_out_product_id": {"type": "string"},
                "backup_product_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
        gate_decision=GateDecision.AUTO,
    ),
    SkillManifest(
        skill_id="aggregate_danmaku_questions",
        description="按 5 秒窗口聚合播中弹幕同类问题，不修改状态",
        lifecycle=_ON,
        risk_level=RiskLevel.LOW,
        parameter_schema={
            "type": "object",
            "required": ["room_id", "trace_id", "events"],
            "properties": {
                "room_id": {"type": "string"},
                "trace_id": {"type": "string"},
                "events": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": False,
        },
        gate_decision=GateDecision.AUTO,
    ),
    SkillManifest(
        skill_id="generate_danmaku_reply",
        description="为聚合后的弹幕问题生成主播参考回复，不自动发送",
        lifecycle=_ON,
        risk_level=RiskLevel.MEDIUM,
        parameter_schema={
            "type": "object",
            "required": ["room_id", "trace_id", "category", "summary"],
            "properties": {
                "room_id": {"type": "string"},
                "trace_id": {"type": "string"},
                "category": {"type": "string"},
                "summary": {"type": "string"},
            },
            "additionalProperties": False,
        },
        gate_decision=GateDecision.SOFT_GATE,
    ),
    SkillManifest(
        skill_id="on_live_context_collect",
        description="播中收集弹幕聚合摘要和库存告警，不修改状态",
        lifecycle=_ON,
        risk_level=RiskLevel.LOW,
        parameter_schema={
            "type": "object",
            "required": ["room_id", "trace_id"],
            "properties": {
                "room_id": {"type": "string"},
                "trace_id": {"type": "string"},
                "danmaku_summary": {"type": "array", "items": {"type": "object"}},
                "inventory_alerts": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": False,
        },
        gate_decision=GateDecision.AUTO,
    ),
)


# ── 编译时校验 ──────────────────────────────────────────────────────

def validate_manifests(manifests: Sequence[SkillManifest]) -> tuple[SkillManifest, ...]:
    """校验并冻结一组 Manifest，供启动装配和失败测试复用。"""
    seen_ids: set[str] = set()
    validated: list[SkillManifest] = []
    for mf in manifests:
        if mf.skill_id in seen_ids:
            raise ValueError(f"重复 skill_id: {mf.skill_id}")
        seen_ids.add(mf.skill_id)
        if mf.parameter_schema:
            try:
                Draft202012Validator.check_schema(mf.parameter_schema)
            except JsonSchemaError as exc:
                raise ValueError(f"{mf.skill_id} schema 不合规: {exc}") from exc
        if not mf.version:
            raise ValueError(f"{mf.skill_id} version 为空")
        validated.append(mf)
    return tuple(validated)


_MANIFESTS = validate_manifests(_MANIFESTS)


# ── 唯一公开查询函数 ──────────────────────────────────────────────────


def get_default_skill_catalog() -> Sequence[SkillManifest]:
    """返回不可变 Manifest 序列。

    调用方不得修改返回值。Catalog 是唯一事实源，
    ToolRegistry 通过投影从本 Catalog 生成。
    """
    return _MANIFESTS
