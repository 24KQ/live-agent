"""Phase 3B 记忆检索与冲突修正样例数据。

该 seed 使用独立的脱敏主播和直播间，避免影响 Phase 3A 对默认样例主播的断言。
每次执行都会清理上一轮 Phase 3B 反馈记忆，并把旧偏好重置为 active，保证演示可重复。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from src.config.settings import Settings
from src.memory.memory_store import MemoryStore
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource, MemoryStatus
from src.skills.demo_data_seed import DEMO_PRODUCTS


PHASE3B_ANCHOR_ID = "anchor-phase3b-001"
PHASE3B_ROOM_ID = "room-phase3b-001"


@dataclass(frozen=True)
class Phase3BSeedResult:
    """Phase 3B seed 执行结果摘要。"""

    anchor_id: str
    room_id: str
    reset_memory_key: str
    product_count: int


def initialize_phase3b_demo_data(settings: Settings) -> Phase3BSeedResult:
    """初始化 Phase 3B 独立主播、直播间和旧偏好记忆。"""

    _seed_anchor_room_and_products(settings)
    _clear_previous_phase3b_feedback(settings)
    memory_key = _seed_old_preference(settings)
    return Phase3BSeedResult(
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
        reset_memory_key=memory_key,
        product_count=len(DEMO_PRODUCTS),
    )


def _seed_anchor_room_and_products(settings: Settings) -> None:
    """写入独立主播和直播间，并复用 Phase 2A 的脱敏商品货盘。"""

    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO live_agent_anchors(anchor_id, display_name, style_tags)
                VALUES (%(anchor_id)s, %(display_name)s, %(style_tags)s)
                ON CONFLICT (anchor_id)
                DO UPDATE SET display_name = EXCLUDED.display_name, style_tags = EXCLUDED.style_tags;
                """,
                {
                    "anchor_id": PHASE3B_ANCHOR_ID,
                    "display_name": "Phase 3B Demo 主播",
                    "style_tags": Jsonb(["记忆修正", "偏好复盘", "结构化决策"]),
                },
            )
            cursor.execute(
                """
                INSERT INTO live_agent_live_rooms(room_id, anchor_id, title, lifecycle, scheduled_at)
                VALUES (%(room_id)s, %(anchor_id)s, %(title)s, 'PRE_LIVE', %(scheduled_at)s)
                ON CONFLICT (room_id)
                DO UPDATE SET
                    anchor_id = EXCLUDED.anchor_id,
                    title = EXCLUDED.title,
                    lifecycle = EXCLUDED.lifecycle,
                    scheduled_at = EXCLUDED.scheduled_at;
                """,
                {
                    "room_id": PHASE3B_ROOM_ID,
                    "anchor_id": PHASE3B_ANCHOR_ID,
                    "title": "LiveAgent Phase 3B 记忆修正样例场",
                    "scheduled_at": datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc),
                },
            )
            for index, product in enumerate(DEMO_PRODUCTS, start=1):
                product_id = product[0]
                cursor.execute(
                    """
                    INSERT INTO live_agent_room_products(room_id, product_id, display_order)
                    VALUES (%(room_id)s, %(product_id)s, %(display_order)s)
                    ON CONFLICT (room_id, product_id)
                    DO UPDATE SET display_order = EXCLUDED.display_order;
                    """,
                    {"room_id": PHASE3B_ROOM_ID, "product_id": product_id, "display_order": index},
                )
        connection.commit()


def _clear_previous_phase3b_feedback(settings: Settings) -> None:
    """清理上一轮演示生成的反馈记忆，避免重复运行影响修正前结果。"""

    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM live_agent_anchor_memories
                WHERE anchor_id = %(anchor_id)s
                  AND memory_key LIKE %(memory_key_prefix)s;
                """,
                {"anchor_id": PHASE3B_ANCHOR_ID, "memory_key_prefix": "phase3b-feedback-%"},
            )
        connection.commit()


def _seed_old_preference(settings: Settings) -> str:
    """写入旧的家居类偏好，作为冲突修正前的基线记忆。"""

    memory_key = "phase3b-old-home-preference"
    store = MemoryStore(settings)
    store.write_memory(
        AnchorMemoryEntry(
            memory_key=memory_key,
            anchor_id=PHASE3B_ANCHOR_ID,
            room_id=PHASE3B_ROOM_ID,
            layer=MemoryLayer.L1,
            content="主播历史上明确偏好把家居类商品放在开场主推位。",
            metadata={
                "conflict_group": "primary_category_strategy",
                "preferred_category": "家居",
                "preferred_product_ids": ["p001"],
                "preferred_tags": ["引流款"],
            },
            confidence=Decimal("0.96"),
            evidence_weight=Decimal("0.92"),
            source=MemorySource.USER_STATED,
            status=MemoryStatus.ACTIVE,
            suppressed_reason=None,
        )
    )
    return memory_key
