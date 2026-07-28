"""Phase 16 V5 受控 E2E 的独立 DeepSeek 禁思考 Adapter。

该模块不修改 V1 至 V4 已冻结的 Adapter。它只在 V5 的显式命令入口中把 DeepSeek 顶层
``thinking`` 固定为 ``disabled``，其余 HTTP、deadline、模型身份、usage 和 JSON 安全规则
继续由共享 ``DeepSeekAgentModelAdapter`` 执行。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Callable

from src.specialist_runtime.deepseek_adapter import (
    AsyncHttpResponse,
    AsyncHttpTransport,
    DeepSeekAgentModelAdapter,
    HttpxAsyncHttpTransport,
)
from src.specialist_runtime.model_port import ModelOutcome, ModelRequest


class DeepSeekV5ThinkingMode(StrEnum):
    """V5 真实模型证据唯一允许的思考模式，拒绝由 CLI 或环境变量覆盖。"""

    DISABLED = "disabled"


class _V5ThinkingDisabledTransport:
    """仅为 V5 请求添加禁思考字段的窄传输装饰器，不保留响应正文或 Provider 标识。"""

    def __init__(self, delegate: AsyncHttpTransport) -> None:
        """保存共享 HTTP Transport；该对象不缓存请求、响应或任何凭据。"""

        self._delegate = delegate

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> AsyncHttpResponse:
        """在唯一请求发送前固定顶层 thinking.disabled，继续委托共享 Transport。"""

        return await self._delegate.post_json(
            url=url,
            headers=headers,
            payload={
                **payload,
                "thinking": {"type": DeepSeekV5ThinkingMode.DISABLED.value},
            },
            timeout_seconds=timeout_seconds,
        )


class DeepSeekV5ControlledE2EAdapter:
    """V5 专属模型端口：独立禁思考边界加共享单次调用安全语义。"""

    thinking_mode = DeepSeekV5ThinkingMode.DISABLED

    def __init__(
        self,
        *,
        api_key: str,
        transport: AsyncHttpTransport | None = None,
        clock: Callable[[], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        """只把 API Key 交给共享 Adapter；V5 本身不记录、打印或持久化该值。"""

        self._delegate = DeepSeekAgentModelAdapter(
            api_key=api_key,
            transport=_V5ThinkingDisabledTransport(transport or HttpxAsyncHttpTransport()),
            clock=clock,
            monotonic=monotonic,
        )

    async def complete(self, request: ModelRequest) -> ModelOutcome:
        """执行一次共享 Adapter 调用；V5 不增加 fallback、重试或输出修补。"""

        return await self._delegate.complete(request)
