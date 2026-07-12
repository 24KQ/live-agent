"""Phase 11A Skill Catalog。

13 个 SkillManifest 的唯一事实源。Catalog 在应用启动时统一校验所有 Manifest，
非法 Schema、重复 ID 或空版本导致 Catalog 构建失败。

ToolRegistry 通过本 Catalog 的只读投影生成，不再维护独立元数据。
"""

from __future__ import annotations

from typing import Sequence

from jsonschema import Draft202012Validator, ValidationError as JsonSchemaError

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

# query_products：无业务参数，房间信息来自可信上下文
_QUERY_PRODUCTS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "room_id": {"type": "string"},
    },
    "additionalProperties": False,
}

# generate_live_plan：接收不可变商品快照列表
_GENERATE_LIVE_PLAN_SCHEMA: dict = {
    "type": "object",
    "required": ["room_id", "products"],
    "properties": {
        "room_id": {"type": "string"},
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["product_id", "title", "price"],
                "properties": {
                    "product_id": {"type": "string"},
                    "title": {"type": "string"},
                    "price": {"type": "string"},
                },
            },
        },
    },
    "additionalProperties": False,
}

# generate_product_card：接收单个不可变商品快照
_GENERATE_PRODUCT_CARD_SCHEMA: dict = {
    "type": "object",
    "required": ["room_id", "product"],
    "properties": {
        "room_id": {"type": "string"},
        "product": {
            "type": "object",
            "required": ["product_id", "title", "price"],
            "properties": {
                "product_id": {"type": "string"},
                "title": {"type": "string"},
                "price": {"type": "string"},
            },
        },
    },
    "additionalProperties": False,
}

# setup_live_session：接收不可变计划快照
_SETUP_LIVE_SESSION_SCHEMA: dict = {
    "type": "object",
    "required": ["room_id", "plan", "idempotency_key"],
    "properties": {
        "room_id": {"type": "string"},
        "plan": {
            "type": "object",
            "required": ["plan_id", "items"],
            "properties": {
                "plan_id": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["item_id", "product_id"],
                        "properties": {
                            "item_id": {"type": "string"},
                            "product_id": {"type": "string"},
                            "start_time": {"type": "string"},
                            "duration_seconds": {"type": "integer"},
                        },
                    },
                },
            },
        },
        "idempotency_key": {"type": "string"},
    },
    "additionalProperties": False,
}


# ── 编译时 Manifest 列表 ──────────────────────────────────────────────

_MANIFESTS: list[SkillManifest] = [
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
        },
        gate_decision=GateDecision.HARD_GATE,
        requires_idempotency_key=True,
    ),
    SkillManifest(
        skill_id="create_live_plan_draft",
        description="生成播前商品草案，不执行写操作",
        lifecycle=_PRE,
        risk_level=RiskLevel.MEDIUM,
        parameter_schema={"type": "object", "properties": {"room_id": {"type": "string"}}},
        gate_decision=GateDecision.SOFT_GATE,
    ),
    SkillManifest(
        skill_id="generate_live_plan",
        description="基于当前货盘生成确定性播前排品计划",
        lifecycle=_PRE,
        risk_level=RiskLevel.MEDIUM,
        parameter_schema=_GENERATE_LIVE_PLAN_SCHEMA,
        gate_decision=GateDecision.SOFT_GATE,
        compatibility_note="输入从旧 room_id 改为不可变商品快照列表",
    ),
    SkillManifest(
        skill_id="generate_product_card",
        description="为单商品生成确定性直播手卡",
        lifecycle=_PRE,
        risk_level=RiskLevel.MEDIUM,
        parameter_schema=_GENERATE_PRODUCT_CARD_SCHEMA,
        gate_decision=GateDecision.SOFT_GATE,
        compatibility_note="输入从旧 product_id 改为不可变商品对象",
    ),
    SkillManifest(
        skill_id="setup_live_session",
        description="根据播前排品模拟建播写操作",
        lifecycle=_PRE,
        risk_level=RiskLevel.HIGH,
        parameter_schema=_SETUP_LIVE_SESSION_SCHEMA,
        gate_decision=GateDecision.HARD_GATE,
        requires_idempotency_key=True,
        compatibility_note="输入从旧 plan_item_ids 改为不可变计划快照",
    ),
    SkillManifest(
        skill_id="handle_sold_out_event",
        description="处理播中售罄事件，触发 Reducer 下架商品",
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
        },
        gate_decision=GateDecision.AUTO,
        requires_idempotency_key=True,
    ),
    SkillManifest(
        skill_id="recommend_backup_product",
        description="售罄时推荐可接盘的备选商品",
        lifecycle=_ON,
        risk_level=RiskLevel.MEDIUM,
        parameter_schema={
            "type": "object",
            "required": ["room_id", "sold_out_product_id"],
            "properties": {
                "room_id": {"type": "string"},
                "sold_out_product_id": {"type": "string"},
            },
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
        },
        gate_decision=GateDecision.AUTO,
    ),
    SkillManifest(
        skill_id="aggregate_danmaku_questions",
        description="在 5 秒窗口聚合播中弹幕同类型问题，不改状态",
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
        },
        gate_decision=GateDecision.AUTO,
    ),
    SkillManifest(
        skill_id="generate_danmaku_reply",
        description="为聚合后的弹幕问题生成友好参考回复文案",
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
        },
        gate_decision=GateDecision.SOFT_GATE,
    ),
    SkillManifest(
        skill_id="on_live_context_collect",
        description="被动收集弹幕摘要和库存警报，不改状态",
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
        },
        gate_decision=GateDecision.AUTO,
    ),
]


# ── 编译时校验 ──────────────────────────────────────────────────────

def _validate() -> None:
    """启动时校验所有 Manifest，fail-fast。"""
    seen_ids: set[str] = set()
    for mf in _MANIFESTS:
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


_validate()


# ── 唯一公开查询函数 ──────────────────────────────────────────────────


def get_default_skill_catalog() -> Sequence[SkillManifest]:
    """返回不可变 Manifest 序列。

    调用方不得修改返回值。Catalog 是唯一事实源，
    ToolRegistry 通过投影从本 Catalog 生成。
    """
    return list(_MANIFESTS)
