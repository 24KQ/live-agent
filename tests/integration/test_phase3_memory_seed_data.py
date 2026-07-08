"""Phase 3A 记忆样例数据初始化集成测试。"""

from src.config.settings import get_settings
from src.memory.demo_memory_seed import initialize_phase3_schema, seed_phase3_memory_demo_data
from src.memory.memory_store import MemoryStore
from src.memory.models import MemoryLayer
from src.skills.demo_data_seed import DEMO_ANCHOR_ID, initialize_phase2_schema, seed_phase2_demo_data


def test_seed_phase3_memory_demo_data_creates_memories_and_trust_state() -> None:
    """Phase 3A seed 应写入 L1/L2/L3 记忆，并为样例主播创建默认 trust_score。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    seed_result = seed_phase3_memory_demo_data(settings)
    second_seed_result = seed_phase3_memory_demo_data(settings)

    store = MemoryStore(settings)
    l1_memories = store.list_memories(anchor_id=DEMO_ANCHOR_ID, layer=MemoryLayer.L1)
    l2_memories = store.list_memories(anchor_id=DEMO_ANCHOR_ID, layer=MemoryLayer.L2)
    l3_memories = store.list_memories(anchor_id=DEMO_ANCHOR_ID, layer=MemoryLayer.L3)
    trust_state = store.get_trust_state(DEMO_ANCHOR_ID)
    seeded_keys = {
        memory.memory_key
        for memory in store.list_memories(anchor_id=DEMO_ANCHOR_ID)
        if memory.memory_key and memory.memory_key.startswith("anchor-demo-001-")
    }

    assert seed_result.memory_count == 3
    assert second_seed_result.memory_count == 3
    assert len(l1_memories) == 1
    assert len(l2_memories) >= 1
    assert len(l3_memories) == 1
    assert seeded_keys == {
        "anchor-demo-001-l1-prefer-kitchen-profit",
        "anchor-demo-001-l2-history-kitchen-performance",
        "anchor-demo-001-l3-style-summary",
    }
    assert trust_state.trust_score == seed_result.trust_score
