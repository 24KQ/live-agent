"""Phase 4A 播后复盘完整闭环集成测试。

验证 POST_LIVE 锁定、归因、复盘、信任更新的端到端链路。
"""

import uuid
import pytest

from src.config.settings import get_settings
from src.core.post_live_lock import post_live_tool_mask
from src.memory.trust_manager import TrustManager
from src.memory.decision_trace_store import DecisionTraceStore
from src.memory.memory_store import MemoryStore
from src.skills.post_live_attribution import PostLiveAttribution
from src.skills.post_live_review import PostLiveReview
from src.state.models import LifecycleStage

pytestmark = pytest.mark.integration


class TestPostLiveFlow:

    def test_post_live_lock_blocks_all_write_tools(self):
        """POST_LIVE 下所有写操作必须被 block。"""
        blocked = {"set_product_price", "switch_product", "setup_live_session", "generate_live_plan"}
        for tool in blocked:
            result = post_live_tool_mask(tool, LifecycleStage.POST_LIVE, trust_score=0.5)
            assert result == "block"

    def test_full_post_live_review_loop(self):
        """完整播后复盘闭环。"""
        settings = get_settings()

        # 模拟几条决策记录
        traces = [
            {"anchor_action": "accepted", "business_result": "good", "trust_delta": 0.05},
            {"anchor_action": "accepted", "business_result": "bad", "trust_delta": -0.10},
            {"anchor_action": "rejected", "business_result": "agent_right", "trust_delta": 0.03},
        ]
        # 归因
        attr = PostLiveAttribution.calculate(traces)
        assert attr.total_decisions == 3
        assert attr.adoption_rate > 0

        # 复盘
        report = PostLiveReview.review(traces)
        assert report["total_decisions"] == 3
        assert len(report["issues"]) >= 1  # 至少一个采纳但效果差的问题

        print(f"\n归因: adoption={attr.adoption_rate}, accuracy={attr.accuracy_rate}")
        print(f"复盘: issues={report['issues']}")
