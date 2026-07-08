# -*- coding: utf-8 -*-
"""Phase 4E PostLiveMemorySyncService 单元测试。"""
import pytest
from decimal import Decimal
from src.skills.post_live_memory_sync import PostLiveMemorySyncService
from src.memory.models import AnchorAction, BusinessResult, DecisionTraceRecord


class TestPostLiveMemorySync:
    def setup_method(self):
        from src.config.settings import get_settings
        self.settings = get_settings()

    def test_empty_trace_returns_zero(self):
        """空 trace 应返回 0 条写入，不报错。"""
        service = PostLiveMemorySyncService(self.settings)
        # sync with a non-existent trace_id
        result = service.sync_room_traces(
            anchor_id="anchor-demo-001",
            room_id="room-demo-001",
            trace_id="non-existent-trace-xxxxx",
        )
        assert result["memories_written"] == 0
        assert result["trust_updated"] is False

    def test_valid_trace_writes_memory(self):
        """合法 trace 应写入 L2 记忆并返回 >0 条。"""
        service = PostLiveMemorySyncService(self.settings)
        result = service.sync_room_traces(
            anchor_id="anchor-demo-001",
            room_id="room-demo-001",
            trace_id="trace-phase3a-memory-demo",
        )
        assert result["memories_written"] >= 1
        assert result["errors"] == 0
