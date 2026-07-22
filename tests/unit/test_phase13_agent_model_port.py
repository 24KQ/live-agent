"""Phase 13 Task 2 原生 async 单次 AgentModelPort 测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from src.specialist_runtime.deepseek_adapter import (
    AsyncHttpResponse,
    DeepSeekAgentModelAdapter,
    HttpxAsyncHttpTransport,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelMessage,
    ModelRequest,
    ModelSuccess,
)
from src.specialist_runtime.scripted_model import ScriptedAgentModel


HASH_A = "a" * 64
HASH_B = "b" * 64


def _request(*, deadline_at: datetime | None = None) -> ModelRequest:
    """构造固定模型身份与绝对 deadline 的最小请求。"""

    return ModelRequest(
        request_id="request-001",
        endpoint_host="api.deepseek.com",
        model_id="deepseek-v4-flash",
        temperature=Decimal("0"),
        prompt_hash=HASH_A,
        result_schema_hash=HASH_B,
        messages=(
            ModelMessage(role="system", content="只输出 JSON"),
            ModelMessage(role="user", content="给出播中建议"),
        ),
        max_output_tokens=200,
        deadline_at=deadline_at
        or datetime.now(timezone.utc) + timedelta(seconds=5),
    )


def _response(
    *,
    status_code: int = 200,
    model: str = "deepseek-v4-flash",
    content: str = '{"action":"NO_ACTION"}',
    usage: dict[str, int] | None = None,
    headers: dict[str, str] | None = None,
    provider_response_id: str = "chatcmpl-test-001",
    finish_reason: str = "stop",
) -> AsyncHttpResponse:
    """构造 OpenAI-compatible HTTP 响应，不依赖真实网络。"""

    body: dict[str, Any] = {
        "id": provider_response_id,
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return AsyncHttpResponse(
        status_code=status_code,
        headers=headers or {},
        body=json.dumps(body).encode("utf-8"),
    )


class _RecordingTransport:
    """记录请求次数和请求事实的 async Fake Transport。"""

    def __init__(
        self,
        response: AsyncHttpResponse | Exception,
        *,
        delay_seconds: float = 0,
    ) -> None:
        self._response = response
        self._delay_seconds = delay_seconds
        self.calls: list[dict[str, Any]] = []

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> AsyncHttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _CancellationIgnoringTransport(_RecordingTransport):
    """模拟错误吞掉取消信号、仍在 deadline 后返回的第三方 Transport。"""

    async def post_json(self, **kwargs: Any) -> AsyncHttpResponse:
        self.calls.append(dict(kwargs))
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
        assert isinstance(self._response, AsyncHttpResponse)
        return self._response


def test_adapter_makes_one_request_and_preserves_identity_usage() -> None:
    """成功调用只发送一次，并原样保留模型身份和 token usage。"""

    transport = _RecordingTransport(
        _response(
            usage={"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100}
        )
    )
    adapter = DeepSeekAgentModelAdapter(api_key="test-secret", transport=transport)

    result = asyncio.run(adapter.complete(_request()))

    assert isinstance(result, ModelSuccess)
    assert result.model_id == "deepseek-v4-flash"
    assert result.output == {"action": "NO_ACTION"}
    assert result.usage is not None
    assert result.usage.input_tokens == 90
    assert result.usage.output_tokens == 10
    assert result.usage.total_tokens == 100
    assert result.provider_response_id == "chatcmpl-test-001"
    assert result.finish_reason == "stop"
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert transport.calls[0]["payload"]["model"] == "deepseek-v4-flash"
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer test-secret"
    assert "test-secret" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("status_code", "expected_category"),
    [
        (429, ModelFailureCategory.RATE_LIMITED),
        (500, ModelFailureCategory.HTTP_ERROR),
    ],
)
def test_adapter_does_not_retry_http_failures(
    status_code: int,
    expected_category: ModelFailureCategory,
) -> None:
    """限流和服务端错误均形成单次失败事实，不在 Adapter 内隐藏重试。"""

    transport = _RecordingTransport(
        AsyncHttpResponse(
            status_code=status_code,
            headers={"Retry-After": "7"},
            body=b'{"error":{"code":"upstream_error"}}',
        )
    )
    result = asyncio.run(
        DeepSeekAgentModelAdapter(api_key="test-secret", transport=transport).complete(
            _request()
        )
    )

    assert isinstance(result, ModelFailure)
    assert result.category is expected_category
    assert result.http_status == status_code
    assert result.retry_after_seconds == 7
    assert len(transport.calls) == 1


def test_adapter_rejects_expired_deadline_before_transport() -> None:
    """绝对 deadline 已过期时不得发送请求。"""

    transport = _RecordingTransport(_response())
    result = asyncio.run(
        DeepSeekAgentModelAdapter(api_key="test-secret", transport=transport).complete(
            _request(deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    )

    assert isinstance(result, ModelFailure)
    assert result.category is ModelFailureCategory.DEADLINE_EXCEEDED
    assert result.request_sent is False
    assert transport.calls == []


def test_adapter_cooperatively_times_out_one_in_flight_request() -> None:
    """请求超过剩余 deadline 时协作取消，且绝不发起第二次请求。"""

    transport = _RecordingTransport(_response(), delay_seconds=0.1)
    result = asyncio.run(
        DeepSeekAgentModelAdapter(api_key="test-secret", transport=transport).complete(
            _request(deadline_at=datetime.now(timezone.utc) + timedelta(milliseconds=30))
        )
    )

    assert isinstance(result, ModelFailure)
    assert result.category is ModelFailureCategory.DEADLINE_EXCEEDED
    assert result.request_sent is True
    assert len(transport.calls) == 1


def test_adapter_rechecks_deadline_after_transport_suppresses_cancellation() -> None:
    """即使错误 Transport 吞掉取消并返回成功，Adapter 也不得在 deadline 后成功。"""

    transport = _CancellationIgnoringTransport(_response())
    result = asyncio.run(
        DeepSeekAgentModelAdapter(api_key="test-secret", transport=transport).complete(
            _request(deadline_at=datetime.now(timezone.utc) + timedelta(milliseconds=30))
        )
    )

    assert isinstance(result, ModelFailure)
    assert result.category is ModelFailureCategory.DEADLINE_EXCEEDED
    assert result.request_sent is True
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("response", "expected_category"),
    [
        (
            AsyncHttpResponse(status_code=200, headers={}, body=b"not-json"),
            ModelFailureCategory.INVALID_RESPONSE,
        ),
        (
            _response(content="not-json"),
            ModelFailureCategory.INVALID_OUTPUT_JSON,
        ),
        (
            _response(model="unexpected-model"),
            ModelFailureCategory.MODEL_IDENTITY_MISMATCH,
        ),
        (
            _response(content='{"chain_of_thought":"secret","action":"NO_ACTION"}'),
            ModelFailureCategory.FORBIDDEN_REASONING,
        ),
    ],
)
def test_adapter_classifies_invalid_or_untrusted_responses(
    response: AsyncHttpResponse,
    expected_category: ModelFailureCategory,
) -> None:
    """非法响应、模型漂移和思维链字段均 fail-closed，且不记录原始正文。"""

    transport = _RecordingTransport(response)
    result = asyncio.run(
        DeepSeekAgentModelAdapter(api_key="test-secret", transport=transport).complete(
            _request()
        )
    )

    assert isinstance(result, ModelFailure)
    assert result.category is expected_category
    assert result.response_digest is not None
    serialized = result.model_dump_json()
    assert "chain_of_thought" not in serialized
    assert "secret" not in serialized
    assert len(transport.calls) == 1


def test_adapter_classifies_non_object_choice_as_invalid_response() -> None:
    """choices 首项不是对象时不能泄漏 AttributeError，也不能被当作可审计成功回执。"""

    malformed = AsyncHttpResponse(
        status_code=200,
        headers={},
        body=json.dumps(
            {
                "id": "chatcmpl-malformed-choice",
                "model": "deepseek-v4-flash",
                "choices": [[]],
            }
        ).encode("utf-8"),
    )
    result = asyncio.run(
        DeepSeekAgentModelAdapter(
            api_key="test-secret",
            transport=_RecordingTransport(malformed),
        ).complete(_request())
    )

    assert isinstance(result, ModelFailure)
    assert result.category is ModelFailureCategory.INVALID_RESPONSE


def test_adapter_rejects_blank_provider_receipt_fields() -> None:
    """空白 Provider ID 或 finish reason 不能伪装成正式 smoke 的完整回执。"""

    result = asyncio.run(
        DeepSeekAgentModelAdapter(
            api_key="test-secret",
            transport=_RecordingTransport(
                _response(provider_response_id=" ", finish_reason="\t")
            ),
        ).complete(_request())
    )

    assert isinstance(result, ModelFailure)
    assert result.category is ModelFailureCategory.INVALID_RESPONSE


def test_adapter_rejects_excessively_nested_output_without_exception_escape() -> None:
    """恶意深层 JSON 必须稳定失败，不能以 RecursionError 中断评估 Worker。"""

    deep_json = "[" * 1100 + "0" + "]" * 1100
    result = asyncio.run(
        DeepSeekAgentModelAdapter(
            api_key="test-secret",
            transport=_RecordingTransport(_response(content=deep_json)),
        ).complete(_request())
    )

    assert isinstance(result, ModelFailure)
    assert result.category is ModelFailureCategory.INVALID_OUTPUT_JSON


def test_adapter_allows_missing_usage_as_explicit_unpriced_success() -> None:
    """usage 缺失不伪造 token；后续正式评估预检可据此阻断。"""

    result = asyncio.run(
        DeepSeekAgentModelAdapter(
            api_key="test-secret",
            transport=_RecordingTransport(_response(usage=None)),
        ).complete(_request())
    )

    assert isinstance(result, ModelSuccess)
    assert result.usage is None


def test_adapter_classifies_incomplete_usage_as_invalid_response() -> None:
    """usage 对象一旦存在就必须完整，缺字段不能以 KeyError 逃逸 Port。"""

    result = asyncio.run(
        DeepSeekAgentModelAdapter(
            api_key="test-secret",
            transport=_RecordingTransport(
                _response(usage={"prompt_tokens": 90, "total_tokens": 100})
            ),
        ).complete(_request())
    )

    assert isinstance(result, ModelFailure)
    assert result.category is ModelFailureCategory.INVALID_RESPONSE


def test_transport_exception_becomes_single_stable_failure() -> None:
    """Transport 异常统一转为失败事实，不能泄露异常对象或触发重试。"""

    transport = _RecordingTransport(OSError("connection reset"))
    result = asyncio.run(
        DeepSeekAgentModelAdapter(api_key="test-secret", transport=transport).complete(
            _request()
        )
    )

    assert isinstance(result, ModelFailure)
    assert result.category is ModelFailureCategory.TRANSPORT_ERROR
    assert result.request_sent is True
    assert len(transport.calls) == 1
    assert "connection reset" not in result.model_dump_json()


def test_httpx_timeout_is_classified_as_deadline_exceeded() -> None:
    """底层 async client 的超时类型与外层 wait_for 使用相同 deadline 分类。"""

    transport = _RecordingTransport(httpx.ReadTimeout("read timed out"))
    result = asyncio.run(
        DeepSeekAgentModelAdapter(api_key="test-secret", transport=transport).complete(
            _request()
        )
    )

    assert isinstance(result, ModelFailure)
    assert result.category is ModelFailureCategory.DEADLINE_EXCEEDED
    assert result.request_sent is True
    assert len(transport.calls) == 1


def test_request_models_are_strict_frozen_and_deadline_aware() -> None:
    """请求不能携带额外字段、naive 时间或通过 copy 覆盖冻结执行控制。"""

    request = _request()
    with pytest.raises(TypeError, match="update"):
        request.model_copy(update={"model_id": "other"})
    with pytest.raises(ValidationError):
        ModelRequest.model_validate({**request.model_dump(mode="json"), "extra": True})
    with pytest.raises(ValidationError, match="timezone"):
        ModelRequest.model_validate(
            {**request.model_dump(mode="json"), "deadline_at": "2026-07-15T10:00:00"}
        )
    with pytest.raises(ValidationError, match="endpoint_host"):
        ModelRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "endpoint_host": "api.deepseek.com@evil.example",
            }
        )
    with pytest.raises(ValidationError, match="endpoint_host"):
        ModelRequest.model_validate(
            {**request.model_dump(mode="json"), "endpoint_host": "example.com"}
        )
    with pytest.raises(ValidationError, match="model_id"):
        ModelRequest.model_validate(
            {**request.model_dump(mode="json"), "model_id": "other-model"}
        )


def test_httpx_transport_sends_exactly_one_native_async_request() -> None:
    """默认生产 Transport 使用原生 async client，并且一次调用只产生一个 HTTP 请求。"""

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers={"X-Request-ID": "upstream-001"},
            content=b'{"ok":true}',
        )

    transport = HttpxAsyncHttpTransport(
        transport=httpx.MockTransport(handler),
    )
    async def invoke_and_close() -> AsyncHttpResponse:
        response = await transport.post_json(
            url="https://api.deepseek.com/chat/completions",
            headers={"Authorization": "Bearer test-secret"},
            payload={"model": "deepseek-v4-flash"},
            timeout_seconds=1,
        )
        await transport.aclose()
        return response

    response = asyncio.run(invoke_and_close())

    assert response.status_code == 200
    assert response.body == b'{"ok":true}'
    assert len(calls) == 1


def test_scripted_model_returns_one_scripted_outcome_per_call() -> None:
    """ScriptedModel 按请求序列消费结果，支持无 usage 与确定性失败。"""

    scripted = ScriptedAgentModel(
        outcomes={
            "request-001": (
                ModelSuccess(
                    request_id="request-001",
                    model_id="deepseek-v4-flash",
                    output={"action": "NO_ACTION"},
                    usage=None,
                    response_digest=HASH_A,
                    latency_ms=Decimal("1"),
                ),
                ModelFailure(
                    request_id="request-001",
                    category=ModelFailureCategory.DEADLINE_EXCEEDED,
                    request_sent=False,
                    response_digest=None,
                ),
            )
        }
    )

    first = asyncio.run(scripted.complete(_request()))
    second = asyncio.run(scripted.complete(_request()))

    assert isinstance(first, ModelSuccess)
    assert isinstance(second, ModelFailure)
    assert scripted.call_count == 2
    with pytest.raises(RuntimeError, match="exhausted"):
        asyncio.run(scripted.complete(_request()))
