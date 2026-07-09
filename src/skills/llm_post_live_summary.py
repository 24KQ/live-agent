"""Phase 5D LLM 播后复盘总结器。

在 Phase 4A 结构化复盘基础上，用 LLM 生成自然语言播后总结。
LLM 不可用时降级到确定性结构化模板，不阻塞播后流程。

总结包含三部分：
1. 本场概览：采纳率、准确率、总决策数
2. 发现问题：拒绝有效建议、采纳无效建议等
3. 后续建议：优化方向

复用 Phase 3E 的 LLM API 配置（llm_api_base_url、llm_api_key 等），
不加新依赖，用标准库 urllib 调用。
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any

from src.config.settings import Settings, get_settings


def build_review_prompt(attribution: dict[str, Any], issues: list[str]) -> str:
    """构造 LLM 复盘 prompt。

    参数:
        attribution: 归因指标字典（total_decisions, adoption_rate, accuracy_rate 等）
        issues: 发现的问题列表

    返回:
        完整的 system + user prompt 字符串
    """
    total = attribution.get("total_decisions", 0)
    adoption = attribution.get("adoption_rate", 0)
    accuracy = attribution.get("accuracy_rate", 0)
    unattributable = attribution.get("unattributable_count", 0)

    issues_text = "\n".join(f"- {issue}" for issue in issues) if issues else "无"

    return f"""你是一个直播复盘分析师。请根据以下数据生成一场直播的自然语言复盘总结。

## 本场数据

- 总决策数：{total}
- 采纳率：{adoption * 100:.1f}%
- 准确率：{accuracy * 100:.1f}%
- 不可归因记录：{unattributable} 条

## 发现的问题

{issues_text}

## 输出要求

请用中文输出，包含以下三部分：

1. **本场概览** — 用一两句话总结本场表现，突出采纳率和准确率
2. **发现问题** — 如果存在问题请指出，不存在则写"本场无明显问题"
3. **后续建议** — 针对发现的问题给出 1-2 条改进建议

只输出复盘内容，不要加额外说明。"""


def build_structured_fallback(attribution: dict[str, Any], issues: list[str]) -> str:
    """LLM 不可用时输出确定性结构化报告。

    参数与 build_review_prompt 相同。
    """
    total = attribution.get("total_decisions", 0)
    adoption = attribution.get("adoption_rate", 0)
    accuracy = attribution.get("accuracy_rate", 0)
    unattributable = attribution.get("unattributable_count", 0)

    lines: list[str] = []
    lines.append("=" * 40)
    lines.append("播后复盘报告（结构化降级）")
    lines.append("=" * 40)
    lines.append(f"总决策数：{total}")
    lines.append(f"采纳率：{adoption * 100:.1f}%")
    lines.append(f"准确率：{accuracy * 100:.1f}%")
    lines.append(f"不可归因记录：{unattributable} 条")
    lines.append("")

    if issues:
        lines.append("发现的问题：")
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("发现的问题：无")

    lines.append("")
    lines.append("后续建议：LLM 不可用，建议手动查看决策详情")
    lines.append("=" * 40)

    return "\n".join(lines)


class LLMPostLiveSummary:
    """LLM 播后复盘总结器。

    用法：
        summarizer = LLMPostLiveSummary()
        report = summarizer.generate(attribution=..., issues=...)
    """

    def __init__(self, settings: Settings | None = None) -> None:
        if settings is None:
            try:
                settings = get_settings()
            except Exception:
                settings = None

        if settings is not None:
            self._base_url = (settings.llm_api_base_url or "https://api.deepseek.com").rstrip("/")
            self._api_key = settings.llm_api_key or ""
            self._model = settings.llm_model or "deepseek-v4-flash"
            self._max_tokens = settings.llm_max_tokens or 1000
            self._temperature = settings.llm_temperature or 0.3
            self._timeout = settings.llm_timeout_seconds or 15
        else:
            self._base_url = "https://api.deepseek.com"
            self._api_key = ""
            self._model = "deepseek-v4-flash"
            self._max_tokens = 1000
            self._temperature = 0.3
            self._timeout = 15

    def generate(self, attribution: dict[str, Any], issues: list[str]) -> str:
        """生成 LLM 复盘总结。

        先尝试调用 LLM，失败时降级到结构化模板。
        """
        if not attribution:
            return "播后复盘：无决策数据"

        try:
            if not self._api_key:
                raise RuntimeError("LLM API key not configured")

            prompt = build_review_prompt(attribution, issues)
            return self._call_llm(prompt)

        except Exception:
            return build_structured_fallback(attribution, issues)

    def _call_llm(self, user_prompt: str) -> str:
        """调用 DeepSeek chat completions API。"""
        url = f"{self._base_url}/chat/completions"
        body = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": "你是一个直播复盘分析师。请基于数据生成自然语言的复盘总结。"},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"LLM API call failed: {exc}") from exc

        return data["choices"][0]["message"]["content"]
