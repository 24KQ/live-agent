"""Phase 5G Harness Agent 生命周期 Hook 单元测试。

测试 AgentLifecycleHooks：
- 未注册工具被拒绝。
- 生命周期不匹配工具被拒绝。
- 高风险工具不自动执行。
- 连续重复工具调用超过 3 次被阻断。
"""

from __future__ import annotations

import pytest

from src.core.agent_lifecycle_hooks import AgentLifecycleHooks, HookResult
from src.core.security_hooks import GateDecision
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.policy_view import SkillPolicyView, get_default_skill_policy_view


class TestPreToolCallHook:

    def test_accepts_startup_frozen_skill_policy_view(self):
        """Hook 使用调用方注入的治理快照，未知能力继续 fail-closed。"""

        hooks = AgentLifecycleHooks(policy_view=get_default_skill_policy_view())
        result = hooks.pre_tool_call("missing_skill", {}, 0, "ON_LIVE")
        assert result.allowed is False
        assert result.auto_execute is False

    def test_unknown_tool_is_rejected(self):
        """未注册工具应被拒绝。"""
        hooks = AgentLifecycleHooks()
        result = hooks.pre_tool_call("unknown_tool", {}, 0, "ON_LIVE")
        assert result.allowed is False
        assert "unknown" in result.reason.lower() or "未注册" in result.reason

    def test_unknown_lifecycle_is_rejected_instead_of_assuming_pre_live(self):
        """任意未知生命周期都必须 fail-closed，不能默认为播前。"""

        hooks = AgentLifecycleHooks()
        result = hooks.pre_tool_call("query_products", {}, 0, "NOT_A_STAGE")
        assert result.allowed is False
        assert result.auto_execute is False
        assert "lifecycle" in result.reason.lower()

    def test_block_gate_is_rejected_before_risk_and_repeat_checks(self):
        """低风险能力一旦被治理策略 BLOCK，Hook 也不得自动放行。"""

        policy_view = SkillPolicyView(
            [
                manifest.model_copy(update={"gate_decision": GateDecision.BLOCK})
                if manifest.skill_id == "aggregate_danmaku_questions"
                else manifest
                for manifest in get_default_skill_catalog()
            ]
        )
        hooks = AgentLifecycleHooks(policy_view=policy_view)

        result = hooks.pre_tool_call(
            "aggregate_danmaku_questions", {}, 0, "ON_LIVE"
        )

        assert result.allowed is False
        assert result.auto_execute is False
        assert "blocked" in result.reason.lower()

    def test_wrong_lifecycle_is_rejected(self):
        """生命周期不匹配的工具应被拒绝。"""
        hooks = AgentLifecycleHooks()
        result = hooks.pre_tool_call("setup_live_session", {}, 0, "ON_LIVE")
        assert result.allowed is False

    def test_high_risk_tool_not_auto_executed(self):
        """高风险工具不应自动执行，应返回 pending。"""
        hooks = AgentLifecycleHooks()
        result = hooks.pre_tool_call("handle_sold_out_event", {"product_id": "p001", "room_id": "r1", "trace_id": "t1"}, 0, "ON_LIVE")
        assert result.allowed is True
        assert result.auto_execute is False

    def test_low_risk_tool_can_auto_execute(self):
        """低风险工具可自动执行。"""
        hooks = AgentLifecycleHooks()
        result = hooks.pre_tool_call("aggregate_danmaku_questions", {}, 0, "ON_LIVE")
        assert result.allowed is True
        assert result.auto_execute is True

    def test_repeated_call_blocked_after_3(self):
        """连续重复调用相同工具超过 3 次应被阻断。"""
        hooks = AgentLifecycleHooks()

        for i in range(3):
            result = hooks.pre_tool_call("recommend_backup_product", {"sold_out_product_id": "p001", "room_id": "r1"}, i, "ON_LIVE")
            assert result.allowed is True
            assert hooks._repeated_call_count("recommend_backup_product") == i + 1

        result = hooks.pre_tool_call("recommend_backup_product", {"sold_out_product_id": "p001", "room_id": "r1"}, 3, "ON_LIVE")
        assert result.allowed is False, "4th same call should be blocked"
        assert "repeated" in result.reason.lower() or "重复" in result.reason

        result = hooks.pre_tool_call("recommend_backup_product", {"sold_out_product_id": "p001", "room_id": "r1"}, 4, "ON_LIVE")
        assert result.allowed is False
        assert "repeated" in result.reason.lower() or "重复" in result.reason

    def test_reset_repeated_call_after_max(self):
        """超过最大连续次数后应重置计数。"""
        hooks = AgentLifecycleHooks(max_repeated_calls=2)

        for i in range(2):
            result = hooks.pre_tool_call("generate_on_live_prompt", {}, i, "ON_LIVE")
            assert result.allowed is True

        result = hooks.pre_tool_call("generate_on_live_prompt", {}, 2, "ON_LIVE")
        assert result.allowed is False


class TestPostToolCallHook:

    def test_success_observation(self):
        """工具执行成功应生成 observation。"""
        hooks = AgentLifecycleHooks()
        result = hooks.post_tool_call(
            "recommend_backup",
            {"sold_out_product_id": "p001"},
            {"status": "success", "backup_product_id": "p002"}
        )
        assert result is not None
        assert "recommend_backup" in result.tool_name
        assert result.status == "success"

    def test_error_observation(self):
        """工具执行失败应生成 error observation。"""
        hooks = AgentLifecycleHooks()
        result = hooks.post_tool_call(
            "handle_sold_out_event",
            {},
            {"status": "error", "summary": "service not configured"}
        )
        assert result.tool_name == "handle_sold_out_event"
        assert result.status == "error"
