"""Phase 16 V5 受控 E2E 的独立 DeepSeek 禁思考 Adapter。

该模块不修改 V1 至 V4 已冻结的 Adapter。它只在 V5 的显式命令入口中把 DeepSeek 顶层
``thinking`` 固定为 ``disabled``，其余 HTTP、deadline、模型身份、usage 和 JSON 安全规则
继续由共享 ``DeepSeekAgentModelAdapter`` 执行。

V9 起该包装层同时承担传输层重试与渠道链：只对可证明的瞬态失败（TRANSPORT_ERROR、
HTTP 5xx）在同一端点内最多重试一次，受绝对 deadline 约束；RATE_LIMITED / DEADLINE_EXCEEDED
不重试。每次完整返回值都带上 attempts 与 endpoint_host 事实，供 execution ledger
receipt 审计。

V9 矩阵配置（Phase B）：渠道是**有序列表**（顺序即优先级），运行时在 profiles.py 闭包
认证的白名单集合内挑选；模型 / 思考强度 / 渠道列表的白名单外值在此 fail-fast 拒绝，
receipt/campaign 行钉死实际组合。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import StrEnum
import os
from typing import Any, Awaitable, Callable

from src.specialist_runtime.deepseek_adapter import (
    AsyncHttpResponse,
    AsyncHttpTransport,
    DeepSeekAgentModelAdapter,
    HttpxAsyncHttpTransport,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelOutcome,
    ModelRequest,
    ModelSuccess,
)
from src.specialist_runtime.profiles import (
    FORMAL_ENDPOINT_HOSTS,
    FORMAL_MODEL_IDS,
    FORMAL_REASONING_EFFORTS,
    normalize_endpoint_host,
)


class DeepSeekV5ThinkingMode(StrEnum):
    """V5 真实模型证据唯一允许的思考模式，拒绝由 CLI 或环境变量覆盖。"""

    DISABLED = "disabled"


class _V5ThinkingDisabledTransport:
    """仅为 V5 请求添加禁思考与白名单 reasoning_effort 的窄传输装饰器。

    不保留响应正文或 Provider 标识。reasoning_effort 在构造时已通过白名单校验并固定，
    请求发送时直接钉进 payload —— V5 路径的思考强度注入以本闭包为准，不依赖
    共享 Adapter 的 env 直读。
    """

    def __init__(
        self,
        delegate: AsyncHttpTransport,
        *,
        reasoning_effort: str | None = None,
    ) -> None:
        """保存共享 HTTP Transport；该对象不缓存请求、响应或任何凭据。"""

        self._delegate = delegate
        self._reasoning_effort = reasoning_effort

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> AsyncHttpResponse:
        """在唯一请求发送前固定顶层 thinking.disabled 与白名单 reasoning_effort。"""

        pinned = {
            **payload,
            "thinking": {"type": DeepSeekV5ThinkingMode.DISABLED.value},
        }
        if self._reasoning_effort is not None:
            pinned["reasoning_effort"] = self._reasoning_effort
        return await self._delegate.post_json(
            url=url,
            headers=headers,
            payload=pinned,
            timeout_seconds=timeout_seconds,
        )


def _stamp_attempt(outcome: ModelOutcome, *, attempts: int, endpoint_host: str) -> ModelOutcome:
    """给冻结结果补 attempts/endpoint_host 事实。

    ``StrictFrozenModel`` 禁止 ``model_copy(update=...)``（封闭免校验更新入口），
    因此用 JSON 往返重建：全部字段原值回传，只覆盖两个新增事实字段。
    往返会重跑 validator，但 stamp 的是请求端点的 normalized 值与非负尝试计数，
    不会改变 outcome 的语义。
    """

    payload = outcome.model_dump(mode="json")
    payload["attempts"] = attempts
    payload["endpoint_host"] = endpoint_host
    return type(outcome).model_validate(payload)


class DeepSeekV5ControlledE2EAdapter:
    """V5 专属模型端口：独立禁思考边界加共享单次调用安全语义。

    传输层重试（V9）：每个端点内最多尝试 ``_MAX_ATTEMPTS_PER_ENDPOINT`` 次。
    只重试 TRANSPORT_ERROR（立即）与 HTTP 5xx（退避 ``_RETRY_BACKOFF_SECONDS``，
    且不超过剩余 deadline）；RATE_LIMITED（429）不重试同端点，改走渠道链下一端点。

    渠道链（V9 Phase B）：构造时提供 ``endpoints`` 有序列表（host, api_key) 二元组，
    顺序即优先级。完整序列为端点 1（最多 2 次）→ 端点 2（最多 2 次）→ ……；
    429 触发换端；端点耗尽或换端前都会检查绝对 deadline，剩余不足立即停止，
    不碰后续渠道。

    每次返回都带 ``attempts``（全链实际调用总数）与 ``endpoint_host``（实际响应
    端点）。各端点各自持独立的 API Key，由调用方显式注入；V5 本身不记录、
    打印或持久化任何密钥。
    """

    thinking_mode = DeepSeekV5ThinkingMode.DISABLED

    #: 同一端点内最多尝试次数（1 次原始调用 + 1 次重试）。
    _MAX_ATTEMPTS_PER_ENDPOINT = 2

    #: HTTP 5xx 重试前的固定退避上限；TRANSPORT_ERROR 立即重试。
    _RETRY_BACKOFF_SECONDS = 1.0

    def __init__(
        self,
        *,
        endpoints: tuple[tuple[str, str], ...],
        transport: AsyncHttpTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """校验渠道有序列表与运行时矩阵白名单，逐端点装配独立共享 Adapter。

        白名单强制（闭包内 fail-fast）：
        - 每个端点 host 必须 normalize 后 ∈ FORMAL_ENDPOINT_HOSTS，key 非空，host 不重复；
        - env ``LLM_API_REASONING_EFFORT`` 若设置必须 ∈ FORMAL_REASONING_EFFORTS，
          并通过 V5 传输装饰器在请求发送时固定进 payload；
        - env ``LLM_API_MODEL_ID`` 若设置必须 ∈ FORMAL_MODEL_IDS（请求级校验的提前拦截）。
        """

        if not endpoints:
            raise ValueError("endpoints must be a non-empty ordered channel list")
        normalized = tuple(
            (normalize_endpoint_host(host), api_key) for host, api_key in endpoints
        )
        if len(normalized) != len({host for host, _ in normalized}):
            raise ValueError("endpoints hosts must be unique")
        for host, api_key in normalized:
            if host not in FORMAL_ENDPOINT_HOSTS:
                raise ValueError(f"endpoint host must be one of {sorted(FORMAL_ENDPOINT_HOSTS)}")
            if not api_key:
                raise ValueError("each channel endpoint requires a non-empty api key")

        reasoning_effort = os.environ.get("LLM_API_REASONING_EFFORT", "").strip() or None
        if reasoning_effort is not None and reasoning_effort not in FORMAL_REASONING_EFFORTS:
            raise ValueError(
                f"LLM_API_REASONING_EFFORT must be one of {sorted(FORMAL_REASONING_EFFORTS)}"
            )
        model_id = os.environ.get("LLM_API_MODEL_ID", "").strip() or None
        if model_id is not None and model_id not in FORMAL_MODEL_IDS:
            raise ValueError(f"LLM_API_MODEL_ID must be one of {sorted(FORMAL_MODEL_IDS)}")

        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or asyncio.sleep
        base_transport = transport or HttpxAsyncHttpTransport()
        self._chain: tuple[tuple[DeepSeekAgentModelAdapter, str], ...] = tuple(
            (self._build_delegate(host, api_key, base_transport, reasoning_effort, clock, monotonic), host)
            for host, api_key in normalized
        )

    @staticmethod
    def _build_delegate(
        host: str,
        api_key: str,
        base_transport: AsyncHttpTransport,
        reasoning_effort: str | None,
        clock: Callable[[], datetime] | None,
        monotonic: Callable[[], float] | None,
    ) -> DeepSeekAgentModelAdapter:
        """host 已在上层通过 normalize + FORMAL 白名单；此处仅防御性重校验。"""

        normalized = normalize_endpoint_host(host)
        if normalized not in FORMAL_ENDPOINT_HOSTS:
            raise ValueError(f"endpoint host must be one of {sorted(FORMAL_ENDPOINT_HOSTS)}")
        if not api_key:
            raise ValueError("each channel endpoint requires a non-empty api key")
        return DeepSeekAgentModelAdapter(
            api_key=api_key,
            transport=_V5ThinkingDisabledTransport(
                base_transport,
                reasoning_effort=reasoning_effort,
            ),
            clock=clock,
            monotonic=monotonic,
        )

    @staticmethod
    def _retryable(outcome: ModelFailure) -> bool:
        """只重试可证明的瞬态失败；限流与超时交给渠道链或上层，不在端点内消耗时间。"""

        if outcome.category is ModelFailureCategory.TRANSPORT_ERROR:
            return True
        return outcome.http_status is not None and outcome.http_status >= 500

    @staticmethod
    def _remaining_seconds(request: ModelRequest, clock: Callable[[], datetime]) -> float:
        """剩余绝对 deadline；<=0 时任何重试或换端都不再发生。"""

        return (request.deadline_at - clock()).total_seconds()

    async def complete(self, request: ModelRequest) -> ModelOutcome:
        """按渠道有序列表在绝对 deadline 内执行，每端点最多 2 次调用。

        成功返回（或最后一次失败）统一带上 attempts 与 endpoint_host 事实。
        429 只换端不重试；TRANSPORT_ERROR/5xx 在同端点重试一次；DEADLINE_EXCEEDED
        立即返回；换端前剩余 deadline 不足则不再触碰后续渠道。
        """

        attempts = 0
        last_outcome: ModelOutcome | None = None
        endpoint_request = request
        for delegate, host in self._chain:
            endpoint_request = request
            if host != request.endpoint_host:
                # StrictFrozenModel 禁止 model_copy(update=...)；重建会重跑 endpoint
                # 校验，渠道 host 已在上层手工通过 normalize + FORMAL 白名单。
                payload = request.model_dump(mode="json")
                payload["endpoint_host"] = host
                endpoint_request = ModelRequest.model_validate(payload)
            for _ in range(self._MAX_ATTEMPTS_PER_ENDPOINT):
                attempts += 1
                outcome = await delegate.complete(endpoint_request)
                if isinstance(outcome, ModelSuccess):
                    return _stamp_attempt(
                        outcome,
                        attempts=attempts,
                        endpoint_host=endpoint_request.endpoint_host,
                    )
                last_outcome = outcome
                if self._retryable(outcome):
                    if self._remaining_seconds(request, self._clock) <= 0:
                        break
                    if outcome.http_status is not None and outcome.http_status >= 500:
                        await self._sleep(
                            min(self._RETRY_BACKOFF_SECONDS, self._remaining_seconds(request, self._clock))
                        )
                    continue
                if outcome.category is ModelFailureCategory.RATE_LIMITED:
                    # 429 不重试同端点：直接走渠道链下一端点（换端前仍受 deadline 门约束）。
                    break
                return _stamp_attempt(
                    outcome,
                    attempts=attempts,
                    endpoint_host=endpoint_request.endpoint_host,
                )
            if self._remaining_seconds(request, self._clock) <= 0:
                break
        assert last_outcome is not None
        return _stamp_attempt(
            last_outcome,
            attempts=attempts,
            endpoint_host=endpoint_request.endpoint_host,
        )
