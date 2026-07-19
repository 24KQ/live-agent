# -*- coding: utf-8 -*-
"""Phase 4E PostLiveMemorySyncService 单元测试。"""
import pytest
from decimal import Decimal
from uuid import uuid4

import psycopg

from src.memory.decision_trace_store import DecisionTraceStore
from src.skills.embedding_service import EmbeddingService
from src.skills.post_live_memory_sync import PostLiveMemorySyncService
from src.memory.models import AnchorAction, BusinessResult, DecisionTraceRecord


class TestPostLiveMemorySync:
    def setup_method(self):
        from src.config.settings import get_settings

        self.settings = get_settings()
        self._test_scope: str | None = None

    def teardown_method(self):
        """按外键依赖逆序清理本测试创建的最小 PostgreSQL 事实。"""

        if self._test_scope is None:
            return
        anchor_id = f"anchor-phase4e-unit-{self._test_scope}"
        room_id = f"room-phase4e-unit-{self._test_scope}"
        product_id = f"product-phase4e-unit-{self._test_scope}"
        with psycopg.connect(**self.settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                # 反馈记忆与 DecisionTrace 都引用主播/直播间事实，必须先删除它们。
                cursor.execute("DELETE FROM live_agent_anchor_memories WHERE anchor_id = %(anchor_id)s", {"anchor_id": anchor_id})
                cursor.execute("DELETE FROM live_agent_decision_trace WHERE anchor_id = %(anchor_id)s", {"anchor_id": anchor_id})
                cursor.execute("DELETE FROM live_agent_room_products WHERE room_id = %(room_id)s", {"room_id": room_id})
                cursor.execute("DELETE FROM live_agent_products WHERE product_id = %(product_id)s", {"product_id": product_id})
                cursor.execute("DELETE FROM live_agent_live_rooms WHERE room_id = %(room_id)s", {"room_id": room_id})
                cursor.execute("DELETE FROM live_agent_anchors WHERE anchor_id = %(anchor_id)s", {"anchor_id": anchor_id})
            connection.commit()

    def _create_trace_fixture(self) -> tuple[str, str, str]:
        """创建不调用外部模型的最小货盘与不可变 DecisionTrace 夹具。"""

        self._test_scope = uuid4().hex
        anchor_id = f"anchor-phase4e-unit-{self._test_scope}"
        room_id = f"room-phase4e-unit-{self._test_scope}"
        product_id = f"product-phase4e-unit-{self._test_scope}"
        trace_id = f"trace-phase4e-unit-{self._test_scope}"
        with psycopg.connect(**self.settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO live_agent_anchors(anchor_id, display_name, style_tags)
                    VALUES (%(anchor_id)s, 'Phase4E Test Anchor', '[]'::jsonb)
                    """,
                    {"anchor_id": anchor_id},
                )
                cursor.execute(
                    """
                    INSERT INTO live_agent_live_rooms(room_id, anchor_id, title, lifecycle, scheduled_at)
                    VALUES (%(room_id)s, %(anchor_id)s, 'Phase4E Test Room', 'REVIEW', NOW())
                    """,
                    {"room_id": room_id, "anchor_id": anchor_id},
                )
                cursor.execute(
                    """
                    INSERT INTO live_agent_products(
                        product_id, name, category, price, inventory,
                        conversion_rate, commission_rate, tags, selling_points, is_active
                    )
                    VALUES (
                        %(product_id)s, 'Phase4E Test Product', 'test', 1.00, 1,
                        0.1000, 0.1000, '["unit"]'::jsonb, '["deterministic"]'::jsonb, TRUE
                    )
                    """,
                    {"product_id": product_id},
                )
                cursor.execute(
                    """
                    INSERT INTO live_agent_room_products(room_id, product_id, display_order)
                    VALUES (%(room_id)s, %(product_id)s, 1)
                    """,
                    {"room_id": room_id, "product_id": product_id},
                )
            connection.commit()

        # 通过真实 Store 写入，保留生产不可变 Trace 校验，而不是直接伪造数据库行。
        DecisionTraceStore(self.settings).record_trace(
            DecisionTraceRecord(
                trace_id=trace_id,
                anchor_id=anchor_id,
                room_id=room_id,
                recommendation={"preferred_product_id": product_id},
                anchor_action=AnchorAction.ACCEPTED,
                business_result=BusinessResult.GOOD,
                lift=Decimal("0.12"),
                trust_delta=Decimal("0.05"),
                final_trust_score=Decimal("0.75"),
            )
        )
        return anchor_id, room_id, trace_id

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

    def test_valid_trace_writes_memory(self, monkeypatch):
        """合法 trace 应写入 L2 记忆并返回 >0 条。"""

        anchor_id, room_id, trace_id = self._create_trace_fixture()
        embedding_inputs: list[str] = []

        def _offline_embedding(_service, content: str) -> list[float]:
            """保留 MemoryStore 的嵌入分支覆盖，同时确保单元测试不访问外部服务。"""

            embedding_inputs.append(content)
            return []

        monkeypatch.setattr(EmbeddingService, "embed", _offline_embedding)
        service = PostLiveMemorySyncService(self.settings)
        result = service.sync_room_traces(
            anchor_id=anchor_id,
            room_id=room_id,
            trace_id=trace_id,
        )
        assert result["memories_written"] >= 1
        assert result["errors"] == 0
        assert embedding_inputs
