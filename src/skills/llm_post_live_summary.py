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
from typing import Any

from src.skills.llm_client import LLMClient

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
            base_url = (settings.llm_api_base_url or "https://api.deepseek.com").rstrip("/")
            api_key_val = settings.llm_api_key or ""
            model = settings.llm_model or "deepseek-v4-flash"
            max_tokens = settings.llm_max_tokens or 1000
            temperature = settings.llm_temperature or 0.3
            timeout = settings.llm_timeout_seconds or 15
        else:
            base_url = "https://api.deepseek.com"
            api_key_val = ""
            model = "deepseek-v4-flash"
            max_tokens = 1000
            temperature = 0.3
            timeout = 15
        self._llm_client = LLMClient(
            api_key=api_key_val,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
        )

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
            resp = self._llm_client.call(
                user_prompt=prompt,
                system_prompt="你是一个直播复盘分析师。请基于数据生成自然语言的复盘总结。",
            )
            if resp.fallback_triggered:
                raise RuntimeError("LLM call fallback: no api key or all retries exhausted")
            return resp.content

        except Exception:
            return build_structured_fallback(attribution, issues)


