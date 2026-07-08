# -*- coding: utf-8 -*-
"""Phase 4E 播后记忆同步编排层。

从 PostgreSQL 读取 DecisionTrace 记录，通过 DecisionTraceMemoryFeedbackService
生成 L2 反馈记忆，写入 MemoryStore 并更新 TrustManager。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.config.settings import Settings, get_settings
from src.memory.decision_memory_feedback import DecisionTraceMemoryFeedbackService
from src.memory.decision_trace_store import DecisionTraceStore
from src.memory.memory_store import MemoryStore
from src.memory.trust_manager import TrustManager
from src.memory.models import AnchorAction, BusinessResult
from src.skills.product_catalog import ProductCatalogRepository, CatalogProduct


class PostLiveMemorySyncService:
    """播后记忆同步服务：读取决策记录 -> 生成记忆 -> 写入 MemoryStore -> 更新信任分。"""

    def __init__(self, settings: Settings | None = None) -> None:
        if settings is None:
            settings = get_settings()
        self._settings = settings
        self._memory_store = MemoryStore(settings)
        self._decision_store = DecisionTraceStore(settings)
        self._feedback_service = DecisionTraceMemoryFeedbackService(self._memory_store)
        self._trust_manager = TrustManager()
        self._product_repo = ProductCatalogRepository(settings)

    def sync_room_traces(
        self,
        anchor_id: str,
        room_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """从 DB 读取指定 trace 的决策记录，同步到记忆层。

        Args:
            anchor_id: 主播 ID
            room_id: 直播间 ID
            trace_id: 决策记录 trace_id

        Returns:
            dict: 同步结果摘要
                - memories_written: 写入的记忆数
                - trust_updated: 是否更新了信任分
                - trust_before: 更新前信任分
                - trust_after: 更新后信任分
                - errors: 处理失败的记录数
        """
        # 1. 读取决策记录
        traces = self._decision_store.list_traces(trace_id)
        if not traces:
            return {"memories_written": 0, "trust_updated": False,
                    "trust_before": None, "trust_after": None, "errors": 0}

        # 2. 获取货盘（用于记忆白名单过滤）
        catalog_products = self._product_repo.list_room_products(room_id)

        # 3. 获取当前信任分
        trust_before = self._memory_store.get_trust_state(anchor_id)
        trust_before_score = trust_before.trust_score if trust_before else Decimal("0.70")

        # 4. 逐条处理
        memories_written = 0
        errors = 0
        for trace in traces:
            try:
                memory = self._feedback_service.build_feedback_memory(
                    trace_id=trace.trace_id,
                    anchor_id=anchor_id,
                    room_id=room_id,
                    anchor_action=trace.anchor_action,
                    business_result=trace.business_result,
                    recommendation=trace.recommendation,
                    lift=trace.lift,
                    catalog_products=catalog_products,
                )
                self._feedback_service.write_feedback_memory(memory)
                memories_written += 1

            except Exception:
                errors += 1

        # 5. ??????????? TrustManager
        trust_after_score = trust_before_score
        if traces:
            for t in traces:
                state = self._memory_store.get_trust_state(anchor_id)
                if state is not None:
                    self._trust_manager.apply_feedback(
                        state,
                        anchor_action=t.anchor_action,
                        business_result=t.business_result,
                    )
            trust_after = self._memory_store.get_trust_state(anchor_id)
            trust_after_score = trust_after.trust_score if trust_after else trust_before_score

        return {
            "memories_written": memories_written,
            "trust_updated": len(traces) > 0,
            "trust_before": float(trust_before_score),
            "trust_after": float(trust_after_score),
            "errors": errors,
        }
