# -*- coding: utf-8 -*-
"""Phase 4E 记忆回写集成测试。"""
import uuid
import pytest
from decimal import Decimal
from src.config.settings import Settings
from src.memory.decision_trace_store import DecisionTraceStore
from src.memory.demo_memory_seed import initialize_phase3_schema
from src.memory.models import AnchorAction, BusinessResult, DecisionTraceRecord
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data
from src.skills.embedding_service import EmbeddingService
from src.skills.post_live_memory_sync import PostLiveMemorySyncService


@pytest.mark.integration
class TestPostLiveMemorySyncFlow:
    """使用自给自足的脱敏事实夹具验证播后记忆同步链路。"""

    @pytest.fixture
    def settings(self):
        return Settings()

    @pytest.fixture(autouse=True)
    def _disable_external_embedding(self, monkeypatch):
        """本测试验证本地 Store 链路，不能因外部 Embedding 凭证改变结果。"""

        monkeypatch.setattr(EmbeddingService, "embed", lambda _self, _text: [])

    @pytest.fixture(autouse=True)
    def _seed_required_trace(self, settings):
        """准备固定 Trace 及其货盘父事实，避免依赖开发库残留的 Demo 数据。"""

        initialize_phase2_schema(settings)
        seed_phase2_demo_data(settings)
        initialize_phase3_schema(settings)
        trace_store = DecisionTraceStore(settings)
        trace_id = "trace-phase3a-memory-demo"
        if not trace_store.list_traces(trace_id):
            # 真实 Store 保持 Trace 不可变；仅在干净库中创建一次确定性基线。
            trace_store.record_trace(
                DecisionTraceRecord(
                    trace_id=trace_id,
                    anchor_id="anchor-demo-001",
                    room_id="room-demo-001",
                    recommendation={"preferred_product_id": "p003"},
                    anchor_action=AnchorAction.ACCEPTED,
                    business_result=BusinessResult.GOOD,
                    lift=Decimal("0.12"),
                    trust_delta=Decimal("0.05"),
                    final_trust_score=Decimal("0.75"),
                )
            )

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
