"""Phase 16 V3 Planner 单次诊断的脱敏事实契约。

V3 只用于解释一次新的 Planner 外部调用为什么成功、失败或发送前被阻断。它不修改
已关闭的 V1/V2 账本，不产生经营建议，也不拥有生产 LIVE 路由。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from src.specialist_runtime.model_port import ModelFailure, ModelFailureCategory
from src.specialist_runtime.models import StrictFrozenModel, canonical_json_sha256


class Phase16V3DiagnosticFailureCategory(StrEnum):
    """V3 运行器自身的封闭失败类别，不污染 V2 已冻结的通用模型端口契约。"""

    RUNNER_OUTCOME_CONTRACT_BREACH = "RUNNER_OUTCOME_CONTRACT_BREACH"


class Phase16V3ModelFailureFact(StrictFrozenModel):
    """一次模型端口失败的最小、不可变且可审计投影。

    该对象刻意只有供应商交互的结构化元数据。它没有 Prompt、请求正文、响应正文、
    异常消息、Header 或任何凭据字段，因此上层即使把它写入 PostgreSQL 或报告，也
    不会把模型生成内容和认证材料扩散到审计面。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str = Field(..., min_length=1)
    category: ModelFailureCategory | Phase16V3DiagnosticFailureCategory
    # None 表示共享 Runner 已写发送意图但端口没有返回 Outcome，不能臆测 Provider 是否收包。
    request_sent: bool | None
    response_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    retry_after_seconds: int | None = Field(default=None, ge=0, strict=True)
    latency_ms: Decimal = Field(..., ge=Decimal("0"))
    fact_digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @field_validator("attempt_id")
    @classmethod
    def _validate_attempt_id(cls, value: str) -> str:
        """诊断账本只接受规范 UUID，禁止把异常文本或自由输入伪装成 attempt 身份。"""

        try:
            parsed = UUID(value)
        except (AttributeError, ValueError) as error:
            raise ValueError("V3 failure attempt_id must be a UUID") from error
        return str(parsed)

    @field_validator("latency_ms")
    @classmethod
    def _normalize_latency_for_persistence(cls, value: Decimal) -> Decimal:
        """在摘要、HMAC 与 NUMERIC(16,3) 落库前统一毫秒精度，保证可逆复验。

        DeepSeek adapter 由单调时钟浮点数生成延迟，可能携带超过三位的小数；V3 表的
        ``NUMERIC(16,3)`` 会在 PostgreSQL 中量化。若在量化前签名、量化后读取，历史
        事实就无法重建摘要。这里显式采用与 PostgreSQL 正数数值舍入一致的 half-up，
        使未来事实从数据库读取后仍能精确复验。
        """

        return Decimal(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

    @model_validator(mode="after")
    def _bind_fact_digest(self) -> "Phase16V3ModelFailureFact":
        """将每项允许写入的字段绑定为 SHA-256，后续账本可拒绝静默篡改。"""

        payload: dict[str, Any] = {
            "attempt_id": self.attempt_id,
            "category": self.category.value,
            "request_sent": self.request_sent,
            "response_digest": self.response_digest,
            "http_status": self.http_status,
            "retry_after_seconds": self.retry_after_seconds,
            "latency_ms": str(self.latency_ms),
        }
        calculated = canonical_json_sha256(payload)
        if self.fact_digest and self.fact_digest != calculated:
            raise ValueError("V3 model failure fact_digest does not match facts")
        object.__setattr__(self, "fact_digest", calculated)
        return self


def model_failure_fact_from_outcome(
    *,
    attempt_id: str,
    outcome: ModelFailure,
) -> Phase16V3ModelFailureFact:
    """把共享端口的失败 outcome 投影为允许持久化的 V3 脱敏事实。

    `ModelFailure` 已经在 DeepSeek Adapter 边界剥离了原始异常和 HTTP 正文。这里不再
    捕获或推断任何缺失信息，只逐项复制其稳定字段，避免将未知原因错误标记为网络、
    Prompt 或供应商问题。
    """

    if not isinstance(outcome, ModelFailure):
        raise TypeError("V3 failure fact requires ModelFailure")
    return Phase16V3ModelFailureFact(
        attempt_id=attempt_id,
        category=outcome.category,
        request_sent=outcome.request_sent,
        response_digest=outcome.response_digest,
        http_status=outcome.http_status,
        retry_after_seconds=outcome.retry_after_seconds,
        latency_ms=outcome.latency_ms,
    )


def model_failure_fact_from_contract_breach(
    *, attempt_id: str, latency_ms: Decimal
) -> Phase16V3ModelFailureFact:
    """记录端口未返回 Outcome 的受控边界失约，发送状态必须保持未知。"""

    return Phase16V3ModelFailureFact(
        attempt_id=attempt_id,
        category=Phase16V3DiagnosticFailureCategory.RUNNER_OUTCOME_CONTRACT_BREACH,
        request_sent=None,
        response_digest=None,
        http_status=None,
        retry_after_seconds=None,
        latency_ms=latency_ms,
    )
