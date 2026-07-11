"""Phase 5G Harness Agent 生命周期钩子模块。

在 Agent 推理和执行的关键时机插入强规则拦截：
- pre_tool_call: 校验工具是否注册、生命周期匹配、风险等级、重复调用阻断。
- post_tool_call: 把工具执行结果转成结构化 observation。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.config.tool_registry import get_default_tool_registry, ToolNotFoundError
from src.state.models import LifecycleStage, RiskLevel
from src.core.agent_decision import AgentObservation


@dataclass
class HookResult:
    """生命周期钩子返回结果。"""
    allowed: bool = True
    auto_execute: bool = True
    reason: str = ""


_TOOL_RISK_MAP: dict[str, str] = {}


def _init_risk_map():
    """从 ToolRegistry 加载工具风险等级映射。"""
    global _TOOL_RISK_MAP
    if _TOOL_RISK_MAP:
        return
    try:
        registry = get_default_tool_registry()
        for name in registry.tool_names():
            try:
                meta = registry.get(name)
                _TOOL_RISK_MAP[name] = meta.risk_level.value if hasattr(meta.risk_level, "value") else str(meta.risk_level)
            except (ToolNotFoundError, Exception):
                pass
    except Exception:
        pass


class AgentLifecycleHooks:
    """生命周期钩子集。"""

    def __init__(self, max_repeated_calls: int = 3):
        self._max_repeated = max_repeated_calls
        self._call_history: list[tuple[str, str]] = []
        _init_risk_map()

    def _get_risk_level(self, tool_name: str) -> str:
        """获取工具风险等级，未知工具返回 HIGH。"""
        return _TOOL_RISK_MAP.get(tool_name, "HIGH")

    def _repeated_call_count(self, tool_name: str) -> int:
        """检查最近连续调用同一工具的次数。"""
        count = 0
        for t, _ in reversed(self._call_history):
            if t == tool_name:
                count += 1
            else:
                break
        return count

    def _reset_if_blocked(self):
        """如果历史记录超过 max_repeated，重置计数避免永久阻塞。"""
        while len(self._call_history) > self._max_repeated * 2:
            self._call_history.pop(0)

    def pre_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        iteration: int,
        lifecycle: str,
    ) -> HookResult:
        """工具调用前校验。"""

        registry = get_default_tool_registry()

        try:
            meta = registry.get(tool_name)
        except ToolNotFoundError:
            return HookResult(allowed=False, auto_execute=False, reason="tool not registered: " + tool_name)

        lifecycle_enum = LifecycleStage.ON_LIVE if lifecycle == "ON_LIVE" else LifecycleStage.PRE_LIVE
        if lifecycle_enum not in meta.lifecycle:
            return HookResult(allowed=False, auto_execute=False, reason="lifecycle mismatch: " + str(meta.lifecycle))

        risk = self._get_risk_level(tool_name)

        if risk == "HIGH":
            self._call_history.append((tool_name, "blocked_high_risk"))
            return HookResult(allowed=True, auto_execute=False, reason="high risk tool: pending human approval")

        repeated = self._repeated_call_count(tool_name)
        if repeated >= self._max_repeated:
            self._call_history.append((tool_name, "blocked_repeated"))
            self._reset_if_blocked()
            return HookResult(allowed=False, auto_execute=False, reason="repeated call blocked after " + str(self._max_repeated) + " times")

        self._call_history.append((tool_name, "executed"))
        return HookResult(allowed=True, auto_execute=True, reason="")

    def post_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> AgentObservation:
        """工具执行后生成 observation。"""
        status = result.get("status", "error")
        summary = result.get("summary", "")
        audit_id = result.get("audit_id")
        return AgentObservation(
            tool_name=tool_name,
            status=status,
            summary=summary,
            audit_id=audit_id,
        )

    def post_reasoning(
        self,
        tool_name: str | None,
        arguments: dict[str, Any],
        current_product: dict[str, Any] | None,
        inventory_alerts: list[dict[str, Any]],
    ) -> PostReasoningResult:
        """对 LLM 决策结果做交叉验证，检测三种幻觉。

        1. 商品不存在幻觉：call_tool 携带的 product_id 不在当前货盘中
        2. 无事件调用工具幻觉：无库存告警时调用了售罄处理工具
        3. 商品已售罄但未处理：有库存告警但 LLM 未响应

        返回 PostReasoningResult，发现幻觉时 corrected_decision 不为 None。
        """
        issues: list[str] = []

        # 检查 1：商品 ID 是否存在
        product_id = arguments.get("product_id") or arguments.get("sold_out_product_id")
        if product_id and current_product:
            pid = current_product.get("product_id", "")
            if pid and product_id != pid:
                issues.append(f"商品 {product_id} 不在当前讲解商品中（当前商品: {pid}）")

        # 检查 2：无事件调用工具
        if tool_name == "handle_sold_out_event" and not inventory_alerts:
            issues.append("无库存告警，不应调用售罄处理工具 handle_sold_out_event")

        # 检查 3：有库存告警但 LLM 未处理（不强制阻断，仅记录）
        if inventory_alerts and tool_name not in ("handle_sold_out_event", "recommend_backup_product"):
            issues.append(f"存在 {len(inventory_alerts)} 个库存告警，但 LLM 决策未涉及售罄处理或备选推荐")

        if issues:
            return PostReasoningResult(
                passed=False,
                issues=issues,
                corrected_decision={"action": "corrected", "reason": "; ".join(issues)},
            )
        return PostReasoningResult(passed=True, issues=[], corrected_decision=None)


@dataclass
class PostReasoningResult:
    """PostReasoning 幻觉检测结果。"""
    passed: bool = True
    issues: list[str] = field(default_factory=list)
    corrected_decision: dict | None = None
