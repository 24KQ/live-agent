# -*- coding: utf-8 -*-
"""统一 LLM 客户端。

封装 urllib 调用 DeepSeek OpenAI 兼容 API，支持：
- 指数退避重试（0.5s / 2s / 10s）
- 异常细分：网络错误、超时、模型乱码
- Token 消耗和延迟追踪
- 重试耗尽后降级标记

两处 LLM 调用（OnLiveHarnessPlanner、LLMPostLiveSummary）都通过此类统一，
消除重复的 _call_llm 实现。

用法：
    client = LLMClient(api_key="sk-xxx")
    resp = client.call(
        user_prompt="你好",
        system_prompt="你是一个助手",
    )
    if resp.fallback_triggered:
        # 降级处理，不走 LLM
        pass
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class LLMNetworkError(Exception):
    """网络层错误：连接断开、DNS 解析失败、鉴权失败（HTTP 4xx）等。"""


class LLMTimeoutError(Exception):
    """请求超时或连接重置。"""


class LLMResponseError(Exception):
    """模型返回了不可解析或非预期的内容（乱码、格式错误）。"""


@dataclass
class LLMResponse:
    """一次 LLM 调用的结构化结果。"""

    content: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: float | None = None
    model: str = ""
    retry_count: int = 0
    fallback_triggered: bool = False


class LLMClient:
    """统一 LLM 客户端，封装重试和异常分类。"""

    _DEFAULT_RETRY_DELAYS = [0.5, 2.0, 10.0]
    _NON_RETRYABLE_ERROR_CODES = {400, 401, 403, 422}

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout_seconds: int = 15,
        max_tokens: int = 500,
        temperature: float = 0.2,
        retry_delays: list[float] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._retry_delays = retry_delays or list(self._DEFAULT_RETRY_DELAYS)

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    def call(
        self,
        user_prompt: str,
        *,
        system_prompt: str = "",
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """发起 LLM API 调用，带重试和异常分类。

        重试策略：
        - HTTP 4xx（除 429 限流）：不重试，直接抛出 LLMNetworkError。
        - HTTP 429 / 网络错误 / 超时：指数退避重试 3 次。
        - 模型返回乱码（JSONDecodeError）：重试 3 次后抛出 LLMResponseError。
        - 无 API Key：直接返回 fallback_triggered=True，不抛异常。

        参数:
            user_prompt: 用户消息内容（必填）。
            system_prompt: 系统消息（可选）。
            model: 模型名，不传则用构造函数默认值。
            max_tokens: 最大生成长度，不传则用构造函数默认值。
            temperature: 采样温度，不传则用构造函数默认值。

        返回:
            LLMResponse，成功时 content 为模型回复文本，
            fallback_triggered 为 True 时不抛异常，调用方应走降级。
        """
        if not self._api_key:
            return LLMResponse(
                content="",
                prompt_tokens=0,
                completion_tokens=0,
                fallback_triggered=True,
            )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        url = self._base_url + "/chat/completions"
        body = json.dumps(
            {
                "model": model or self._model,
                "messages": messages,
                "max_tokens": max_tokens or self._max_tokens,
                "temperature": temperature or self._temperature,
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self._api_key,
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        start = time.perf_counter()
        retry_count = 0

        for attempt, delay in enumerate(self._retry_delays + [0]):
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
            except urllib.error.HTTPError as exc:
                # HTTP 4xx：鉴权、参数错误等——不重试，立即失败
                if exc.code in self._NON_RETRYABLE_ERROR_CODES or (400 <= exc.code < 500 and exc.code != 429):
                    raise LLMNetworkError(
                        f"LLM API 返回 HTTP {exc.code}（不重试）: {exc.reason}"
                    ) from exc
                # 5xx 或 429（限流）：可以重试
                if attempt < len(self._retry_delays):
                    time.sleep(delay)
                    retry_count += 1
                    continue
                raise LLMNetworkError(
                    f"LLM API HTTP 错误（尝试 {retry_count} 次后放弃）: {exc.code} {exc.reason}"
                ) from exc
            except urllib.error.URLError as exc:
                # 网络错误（连接断开、DNS 解析失败）
                if attempt < len(self._retry_delays):
                    time.sleep(delay)
                    retry_count += 1
                    continue
                raise LLMNetworkError(
                    f"LLM API 网络错误（尝试 {retry_count} 次后放弃）: {exc.reason}"
                ) from exc
            except json.JSONDecodeError as exc:
                # 模型返回了不可解析的内容，可以重试
                if attempt < len(self._retry_delays):
                    time.sleep(delay)
                    retry_count += 1
                    continue
                raise LLMResponseError(
                    f"模型返回了不可解析的 JSON（尝试 {retry_count} 次后放弃）: {exc}"
                ) from exc
            except OSError as exc:
                # 超时或连接重置
                if attempt < len(self._retry_delays):
                    time.sleep(delay)
                    retry_count += 1
                    continue
                raise LLMTimeoutError(
                    f"LLM API 超时或连接重置（尝试 {retry_count} 次后放弃）: {exc}"
                ) from exc

            # 成功路径
            elapsed = (time.perf_counter() - start) * 1000
            usage = data.get("usage", {}) or {}
            return LLMResponse(
                content=data["choices"][0]["message"]["content"],
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                latency_ms=round(elapsed, 2),
                model=data.get("model", model or self._model),
                retry_count=retry_count,
                fallback_triggered=False,
            )

        # 理论不可达，防御性返回 fallback
        elapsed = (time.perf_counter() - start) * 1000
        return LLMResponse(
            content="",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=round(elapsed, 2),
            model=model or self._model,
            retry_count=retry_count,
            fallback_triggered=True,
        )
