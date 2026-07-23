"""Phase 13 原生 async 单次模型调用协议。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from src.specialist_runtime.models import StrictFrozenModel, _freeze_json, _plain_json
from src.specialist_runtime.profiles import (
    FORMAL_ENDPOINT_HOST,
    FORMAL_MODEL_ID,
    normalize_endpoint_host,
)


class ModelFailureCategory(StrEnum):
    """单次模型尝试可观察的稳定失败分类。"""

    RATE_LIMITED = "RATE_LIMITED"
    HTTP_ERROR = "HTTP_ERROR"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    INVALID_OUTPUT_JSON = "INVALID_OUTPUT_JSON"
    MODEL_IDENTITY_MISMATCH = "MODEL_IDENTITY_MISMATCH"
    FORBIDDEN_REASONING = "FORBIDDEN_REASONING"


class ModelMessage(StrictFrozenModel):
    """发给 OpenAI-compatible endpoint 的单条显式消息。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1)


class ModelRequest(StrictFrozenModel):
    """一次模型尝试的冻结输入和执行控制事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(..., min_length=1)
    endpoint_host: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    temperature: Decimal = Field(..., ge=Decimal("0"), le=Decimal("2"))
    prompt_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    result_schema_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    messages: tuple[ModelMessage, ...] = Field(..., min_length=1)
    max_output_tokens: int = Field(..., ge=1, strict=True)
    deadline_at: datetime

    @field_validator("temperature")
    @classmethod
    def _require_deterministic_temperature(cls, value: Decimal) -> Decimal:
        if value != Decimal("0"):
            raise ValueError("formal AgentModelPort requires temperature 0")
        return value

    @field_validator("endpoint_host")
    @classmethod
    def _validate_endpoint_host(cls, value: str) -> str:
        # Adapter 直接用该字段拼接 HTTPS URL，因此请求边界必须再次独立校验。
        normalized = normalize_endpoint_host(value)
        if normalized != FORMAL_ENDPOINT_HOST:
            raise ValueError(f"endpoint_host must be {FORMAL_ENDPOINT_HOST}")
        return normalized

    @field_validator("model_id")
    @classmethod
    def _validate_model_id(cls, value: str) -> str:
        if value != FORMAL_MODEL_ID:
            raise ValueError(f"model_id must be {FORMAL_MODEL_ID}")
        return value

    @field_validator("deadline_at")
    @classmethod
    def _require_absolute_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at requires timezone information")
        return value


class ModelUsage(StrictFrozenModel):
    """API 明确返回的 token 计量；缺失时上层必须看到 ``None``。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(..., ge=0, strict=True)
    output_tokens: int = Field(..., ge=0, strict=True)
    total_tokens: int = Field(..., ge=0, strict=True)

    @model_validator(mode="after")
    def _verify_total(self) -> "ModelUsage":
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


class ModelSuccess(StrictFrozenModel):
    """单次模型成功事实，只保留结构化输出和响应摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    output: Any
    usage: ModelUsage | None
    provider_response_id: str | None = Field(default=None, min_length=1)
    finish_reason: str | None = Field(default=None, min_length=1)
    response_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    latency_ms: Decimal = Field(..., ge=Decimal("0"))

    @field_validator("output", mode="after")
    @classmethod
    def _freeze_output(cls, value: Any) -> Any:
        return _freeze_json(value)

    @field_validator("provider_response_id", "finish_reason")
    @classmethod
    def _validate_optional_provider_receipt(cls, value: str | None) -> str | None:
        """拒绝空白回执字段，避免形式存在却不能用于审计关联。"""

        if value is None:
            # 普通历史 Runtime 仍允许 Provider 不提供这两项；Phase 16 正式 smoke
            # 会在更窄的 receipt 校验中提升为必填，不能在通用模型层误伤旧回放记录。
            return None
        if type(value) is not str or not value.strip() or value != value.strip():
            raise ValueError("provider receipt fields must be non-blank trimmed strings")
        return value

    @field_serializer("output", when_used="json")
    def _serialize_output(self, value: Any) -> Any:
        return _plain_json(value)


class ModelFailure(StrictFrozenModel):
    """不保存原始响应、异常文本或敏感 header 的单次失败事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(..., min_length=1)
    category: ModelFailureCategory
    request_sent: bool
    response_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    retry_after_seconds: int | None = Field(default=None, ge=0, strict=True)
    latency_ms: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))


ModelOutcome = ModelSuccess | ModelFailure


@runtime_checkable
class AgentModelPort(Protocol):
    """模型适配器唯一公共入口；每次调用只代表一个外部尝试。"""

    async def complete(self, request: ModelRequest) -> ModelOutcome:
        """执行一次模型请求，不隐藏重试、fallback 或预算借用。"""
