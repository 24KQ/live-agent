"""运行 Phase 3B 记忆检索、衰减与冲突修正闭环演示。"""

from decimal import Decimal
from pathlib import Path
import sys
from uuid import uuid4


# 直接执行脚本时补充项目根目录，保证 Windows / PowerShell 下导入路径稳定。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings
from src.memory.belief_revision import BeliefRevisionService
from src.memory.decision_memory_feedback import DecisionTraceMemoryFeedbackService
from src.memory.demo_memory_seed import initialize_phase3_schema
from src.memory.demo_memory_seed_phase3b import (
    PHASE3B_ANCHOR_ID,
    PHASE3B_ROOM_ID,
    initialize_phase3b_demo_data,
)
from src.memory.memory_aware_plan import MemoryAwarePlanService
from src.memory.memory_retrieval import MemoryRetriever
from src.memory.memory_store import MemoryStore
from src.memory.models import AnchorAction, BusinessResult
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data
from src.skills.product_catalog import ProductCatalogRepository


def main() -> int:
    """演示旧偏好被新反馈修正后，下一轮播前排品如何变化。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    initialize_phase3b_demo_data(settings)

    trace_id = f"trace-phase3b-memory-revision-{uuid4().hex[:8]}"
    memory_store = MemoryStore(settings)
    plan_service = MemoryAwarePlanService(memory_store)
    products = ProductCatalogRepository(settings).list_room_products(PHASE3B_ROOM_ID)

    before_plan = plan_service.generate_plan(
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
        products=products,
        trace_id=f"{trace_id}-before",
    )
    feedback_memory = DecisionTraceMemoryFeedbackService(memory_store).build_feedback_memory(
        trace_id=trace_id,
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
        anchor_action=AnchorAction.ACCEPTED,
        business_result=BusinessResult.GOOD,
        recommendation={
            "preferred_category": "厨房",
            "preferred_product_ids": ["p003"],
            "preferred_tags": ["利润款"],
            "conflict_group": "primary_category_strategy",
        },
        lift=Decimal("0.18"),
        catalog_products=products,
    )
    revision = BeliefRevisionService(memory_store).revise_preference(
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
        new_memory=feedback_memory,
        reason="CLI 演示：主播复盘后确认厨房利润款策略表现更好。",
    )
    after_plan = plan_service.generate_plan(
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
        products=products,
        trace_id=f"{trace_id}-after",
    )
    hits = MemoryRetriever(memory_store).retrieve(anchor_id=PHASE3B_ANCHOR_ID, room_id=PHASE3B_ROOM_ID)

    print("Phase 3B memory revision demo")
    print(f"anchor_id: {PHASE3B_ANCHOR_ID}")
    print(f"room_id: {PHASE3B_ROOM_ID}")
    print(f"trace_id: {trace_id}")
    print("before_revision_top3:")
    for item in before_plan.items[:3]:
        print(f"  {item.rank}. {item.product_id} {item.product_name} - {item.reason}")
    print(f"suppressed_memory_keys: {', '.join(revision.suppressed_memory_keys)}")
    print(f"new_memory_key: {revision.new_memory_key}")
    print("after_revision_top3:")
    for item in after_plan.items[:3]:
        print(f"  {item.rank}. {item.product_id} {item.product_name} - {item.reason}")
    print("top_memory_hits:")
    for hit in hits[:3]:
        print(
            f"  {hit.memory.memory_key} "
            f"status={hit.memory.status.value} "
            f"effective_weight={hit.effective_weight} "
            f"score={hit.relevance_score}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
