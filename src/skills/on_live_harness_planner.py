"""Phase 5G-B 播中 LangGraph Harness Planner。

这个模块不是普通的“给一句建议”planner，而是为 LangGraph Harness Agent Loop
提供结构化决策：
- call_tool：请求图进入工具执行分支。
- final_answer：本轮已经形成主播建议，可以结束。
- no_action：无事件或无需干预。
- fallback：LLM 不可用或输出不可信时的降级结果。

LLM 只负责生成受控 JSON；工具白名单、生命周期、风险等级由 Harness Hook
继续兜底，避免 prompt 约束失效时越权执行。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from src.skills.llm_client import LLMClient

from pydantic import BaseModel, Field, field_validator

from src.config.settings import Settings, get_settings
from src.core.agent_harness_context import AgentContextResult
from src.core.agent_decision import AgentObservation
from src.skill_runtime.policy_view import SkillPolicyView, get_default_skill_policy_view
from src.skills.on_live_llm_planner import OnLiveLLMPlanner
from src.state.models import LifecycleStage

# 模块级缓存：默认 ON_LIVE Skill 集合，避免每次 planner 调用重建策略视图。
_ON_LIVE_TOOL_NAMES: list[str] | None = None

HarnessAction = Literal["call_tool", "final_answer", "no_action", "fallback"]
HarnessRiskLevel = Literal["LOW", "MEDIUM", "HIGH"]


def _available_on_live_tools(policy_view: SkillPolicyView | None = None) -> list[str]:
    """从 SkillPolicyView 读取 ON_LIVE 能力名，作为 LLM 输出白名单。

    默认装配使用模块级缓存；显式注入的测试或独立装配直接读取自身冻结快照，
    防止一个 Planner 的策略污染另一个 Planner。
    """
    global _ON_LIVE_TOOL_NAMES
    if policy_view is None and _ON_LIVE_TOOL_NAMES is not None:
        return _ON_LIVE_TOOL_NAMES
    view = policy_view or get_default_skill_policy_view()
    available = [
        name
        for name in view.skill_ids()
        if view.is_available(name, LifecycleStage.ON_LIVE)
        if name
        in {
            "handle_sold_out_event",
            "recommend_backup_product",
            "generate_on_live_prompt",
            "aggregate_danmaku_questions",
            "generate_danmaku_reply",
            "on_live_context_collect",
        }
    ]
    if policy_view is None:
        _ON_LIVE_TOOL_NAMES = available
    return available


class OnLiveHarnessDecision(BaseModel):
    """播中 Harness Planner 的结构化决策。

    该模型是 LangGraph 条件路由的唯一输入来源，字段必须稳定、可序列化。
    """

    thought: str = Field(default="")
    action: HarnessAction
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    final_suggestion: str | None = None
    risk_level: HarnessRiskLevel = "LOW"
    fallback_reason: str | None = None

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str | None, info) -> str | None:
        """call_tool 必须使用 SkillPolicyView 中的标准能力名。"""
        action = info.data.get("action")
        if action != "call_tool":
            return value
        if not value:
            raise ValueError("tool_name is required when action is call_tool")
        if value not in _available_on_live_tools():
            raise ValueError(f"unknown on-live tool: {value}")
        return value


def build_harness_prompt(
    context: AgentContextResult,
    available_tools: list[str],
    observations: list[dict[str, Any] | AgentObservation],
) -> str:
    """构造播中 Harness Planner prompt。

    只注入压缩后的状态摘要和工具 observation 摘要，不把大 JSON 原样塞入上下文。
    """
    lines = [
        "你是直播间播中 Harness Agent 的决策节点。",
        "你只能在给定工具白名单内选择工具，不能直接执行高风险动作。",
        "",
        "当前上下文:",
        context.system_context,
        "",
        "可用工具:",
    ]
    for tool in available_tools:
        lines.append(f"- {tool}")
    lines.extend(
        [
            "",
            "风险约束:",
            "- LOW: 可自动执行，例如聚合弹幕、生成提示。",
            "- MEDIUM: 只能给建议或调用安全工具。",
            "- HIGH: 不自动执行，只能返回 pending/human approval 方向。",
            "",
            "最近观察:",
        ]
    )
    if observations:
        for obs in observations[-5:]:
            if isinstance(obs, AgentObservation):
                lines.append(f"- {obs.tool_name}: {obs.status} | {obs.summary}")
            else:
                lines.append(
                    "- "
                    + str(obs.get("tool_name", "unknown"))
                    + ": "
                    + str(obs.get("status", "unknown"))
                    + " | "
                    + str(obs.get("summary", ""))
                )
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "只返回 JSON，不要输出其他文字。JSON 格式:",
            "{",
            '  "thought": "本轮判断依据",',
            '  "action": "call_tool | final_answer | no_action | fallback",',
            '  "tool_name": "工具名或 null",',
            '  "arguments": {},',
            '  "final_suggestion": "给主播看的建议或 null",',
            '  "risk_level": "LOW | MEDIUM | HIGH",',
            '  "fallback_reason": "降级原因或 null"',
            "}",
        ]
    )
    return "\n".join(lines)


def parse_harness_decision(llm_output: str) -> OnLiveHarnessDecision:
    """解析 LLM 输出为 Harness decision。

    解析失败必须抛 ValueError，由调用方降级，不允许 fail-open。
    """
    text = llm_output.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM output does not contain JSON")
    try:
        raw = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    try:
        return OnLiveHarnessDecision.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"invalid harness decision: {exc}") from exc


class OnLiveHarnessPlanner:
    """播中 Harness Planner。

    负责把播中上下文转成 LangGraph 可路由的结构化决策。历史路由允许失败时
    降级到 Phase 5F；Phase 14 显式 Decision Support 会冻结为禁用该降级。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        api_key: str = "",
        *,
        policy_view: SkillPolicyView | None = None,
        fallback_enabled: bool = True,
    ) -> None:
        # Planner 持有启动冻结策略；prompt 白名单与后续 Hook 使用同一事实来源。
        self._policy_view = policy_view or get_default_skill_policy_view()
        if settings is None and not api_key:
            try:
                settings = get_settings()
            except Exception:
                settings = None
        if settings is not None:
            base_url = (settings.llm_api_base_url or "https://api.deepseek.com").rstrip("/")
            api_key_val = settings.llm_api_key or api_key
            model = settings.llm_model or "deepseek-v4-flash"
            max_tokens = settings.llm_max_tokens or 500
            temperature = settings.llm_temperature or 0.2
            timeout = settings.llm_timeout_seconds or 15
        else:
            base_url = "https://api.deepseek.com"
            api_key_val = api_key
            model = "deepseek-v4-flash"
            max_tokens = 500
            temperature = 0.2
            timeout = 15
        self._llm_client = LLMClient(
            api_key=api_key_val,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        self._fallback_planner = OnLiveLLMPlanner(settings=settings, api_key=api_key)
        # Phase 14 的显式 Decision Support 路由禁用旧规则 Planner；模型不可用必须
        # 由 Graph 记录 DEGRADED，而不是让调用者误以为获得了新路径的正常建议。
        self._fallback_enabled = fallback_enabled

    def plan_next_step(
        self,
        context: AgentContextResult,
        danmaku_summary: list[dict[str, Any]],
        inventory_alerts: list[dict[str, Any]],
        observations: list[dict[str, Any] | AgentObservation],
    ) -> OnLiveHarnessDecision:
        """生成下一步 Harness 决策。"""
        if not danmaku_summary and not inventory_alerts and not observations:
            return OnLiveHarnessDecision(
                thought="无播中事件，无需干预",
                action="no_action",
                final_suggestion=None,
                risk_level="LOW",
            )
        available_tools = _available_on_live_tools(self._policy_view)
        if self._llm_client.has_api_key:
            try:
                prompt = build_harness_prompt(context, available_tools, observations)
                decision = parse_harness_decision(self._call_llm(prompt))
                if (
                    decision.action == "call_tool"
                    and decision.tool_name not in available_tools
                ):
                    # Prompt 不是安全边界。即使模型输出通过通用 Pydantic 结构校验，
                    # 仍须按当前 Planner 的冻结快照二次校验，禁止调用已下线能力。
                    raise ValueError("LLM selected a skill outside the frozen policy view")
                return decision
            except Exception as exc:
                return self._fallback_decision(danmaku_summary, inventory_alerts, str(exc))
        return self._fallback_decision(danmaku_summary, inventory_alerts, "missing api key")

    def _call_llm(self, user_prompt: str) -> str:
        """通过 LLMClient 调用 DeepSeek API，带重试和异常分类。"""
        resp = self._llm_client.call(
            user_prompt=user_prompt,
            system_prompt="你是直播播中 Harness Agent。只返回 JSON。",
        )
        if resp.fallback_triggered:
            raise RuntimeError("LLM call fallback: no api key or all retries exhausted")
        return resp.content

    def _fallback_decision(
        self,
        danmaku_summary: list[dict[str, Any]],
        inventory_alerts: list[dict[str, Any]],
        reason: str,
    ) -> OnLiveHarnessDecision:
        """按启动冻结策略选择旧规则降级，或把失败显式上抛。"""

        if not self._fallback_enabled:
            raise RuntimeError(f"legacy planner fallback disabled: {reason}")
        fallback = self._fallback_planner.plan(
            danmaku_summary=danmaku_summary,
            inventory_alerts=inventory_alerts,
            trust_score=0.7,
        )
        suggestion = fallback.get("suggestion")
        if suggestion:
            return OnLiveHarnessDecision(
                thought="Harness planner 降级到 Phase 5F planner",
                action="final_answer",
                final_suggestion=suggestion,
                risk_level="LOW",
                fallback_reason=reason,
            )
        return OnLiveHarnessDecision(
            thought="Harness planner 降级后仍无建议",
            action="fallback",
            final_suggestion=None,
            risk_level="LOW",
            fallback_reason=reason,
        )
