"""Phase 5F 播中 LLM Planner。

把播中 Agent 的决策从确定性规则升级为 LLM 驱动。
LLM 不可用时降级到现有 if/else 规则，不中断流程。

复用 Phase 3E 的 DeepSeek API 配置，不加新依赖。
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any

from src.config.settings import Settings, get_settings


def build_on_live_prompt(
    danmaku: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    trust_score: float,
    memory_hints: list[tuple[str, float]] | None = None,
) -> str:
    """构造播中 LLM 决策 prompt。"

    参数:
        danmaku: 弹幕摘要列表
        alerts: 库存告警列表
        trust_score: 信任分 0.0-1.0
        memory_hints: 主播记忆偏好 [(内容, 置信度)]

    返回:
        完整的 user prompt 字符串
    """
    lines = ["你是一个直播 AI 助手，观察弹幕和库存后决定是否给主播建议。"]
    lines.append("")
    lines.append(f"信任分：{trust_score}")
    lines.append("")

    if danmaku:
        lines.append("弹幕摘要：")
        for d in danmaku:
            cat = d.get("summary", d.get("category", "未知"))
            count = d.get("count", 0)
            samples = d.get("sample_contents", [])
            samples_str = "、".join(samples[:3]) if samples else ""
            lines.append(f"  - {cat} ({count} 次){' 例如：' + samples_str if samples_str else ''}")
        lines.append("")

    if alerts:
        lines.append("库存告警：")
        for a in alerts:
            pid = a.get("product_id", "?")
            pname = a.get("product_name", "")
            sev = a.get("severity", "warning")
            lines.append(f"  - [{sev}] {pname}({pid})")
        lines.append("")

    if memory_hints:
        lines.append("主播偏好（来自历史记忆）：")
        for hint, confidence in memory_hints:
            lines.append(f"  - {hint} (置信度 {confidence})")
        lines.append("")

    lines.append("请输出 JSON 格式的决策：")
    lines.append('  {"route": "direct_plan" 或 "finish"')
    lines.append('   "goal": "简短的目标描述"')
    lines.append('   "suggestion": "给主播的建议文本，不需要干预时写 null"}')
    lines.append("")
    lines.append("约束：")
    lines.append("- 无事件时 route 为 finish，suggestion 为 null")
    lines.append("- 不要插手主播个人风格")
    lines.append("- 不要替主播报价或承诺优惠")
    lines.append("- 只返回 JSON，不要加其他文字")

    return "\n".join(lines)


def parse_on_live_decision(llm_output: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON 决策。

    参数:
        llm_output: LLM 返回的文本

    返回:
        {"route": str, "goal": str, "suggestion": str | None}

    抛出:
        ValueError: JSON 无效或缺少必要字段
    """
    text = llm_output.strip()

    # 处理 markdown 代码块
    if text.startswith("```"):
        lines = text.split("\n")
        json_lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(json_lines)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM output does not contain valid JSON")

    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"failed to parse LLM JSON: {exc}") from exc

    route = raw.get("route", "")
    goal = raw.get("goal", "")
    suggestion = raw.get("suggestion")

    if not route:
        raise ValueError("route is required")

    if route not in ("direct_plan", "finish"):
        raise ValueError(f"invalid route: {route}")

    return {
        "route": route,
        "goal": goal or "",
        "suggestion": suggestion,
    }


class OnLiveLLMPlanner:
    """播中 LLM 决策器。

    用法：
        planner = OnLiveLLMPlanner()
        decision = planner.plan(
            danmaku_summary=[...],
            inventory_alerts=[...],
            trust_score=0.7,
            memory_hints=[...],
        )
    """

    def __init__(self, settings: Settings | None = None, api_key: str = "") -> None:
        if settings is None and not api_key:
            try:
                settings = get_settings()
            except Exception:
                settings = None

        if settings is not None:
            self._base_url = (settings.llm_api_base_url or "https://api.deepseek.com").rstrip("/")
            self._api_key = settings.llm_api_key or api_key
            self._model = settings.llm_model or "deepseek-v4-flash"
            self._max_tokens = settings.llm_max_tokens or 500
            self._temperature = settings.llm_temperature or 0.3
            self._timeout = settings.llm_timeout_seconds or 15
        else:
            self._base_url = "https://api.deepseek.com"
            self._api_key = api_key
            self._model = "deepseek-v4-flash"
            self._max_tokens = 500
            self._temperature = 0.3
            self._timeout = 15

    def plan(
        self,
        danmaku_summary: list[dict[str, Any]],
        inventory_alerts: list[dict[str, Any]],
        trust_score: float,
        memory_hints: list[tuple[str, float]] | None = None,
    ) -> dict[str, Any]:
        """生成播中决策。"

        LLM 可用时优先用 LLM，失败降级到规则。
        """
        # 无事件时直接 finish，不浪费 LLM 调用
        if not danmaku_summary and not inventory_alerts:
            return {"route": "finish", "goal": "无事件，不干预", "suggestion": None}

        # 尝试 LLM
        if self._api_key:
            try:
                prompt = build_on_live_prompt(
                    danmaku=danmaku_summary,
                    alerts=inventory_alerts,
                    trust_score=trust_score,
                    memory_hints=memory_hints,
                )
                llm_result = self._call_llm(prompt)
                return parse_on_live_decision(llm_result)
            except Exception:
                pass

        # LLM 不可用时降级到规则
        return self._rule_fallback(danmaku_summary, inventory_alerts)

    def _call_llm(self, user_prompt: str) -> str:
        """调用 DeepSeek chat completions API。"""
        url = f"{self._base_url}/chat/completions"
        body = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": "你是直播 AI 助手。只返回 JSON，不要加其他文字。"},
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

    def _rule_fallback(
        self,
        danmaku_summary: list[dict[str, Any]],
        inventory_alerts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """确定性规则降级（复用 Phase 5C 的决策逻辑）。"""
        has_high_frequency = any(d.get("count", 0) >= 10 for d in danmaku_summary)
        has_alerts = len(inventory_alerts) > 0

        if has_alerts:
            return {
                "route": "direct_plan",
                "goal": "处理库存告警",
                "suggestion": f"检测到 {len(inventory_alerts)} 个库存异常，建议检查备选商品并准备切换。",
            }
        elif has_high_frequency:
            top = max(danmaku_summary, key=lambda d: d.get("count", 0))
            return {
                "route": "direct_plan",
                "goal": "处理高频弹幕",
                "suggestion": f"弹幕高频问题：{top.get('summary', top.get('category', '未知'))}，建议主播重点回应。",
            }
        else:
            return {"route": "direct_plan", "goal": "低频事件", "suggestion": "建议主播关注观众反馈。"}
