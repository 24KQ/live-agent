"""Phase 5B LLM 兜底分类器。

当关键词分类无法覆盖时，用 LLM 对未分类弹幕做语义分类。
仅在未分类弹幕 >= 5 条时调用 LLM，避免频繁 API 请求。

兜底流程：
1. 收集 classify_danmaku_question() 返回 GENERAL 的弹幕
2. 若数量 >= 5 条，调用 LLM 批量分类
3. LLM 不可用或数量 < 5，降级为 GENERAL
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any

from src.skills.danmaku_aggregator import DanmakuQuestionCategory


class DanmakuLLMFallback:
    """LLM 兜底分类器。

    用法：
        fallback = DanmakuLLMFallback()
        results = fallback.classify_unclassified(unclassified_messages)
    """

    def __init__(self, api_base: str | None = None, api_key: str | None = None, model: str | None = None) -> None:
        self._api_base = api_base
        self._api_key = api_key
        self._model = model or "deepseek-v4-flash"

    def classify_unclassified(
        self,
        unclassified_messages: list[str],
        batch_size: int = 20,
    ) -> list[dict[str, Any]]:
        """对未分类弹幕做 LLM 兜底分类。

        参数:
            unclassified_messages: 关键词分类未命中的弹幕内容列表
            batch_size: 每批最大条数，默认 20

        返回:
            每条弹幕的分类结果，格式为 [{"content": str, "category": DanmakuQuestionCategory, "reason": str}]
            不足 5 条或 LLM 不可用时返回 GENERAL 分类。
        """
        if not unclassified_messages:
            return []

        if len(unclassified_messages) < 5:
            return []

        results: list[dict[str, Any]] = []

        # 分批处理
        for i in range(0, len(unclassified_messages), batch_size):
            batch = unclassified_messages[i:i + batch_size]
            try:
                batch_results = self._call_llm(batch)
                results.extend(batch_results)
            except Exception:
                # LLM 不可用时降级为 general
                for msg in batch:
                    results.append({
                        "content": msg,
                        "category": DanmakuQuestionCategory.GENERAL,
                        "reason": "llm_unavailable",
                    })

        return results

    def _call_llm(self, messages: list[str]) -> list[dict[str, Any]]:
        """调用 LLM API 对一批弹幕做分类。

        返回格式: [{"content": str, "category": DanmakuQuestionCategory, "reason": str}]
        """
        if not self._api_base or not self._api_key:
            # 无 API 配置时降级
            raise RuntimeError("LLM API not configured")

        valid_categories = [c.value for c in DanmakuQuestionCategory]

        system_prompt = f"""你是一个直播弹幕分类助手。请将每条弹幕分类到以下类别之一：
{", ".join(valid_categories)}

只返回 JSON 数组，每条格式：{{"content": "原文本", "category": "类别名", "reason": "分类原因"}}
不要返回其他内容。"""

        user_prompt = json.dumps([{"index": i, "content": msg} for i, msg in enumerate(messages)], ensure_ascii=False)

        payload = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        }).encode("utf-8")

        url = f"{self._api_base.rstrip('/')}/chat/completions"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
            raise RuntimeError(f"LLM API call failed: {exc}") from exc

        llm_text = ""
        try:
            llm_text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected LLM response format: {exc}") from exc

        # 解析 LLM 返回的 JSON
        try:
            parsed = json.loads(llm_text)
        except json.JSONDecodeError:
            raise RuntimeError(f"LLM returned invalid JSON: {llm_text[:200]}") from None

        if not isinstance(parsed, list):
            raise RuntimeError(f"LLM did not return a list: {type(parsed).__name__}")

        results = []
        for item in parsed:
            content = item.get("content", "")
            cat_str = item.get("category", "general")
            reason = item.get("reason", "")

            # 验证分类是否合法
            if cat_str not in valid_categories:
                cat_str = "general"

            results.append({
                "content": content,
                "category": DanmakuQuestionCategory(cat_str),
                "reason": reason,
            })

        return results
