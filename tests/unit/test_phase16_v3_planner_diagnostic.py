"""Phase 16 V3 Planner 诊断的最小离线契约。

这些测试只验证脱敏失败事实如何从共享模型端口传递到诊断账本边界；不读取 .env、
不连接 PostgreSQL，也绝不请求真实模型。
"""

from __future__ import annotations

from decimal import Decimal

from src.decision_support.planner_diagnostic_v3 import model_failure_fact_from_outcome
from src.specialist_runtime.model_port import ModelFailure, ModelFailureCategory


def test_v3_failure_fact_preserves_observable_model_failure_without_sensitive_content() -> None:
    """已发送的端口失败必须保留可诊断类别和脱敏元数据，不能再次坍缩为通用失败码。"""

    outcome = ModelFailure(
        request_id="5ed56d89-2e91-4c55-97b6-aa43a7316a84",
        category=ModelFailureCategory.INVALID_OUTPUT_JSON,
        request_sent=True,
        response_digest="a" * 64,
        http_status=200,
        retry_after_seconds=None,
        latency_ms=Decimal("1234.567"),
    )

    fact = model_failure_fact_from_outcome(
        attempt_id="94d6a1cc-1be9-4473-8f52-b6dfb2fe33b1",
        outcome=outcome,
    )

    assert fact.attempt_id == "94d6a1cc-1be9-4473-8f52-b6dfb2fe33b1"
    assert fact.category is ModelFailureCategory.INVALID_OUTPUT_JSON
    assert fact.request_sent is True
    assert fact.http_status == 200
    assert fact.retry_after_seconds is None
    assert fact.response_digest == "a" * 64
    assert fact.latency_ms == Decimal("1234.567")
    # 失败事实不应接收模型正文、Prompt、异常字符串或认证信息等自由文本字段。
    assert set(fact.model_dump()) == {
        "attempt_id",
        "category",
        "request_sent",
        "response_digest",
        "http_status",
        "retry_after_seconds",
        "latency_ms",
        "fact_digest",
    }


def test_v3_failure_fact_normalizes_latency_before_binding_its_digest() -> None:
    """adapter 的高精度时钟值必须先量化到数据库精度，读取后才能重建同一审计摘要。"""

    fact = model_failure_fact_from_outcome(
        attempt_id="94d6a1cc-1be9-4473-8f52-b6dfb2fe33b1",
        outcome=ModelFailure(
            request_id="5ed56d89-2e91-4c55-97b6-aa43a7316a84",
            category=ModelFailureCategory.INVALID_OUTPUT_JSON,
            request_sent=True,
            response_digest="a" * 64,
            http_status=200,
            retry_after_seconds=None,
            latency_ms=Decimal("1234.5678"),
        ),
    )

    # PostgreSQL NUMERIC(16,3) 中保存的值与签名/摘要输入完全一致，避免出现 V3 首次
    # 真实调用中“行存在但认证无法复验”的精度漂移。
    assert fact.latency_ms == Decimal("1234.568")
