"""Phase 5A LLM Agent Planner.

封装 DeepSeek（deepseek-v4-flash）chat completions API，
为播前编排生成结构化决策。失败时返回 fallback 路由。

不引入 langchain 或 openai 库，用标准库 urllib 直接调用。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from src.core.agent_decision import AgentPlannerDecision, AgentReplanRoute, AgentToolCall
from src.skills.product_catalog import CatalogProduct


def build_planner_prompt(
    room_id: str,
    products: list[CatalogProduct],
    memory_hits: list[tuple[str, float]] | None,
    trust_score: float,
    available_tools: list[str],
) -> str:
    NL = chr(10)
    lines = [f"当前直播间: {room_id}"]
    lines.append(f"信任分: {trust_score}")
    lines.append("")
    lines.append("货盘商品列表:")
    for p in products:
        lines.append(f"  - {p.product_id}: {p.name} ({p.category}, {p.price}元)")
    if memory_hits:
        lines.append("")
        lines.append("主播历史记忆:")
        for mem_text, score in memory_hits:
            lines.append(f"  - [{score}] {mem_text}")
    lines.append("")
    lines.append("可用工具:")
    for t in available_tools:
        lines.append(f"  - {t}")
    lines.append("")
    lines.append("请返回以下 JSON 格式:")
    lines.append(chr(123))
    lines.append('  "route": "memory_first | direct_plan | cards_first | risk_check | fallback | finish",')
    lines.append('  "goal": "本次播前目标",')
    lines.append('  "reason": "决策理由",')
    lines.append('  "tool_calls": [')
    lines.append('    { "tool_name": "工具名", "arguments": {}, "risk_level": "LOW|MEDIUM|HIGH"|"LOW|MEDIUM|HIGH"|"LOW|MEDIUM|HIGH" }')
    lines.append('  ]')
    lines.append(chr(125))
    lines.append("")
    lines.append("路由说明:")
    lines.append("- memory_first: 先检索主播记忆再排品")
    lines.append("- direct_plan: 直接生成排品方案")
    lines.append("- cards_first: 先生成商品手卡")
    lines.append("- risk_check: 先做合规/风险检查")
    lines.append("- fallback: 走确定性规则链路")
    lines.append("- finish: 所有工具执行完，进入建播")
    return NL.join(lines)


class AgentPlanner:

    def __init__(self, settings=None, api_key=""):
        if settings:
            b = settings.llm_api_base_url or "https://api.deepseek.com"
            self._base_url = b.rstrip("/")
            self._api_key = settings.llm_api_key or api_key
            self._model = settings.llm_model or "deepseek-v4-flash"
            self._max_tokens = settings.llm_max_tokens or 300
            self._temperature = settings.llm_temperature or 0.1
            self._timeout = settings.llm_timeout_seconds or 15
        else:
            self._base_url = "https://api.deepseek.com"
            self._api_key = api_key
            self._model = "deepseek-v4-flash"
            self._max_tokens = 300
            self._temperature = 0.1
            self._timeout = 15

    def plan(self, room_id, trace_id, products, memory_hits=None, trust_score=0.7, available_tools=None):
        try:
            prompt = build_planner_prompt(
                room_id=room_id, products=products,
                memory_hits=memory_hits or [],
                trust_score=trust_score,
                available_tools=available_tools or [],
            )
            sp = "你是一个直播带货 AI 编排助手。请根据上下文选择最优路由，只返回 JSON。"
            llm_out = self._call_llm(sp, prompt)
            return self._parse_decision(llm_out, trace_id, room_id)
        except Exception as exc:
            return self._fallback_decision(trace_id, room_id, str(exc))

    def _call_llm(self, system_prompt, user_prompt):
        url = self._base_url + "/chat/completions"
        body = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self._api_key,
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("LLM API call failed: " + str(exc)) from exc
        return data["choices"][0]["message"]["content"]

    def _parse_decision(self, llm_output, trace_id, room_id):
        text = llm_output.strip().replace("```", "").replace("json", "", 1)
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("LLM no JSON: " + text[:200])
        try:
            raw = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("bad JSON: " + str(exc)) from exc
        raw["trace_id"] = trace_id
        raw["room_id"] = room_id
        if "tool_calls" in raw and isinstance(raw["tool_calls"], list):
            raw["tool_calls"] = [AgentToolCall.model_validate(tc) for tc in raw["tool_calls"]]
        else:
            raw["tool_calls"] = []
        return AgentPlannerDecision.model_validate(raw)

    def _fallback_decision(self, trace_id, room_id, reason):
        return AgentPlannerDecision(
            trace_id=trace_id,
            room_id=room_id,
            goal="fallback to deterministic rules",
            route=AgentReplanRoute.FALLBACK,
            reason="LLM planner unavailable, fallback: " + reason,
            tool_calls=[],
            requires_human_approval=False,
            fallback_reason=reason,
        )
