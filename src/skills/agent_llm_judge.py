# -*- coding: utf-8 -*-
"""Phase 7A Agent LLM Judge。

Judge 只评估“建议语义质量”这一低权重维度，不能修改安全、人审和工具合规
结果。实现上要求模型返回 JSON，并通过 Pydantic 校验；超时、限流、非法 JSON
都会返回 partial，不影响规则评分。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from src.config.settings import Settings, get_settings


class AgentLLMJudgeResult(BaseModel):
    """LLM Judge 的结构化结果。"""

    status: str = "completed"
    score: float | None = None
    relevance: float | None = None
    actionability: float | None = None
    context_consistency: float | None = None
    reason: str = ""
    confidence: float | None = None
    model: str = ""
    prompt_version: str = "judge-v1"
    token_usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    error: str | None = None


class _JudgeSchema(BaseModel):
    relevance: float = Field(..., ge=0, le=1)
    actionability: float = Field(..., ge=0, le=1)
    context_consistency: float = Field(..., ge=0, le=1)
    reason: str
    confidence: float = Field(..., ge=0, le=1)


def build_judge_prompt(*, final_suggestion: str, context_summary: str) -> str:
    """构造只要求 JSON 输出的 Judge prompt。"""

    return f"""你是直播 Agent 建议质量评审器。请只输出 JSON，不要输出 Markdown。

评分维度：
- 相关性 relevance：建议是否回应当前上下文。
- 可执行性 actionability：主播是否能立刻照着做。
- 上下文一致性 context_consistency：是否与弹幕、库存和风险状态一致。

上下文摘要：
{context_summary}

Agent 最终建议：
{final_suggestion}

JSON 字段固定为：
{{"relevance":0.0-1.0,"actionability":0.0-1.0,"context_consistency":0.0-1.0,"reason":"中文理由","confidence":0.0-1.0}}"""


class AgentLLMJudge:
    """DeepSeek/OpenAI 兼容的结构化 Judge。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_post: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._http_post = http_post or self._default_http_post
        self._model = self._settings.llm_model

    def judge(self, *, final_suggestion: str, context_summary: str) -> AgentLLMJudgeResult:
        start = time.perf_counter()
        prompt = build_judge_prompt(final_suggestion=final_suggestion, context_summary=context_summary)
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": min(self._settings.llm_max_tokens, 500),
        }
        try:
            response = self._http_post(payload)
            content = response["choices"][0]["message"]["content"]
            parsed = _JudgeSchema.model_validate_json(content)
            score = (parsed.relevance + parsed.actionability + parsed.context_consistency) / 3 * 100
            return AgentLLMJudgeResult(
                status="completed",
                score=round(score, 2),
                relevance=parsed.relevance,
                actionability=parsed.actionability,
                context_consistency=parsed.context_consistency,
                reason=parsed.reason,
                confidence=parsed.confidence,
                model=self._model,
                token_usage=dict(response.get("usage") or {}),
                latency_ms=int((time.perf_counter() - start) * 1000),
            )
        except (KeyError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            return self._partial_result(start, f"invalid judge response: {exc}")
        except Exception as exc:  # noqa: BLE001 - 外部模型不可用时只能标记 partial。
            return self._partial_result(start, f"judge call failed: {exc}")

    def _default_http_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._settings.llm_api_key or self._settings.llm_api_key == "change_me":
            raise RuntimeError("LLM API key not configured")
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._settings.llm_api_base_url.rstrip('/')}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._settings.llm_api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._settings.llm_timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(str(exc)) from exc

    def _partial_result(self, start: float, error: str) -> AgentLLMJudgeResult:
        return AgentLLMJudgeResult(
            status="partial",
            score=None,
            model=self._model,
            latency_ms=int((time.perf_counter() - start) * 1000),
            error=error,
        )
