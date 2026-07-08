"""Phase 4A 播后锁定单元测试。

验证 POST_LIVE 生命周期下所有写操作工具被 post_live_tool_mask 强制 block。
"""

import pytest
from src.state.models import LifecycleStage
from src.core.post_live_lock import post_live_tool_mask, is_post_live_blocked


class TestPostLiveLock:

    def test_write_actions_are_blocked_in_post_live(self):
        blocked_tools = {"set_product_price", "switch_product", "setup_live_session", "generate_live_plan"}
        for tool in blocked_tools:
            result = post_live_tool_mask(tool, LifecycleStage.POST_LIVE, trust_score=0.3)
            assert result == "block", f"POST_LIVE should block {tool}, got {result}"

    def test_write_actions_not_blocked_in_pre_live(self):
        result = post_live_tool_mask("set_product_price", LifecycleStage.PRE_LIVE, trust_score=0.3)
        assert result == "visible"

    def test_readonly_actions_remain_visible_in_post_live(self):
        result = post_live_tool_mask("query_products", LifecycleStage.POST_LIVE, trust_score=0.5)
        assert result == "visible"

    def test_post_live_block_is_mandatory_regardless_of_trust(self):
        result = post_live_tool_mask("set_product_price", LifecycleStage.POST_LIVE, trust_score=0.9)
        assert result == "block"
