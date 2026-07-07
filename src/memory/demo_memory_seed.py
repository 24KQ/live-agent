"""Phase 3A 记忆与信任样例数据初始化。

样例数据只服务本地演示和集成测试，全部使用稳定的脱敏 ID。seed 可以重复执行，
每次都会把样例 trust_score 重置为默认 0.70，便于演示“反馈后 trust_score 如何变化”。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import psycopg

from src.config.settings import Settings
from src.memory.memory_store import MemoryStore
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource, TrustState
from src.skills.demo_data_seed import DEMO_ANCHOR_ID, DEMO_ROOM_ID


@dataclass(frozen=True)
class Phase3MemorySeedResult:
    """Phase 3A seed 执行结果摘要。"""

    memory_count: int
    trust_score: Decimal


def initialize_phase3_schema(settings: Settings) -> None:
    """执行 Phase 3A 记忆与信任表初始化 SQL。"""

    project_root = Path(__file__).resolve().parents[2]
    sql = (project_root / "docker" / "init_phase3_memory.sql").read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
        connection.commit()


def seed_phase3_memory_demo_data(settings: Settings) -> Phase3MemorySeedResult:
    """写入可重复执行的主播偏好、历史表现和长期总结记忆。"""

    store = MemoryStore(settings)
    entries = [
        AnchorMemoryEntry(
            memory_key="anchor-demo-001-l1-prefer-kitchen-profit",
            anchor_id=DEMO_ANCHOR_ID,
            room_id=DEMO_ROOM_ID,
            layer=MemoryLayer.L1,
            content="主播明确偏好优先讲厨房类高利润商品，适合放在开场后的重点转化位。",
            metadata={
                "preferred_category": "厨房",
                "preferred_tags": ["利润款"],
                "preferred_product_ids": ["p003"],
            },
            confidence=Decimal("0.92"),
            evidence_weight=Decimal("0.85"),
            source=MemorySource.USER_STATED,
        ),
        AnchorMemoryEntry(
            memory_key="anchor-demo-001-l2-history-kitchen-performance",
            anchor_id=DEMO_ANCHOR_ID,
            room_id=None,
            layer=MemoryLayer.L2,
            content="历史样例显示厨房类商品在该主播直播间转化稳定，讲解时长不宜过短。",
            metadata={
                "preferred_category": "厨房",
                "historical_lift": "0.12",
            },
            confidence=Decimal("0.80"),
            evidence_weight=Decimal("0.70"),
            source=MemorySource.SYSTEM_OBSERVED,
        ),
        AnchorMemoryEntry(
            memory_key="anchor-demo-001-l3-style-summary",
            anchor_id=DEMO_ANCHOR_ID,
            room_id=None,
            layer=MemoryLayer.L3,
            content="主播整体风格偏稳健，适合使用结构化理由解释排品顺序和风险提示。",
            metadata={
                "preferred_plan_style": "structured",
                "risk_preference": "conservative",
            },
            confidence=Decimal("0.75"),
            evidence_weight=Decimal("0.65"),
            source=MemorySource.OFFLINE_SUMMARY,
        ),
    ]
    memory_ids = [store.write_memory(entry) for entry in entries]
    trust_state = TrustState(anchor_id=DEMO_ANCHOR_ID, trust_score=Decimal("0.70"))
    store.upsert_trust_state(trust_state)
    return Phase3MemorySeedResult(memory_count=len(memory_ids), trust_score=trust_state.trust_score)
