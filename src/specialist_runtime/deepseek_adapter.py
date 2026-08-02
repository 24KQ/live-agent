"""OpenAI-compatible DeepSeek 单次 async Adapter。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
import time
from typing import Any, Callable, Protocol

import httpx
from pydantic import ValidationError

from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelOutcome,
    ModelRequest,
    ModelSuccess,
    ModelUsage,
)


@dataclass(frozen=True, slots=True)
class AsyncHttpResponse:
    """Transport 返回的最小 HTTP 事实；Adapter 不持久化 header 或正文。"""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


class AsyncHttpTransport(Protocol):
    """可替换的单次 async HTTP transport。"""

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> AsyncHttpResponse:
        """发送一个 POST；实现不得在内部重试。"""


class HttpxAsyncHttpTransport:
    """使用原生 async client 的一次 HTTP 请求，不配置 transport 重试。"""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Client 与连接池由该 Transport 独占，调用方在生命周期结束时调用 aclose。
        # 显式连接池限制：最多 5 条 keep-alive 连接、10 条总连接；
        # 60s 无活动后释放 keep-alive，减少与 DeepSeek 服务端的连接重建。
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            limits=httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=60.0,
            ),
        )

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> AsyncHttpResponse:
        response = await self._client.post(
            url,
            headers=headers,
            json=payload,
            timeout=httpx.Timeout(timeout_seconds),
        )
        return AsyncHttpResponse(
            status_code=response.status_code,
            headers=dict(response.headers.items()),
            body=response.content,
        )

    async def aclose(self) -> None:
        """关闭独占连接池；重复关闭由 httpx 按幂等语义处理。"""

        await self._client.aclose()


class DeepSeekAgentModelAdapter:
    """按绝对 deadline 执行一次 DeepSeek chat completion。"""

    _FORBIDDEN_REASONING_KEYS = {
        "chain_of_thought",
        "chain-of-thought",
        "reasoning_content",
    }
    _MAX_RESPONSE_BYTES = 1_048_576
    _MAX_OUTPUT_DEPTH = 64

    def __init__(
        self,
        *,
        api_key: str,
        transport: AsyncHttpTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._transport = transport or HttpxAsyncHttpTransport()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.perf_counter

    async def complete(self, request: ModelRequest) -> ModelOutcome:
        """执行且仅执行一次请求，把所有外部异常归一为稳定失败。"""

        started = self._monotonic()
        remaining = (request.deadline_at - self._clock()).total_seconds()
        if remaining <= 0:
            return self._failure(
                request,
                ModelFailureCategory.DEADLINE_EXCEEDED,
                request_sent=False,
                started=started,
            )

        payload = {
            "model": request.model_id,
            "messages": [message.model_dump(mode="json") for message in request.messages],
            "temperature": float(request.temperature),
            "max_tokens": request.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        # 通过环境变量覆写 reasoning_effort（max / high / medium / low），
        # 不设置则按 API 默认行为（不传此参数）。
        reasoning_effort = os.environ.get("LLM_API_REASONING_EFFORT", "").strip()
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        try:
            response = await asyncio.wait_for(
                self._transport.post_json(
                    url=f"https://{request.endpoint_host}/v1/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self._api_key}",
                    },
                    payload=payload,
                    timeout_seconds=remaining,
                ),
                timeout=remaining,
            )
        except (TimeoutError, httpx.TimeoutException):
            return self._failure(
                request,
                ModelFailureCategory.DEADLINE_EXCEEDED,
                request_sent=True,
                started=started,
            )
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - asyncio.CancelledError 是 BaseException 子类（Python ≥3.8），
            # 必须显式捕获，否则外层 wait_for 超时时 CancelledError 穿透并泄漏正文。
            return self._failure(
                request,
                ModelFailureCategory.TRANSPORT_ERROR,
                request_sent=True,
                started=started,
            )

        response_digest = sha256(response.body).hexdigest()
        # wait_for 依赖被调用协程传播取消；对不协作 Transport 必须在返回后再次
        # 检查权威绝对时间，禁止 deadline 之后的响应被误记为成功。
        if self._clock() >= request.deadline_at:
            return self._failure(
                request,
                ModelFailureCategory.DEADLINE_EXCEEDED,
                request_sent=True,
                started=started,
                response_digest=response_digest,
            )
        if not 200 <= response.status_code < 300:
            category = (
                ModelFailureCategory.RATE_LIMITED
                if response.status_code == 429
                else ModelFailureCategory.HTTP_ERROR
            )
            return self._failure(
                request,
                category,
                request_sent=True,
                started=started,
                response_digest=response_digest,
                http_status=response.status_code,
                retry_after_seconds=self._parse_retry_after(response.headers),
            )

        if len(response.body) > self._MAX_RESPONSE_BYTES:
            return self._failure(
                request,
                ModelFailureCategory.INVALID_RESPONSE,
                request_sent=True,
                started=started,
                response_digest=response_digest,
            )
        try:
            envelope = json.loads(response.body.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("response envelope must be an object")
            response_model = envelope["model"]
            choice = envelope["choices"][0]
            content = choice["message"]["content"]
            if content is None:
                content = ""
            # OpenAI-compatible 返回中的 id/finish_reason 对普通调用保持可选；
            # Phase 16 正式 smoke 会在更窄的 receipt 门禁中把两者提升为必填。
            provider_response_id = envelope.get("id")
            finish_reason = choice.get("finish_reason")
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return self._failure(
                request,
                ModelFailureCategory.INVALID_RESPONSE,
                request_sent=True,
                started=started,
                response_digest=response_digest,
            )

        if response_model != request.model_id:
            return self._failure(
                request,
                ModelFailureCategory.MODEL_IDENTITY_MISMATCH,
                request_sent=True,
                started=started,
                response_digest=response_digest,
            )

        try:
            output = json.loads(content)
        except (TypeError, json.JSONDecodeError, RecursionError):
            recovered = self._recover_json(content)
            if recovered is not None:
                output = recovered
            else:
                # 诊断：输出失败内容的片段和长度，帮助定位 JSON 模式返回非 JSON 的原因
                snippet = content[:200].replace("\r", "\\r").replace("\n", "\\n")
                print(f"  [JSON DECODE FAILED] len={len(content)} snippet={snippet}")
                return self._failure(
                    request,
                    ModelFailureCategory.INVALID_OUTPUT_JSON,
                    request_sent=True,
                    started=started,
                    response_digest=response_digest,
                )
        output_issue = self._inspect_output(output)
        if output_issue is not None:
            return self._failure(
                request,
                output_issue,
                request_sent=True,
                started=started,
                response_digest=response_digest,
            )

        try:
            usage = self._parse_usage(envelope.get("usage"))
            return ModelSuccess(
                request_id=request.request_id,
                model_id=response_model,
                output=output,
                usage=usage,
                provider_response_id=provider_response_id,
                finish_reason=finish_reason,
                response_digest=response_digest,
                latency_ms=self._latency_ms(started),
            )
        except (KeyError, ValidationError, ValueError):
            return self._failure(
                request,
                ModelFailureCategory.INVALID_RESPONSE,
                request_sent=True,
                started=started,
                response_digest=response_digest,
            )

    @staticmethod
    def _parse_usage(value: Any) -> ModelUsage | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("usage must be an object")
        # 网关把 reasoning_content（思维链）计入 completion_tokens，同时在该明细里
        # 如实上报 reasoning_tokens。只接受整数，避免网关以浮点或缺失形式上报时
        # 误伤响应：拿不到明细就按历史语义视为未上报（reasoning_tokens=None）。
        details = value.get("completion_tokens_details")
        reasoning_tokens = None
        if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
            reasoning_tokens = details["reasoning_tokens"]
        return ModelUsage(
            input_tokens=value["prompt_tokens"],
            output_tokens=value["completion_tokens"],
            total_tokens=value["total_tokens"],
            reasoning_tokens=reasoning_tokens,
        )

    @classmethod
    def _recover_json(cls, content: str) -> dict[str, Any] | list[Any] | None:
        """尝试从模型返回的原始文本中恢复 JSON 对象。

        DeepSeek JSON mode 仍偶有返回空 content 或 markdown 包裹的情况。
        这些是已知的 API 边界行为，Adpater 应在放弃前尝试一次恢复，
        使 downstream 获得更大的有效样本量。
        """
        stripped = content.strip()
        if not stripped:
            return None

        # 尝试剥离 markdown 代码块标记（```json / ```）
        if stripped.startswith("```"):
            for delim in ("```json\n", "```json\r\n", "```\n", "```\r\n"):
                if stripped.startswith(delim):
                    inner = stripped.removeprefix(delim).removesuffix("```").strip()
                    if inner:
                        try:
                            return json.loads(inner)
                        except (json.JSONDecodeError, TypeError, RecursionError):
                            pass
                    break

            # 更宽泛：找到第一个 { 或 [，忽略前面的内容
            for start_idx in range(len(stripped)):
                ch = stripped[start_idx]
                if ch in ("{", "["):
                    try:
                        return json.loads(stripped[start_idx:])
                    except (json.JSONDecodeError, TypeError, RecursionError):
                        break

        return None

    @classmethod
    def _inspect_output(cls, value: Any) -> ModelFailureCategory | None:
        """迭代检查嵌套深度和思维链 key，避免递归输入耗尽 Python 栈。"""

        pending: list[tuple[Any, int]] = [(value, 0)]
        while pending:
            current, depth = pending.pop()
            if depth > cls._MAX_OUTPUT_DEPTH:
                return ModelFailureCategory.INVALID_OUTPUT_JSON
            if isinstance(current, dict):
                if any(
                    str(key).lower() in cls._FORBIDDEN_REASONING_KEYS
                    for key in current
                ):
                    return ModelFailureCategory.FORBIDDEN_REASONING
                pending.extend((item, depth + 1) for item in current.values())
            elif isinstance(current, list):
                pending.extend((item, depth + 1) for item in current)
        return None

    @staticmethod
    def _parse_retry_after(headers: Mapping[str, str]) -> int | None:
        value = next(
            (item for key, item in headers.items() if key.lower() == "retry-after"),
            None,
        )
        if value is None:
            return None
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return None
        return max(seconds, 0)

    def _latency_ms(self, started: float) -> Decimal:
        return Decimal(str(max((self._monotonic() - started) * 1000, 0)))

    def _failure(
        self,
        request: ModelRequest,
        category: ModelFailureCategory,
        *,
        request_sent: bool,
        started: float,
        response_digest: str | None = None,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> ModelFailure:
        return ModelFailure(
            request_id=request.request_id,
            category=category,
            request_sent=request_sent,
            response_digest=response_digest,
            http_status=http_status,
            retry_after_seconds=retry_after_seconds,
            latency_ms=self._latency_ms(started),
        )
