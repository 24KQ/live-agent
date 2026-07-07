"""Phase 3A 记忆与信任领域模型。

本模块只定义纯数据契约，不连接数据库、不调用外部服务。所有写入 PostgreSQL
之前的数据都先通过这里的 Pydantic 校验，确保空主播、未知记忆层、越界信任分等
问题尽早暴露，而不是把脏数据留到后续排品或审计链路里。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _strip_required_text(value: str) -> str:
    """清理必填文本字段，并拒绝只包含空白的值。"""

    normalized = value.strip()
    if not normalized:
        raise ValueError("value must not be blank")
    return normalized


def _strip_optional_text(value: str | None) -> str | None:
    """清理可选文本字段；有值时同样不能只是空白。"""

    if value is None:
        return None
    return _strip_required_text(value)


class MemoryLayer(StrEnum):
    """主播记忆分层。

    L1：主播显式表达的偏好或约束。
    L2：系统从历史行为和商品表现中归纳出的稳定模式。
    L3：更长期、更抽象的总结，后续可由离线任务或 LLM 归纳生成。
    """

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class MemorySource(StrEnum):
    """记忆来源。

    记忆来源会写入审计和排品理由，帮助后续复盘“为什么 Agent 会这样建议”。
    """

    USER_STATED = "user_stated"
    SYSTEM_OBSERVED = "system_observed"
    OFFLINE_SUMMARY = "offline_summary"
    MANUAL_IMPORT = "manual_import"


class AnchorAction(StrEnum):
    """主播对建议的动作反馈。"""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class BusinessResult(StrEnum):
    """建议执行后的业务结果归因。

    GOOD/BAD 用于主播采纳建议后的效果；AGENT_RIGHT/ANCHOR_RIGHT 用于主播拒绝建议后，
    事后复盘到底是 Agent 判断更准，还是主播判断更准。
    """

    GOOD = "good"
    BAD = "bad"
    AGENT_RIGHT = "agent_right"
    ANCHOR_RIGHT = "anchor_right"


class AnchorMemoryEntry(BaseModel):
    """单条主播记忆。

    embedding 字段先作为 pgvector 的结构预留，本阶段不接 embedding 模型，因此默认允许为空。
    metadata 用于放结构化偏好，例如 preferred_category、preferred_tags、preferred_product_ids。
    """

    model_config = ConfigDict(frozen=True)

    memory_id: str | None = None
    memory_key: str | None = Field(default=None, min_length=1)
    anchor_id: str = Field(..., min_length=1)
    room_id: str | None = Field(default=None, min_length=1)
    layer: MemoryLayer
    content: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: Decimal = Field(default=Decimal("0.70"), ge=Decimal("0.00"), le=Decimal("1.00"))
    evidence_weight: Decimal = Field(default=Decimal("0.50"), ge=Decimal("0.00"), le=Decimal("1.00"))
    source: MemorySource
    embedding: list[float] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("memory_key", "room_id")
    @classmethod
    def normalize_optional_identifiers(cls, value: str | None) -> str | None:
        """可选 ID 一旦提供，就必须是可追踪的非空白字符串。"""

        return _strip_optional_text(value)

    @field_validator("anchor_id")
    @classmethod
    def normalize_anchor_id(cls, value: str) -> str:
        """主播 ID 不能只包含空白。"""

        return _strip_required_text(value)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        """清理记忆正文两端空白，防止只有空格的内容绕过 min_length。"""

        return _strip_required_text(value)


class TrustState(BaseModel):
    """主播维度的 trust_score 状态。

    trust_score 表示系统对“当前 Agent 建议是否值得展示更多能力”的信任阈值，
    不是对主播本人的评价。默认 0.70 让新主播先看到完整非 block 工具，但仍保留
    hard-gate 人审边界。
    """

    model_config = ConfigDict(frozen=True)

    anchor_id: str = Field(..., min_length=1)
    trust_score: Decimal = Field(default=Decimal("0.70"), ge=Decimal("0.00"), le=Decimal("1.00"))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("anchor_id")
    @classmethod
    def normalize_anchor_id(cls, value: str) -> str:
        """trust 状态必须绑定到明确主播。"""

        return _strip_required_text(value)


class DecisionTraceRecord(BaseModel):
    """一次建议、反馈和信任分变化的可回放记录。"""

    model_config = ConfigDict(frozen=True)

    decision_trace_id: str | None = None
    trace_id: str = Field(..., min_length=1)
    anchor_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    recommendation: dict[str, Any] = Field(default_factory=dict)
    anchor_action: AnchorAction
    business_result: BusinessResult
    lift: Decimal = Field(default=Decimal("0.00"))
    trust_delta: Decimal = Field(default=Decimal("0.00"))
    final_trust_score: Decimal = Field(..., ge=Decimal("0.00"), le=Decimal("1.00"))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("decision_trace_id")
    @classmethod
    def normalize_optional_trace_id(cls, value: str | None) -> str | None:
        """数据库生成的记录 ID 如果被传入，也必须是非空白字符串。"""

        return _strip_optional_text(value)

    @field_validator("trace_id", "anchor_id", "room_id")
    @classmethod
    def normalize_required_identifiers(cls, value: str) -> str:
        """决策轨迹的核心 ID 必须可用于后续审计回放。"""

        return _strip_required_text(value)
