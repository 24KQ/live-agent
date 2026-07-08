# -*- coding: utf-8 -*-
"""Phase 4E 记忆回写集成测试。"""
import uuid
import pytest
from decimal import Decimal
from src.config.settings import Settings
from src.skills.post_live_memory_sync import PostLiveMemorySyncService


@pytest.mark.integration
class TestPostLiveMemorySyncFlow:
    @pytest.fixture
    def settings(self):
        return Settings()

    def test_sync_with_real_db_records(self, settings):
        """使用数据库中已有的 DecisionTrace 记录验证端到端同步。"""
        service = PostLiveMemorySyncService(settings)
        result = service.sync_room_traces(
            anchor_id="anchor-demo-001",
            room_id="room-demo-001",
            trace_id="trace-phase3a-memory-demo",
        )

        # 验证记忆已写入
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(**settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) as cnt FROM live_agent_anchor_memories WHERE memory_key LIKE %s;",
                    ("%phase3a-memory-demo%",),
                )
                row = cur.fetchone()

        assert result["memories_written"] >= 1
        assert result["errors"] == 0
        assert row["cnt"] >= 1
