"""运行 Phase 3A 记忆与信任层闭环演示。"""

from decimal import Decimal
from pathlib import Path
import sys
from uuid import uuid4


# 直接执行脚本时补充项目根目录，保证 Windows / PowerShell 下导入路径稳定。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings
from src.memory.decision_trace_store import DecisionTraceStore
from src.memory.demo_memory_seed import initialize_phase3_schema, seed_phase3_memory_demo_data
from src.memory.memory_aware_plan import MemoryAwarePlanService
from src.memory.memory_store import MemoryStore
from src.memory.models import AnchorAction, BusinessResult, DecisionTraceRecord
from src.memory.tool_mask_policy import ToolMaskPolicy
from src.memory.trust_manager import TrustManager
from src.config.tool_registry import get_default_tool_registry
from src.skills.demo_data_seed import (
    DEMO_ANCHOR_ID,
    DEMO_ROOM_ID,
    initialize_phase2_schema,
    seed_phase2_demo_data,
)
from src.skills.product_catalog import ProductCatalogRepository
from src.state.models import LifecycleStage


def main() -> int:
    """演示“记忆影响排品 -> 主播反馈 -> trust 更新 -> 下一轮建议”的最小闭环。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    seed_phase3_memory_demo_data(settings)

    # Decision Trace 采用不可覆盖策略；CLI 每次演示生成新的脱敏 trace_id，
    # 避免重复运行时尝试改写上一轮演示记录。
    trace_id = f"trace-phase3a-memory-demo-{uuid4().hex[:8]}"
    memory_store = MemoryStore(settings)
    trace_store = DecisionTraceStore(settings)
    plan_service = MemoryAwarePlanService(memory_store)
    products = ProductCatalogRepository(settings).list_room_products(DEMO_ROOM_ID)
    original_trust = memory_store.get_trust_state(DEMO_ANCHOR_ID)

    first_plan = plan_service.generate_plan(
        anchor_id=DEMO_ANCHOR_ID,
        room_id=DEMO_ROOM_ID,
        products=products,
        trace_id=trace_id,
    )
    update = TrustManager().apply_feedback(
        original_trust,
        AnchorAction.ACCEPTED,
        BusinessResult.GOOD,
    )
    memory_store.upsert_trust_state(update.new_state)
    decision_id = trace_store.record_trace(
        DecisionTraceRecord(
            trace_id=trace_id,
            anchor_id=DEMO_ANCHOR_ID,
            room_id=DEMO_ROOM_ID,
            recommendation={
                "first_product_id": first_plan.items[0].product_id,
                "first_product_reason": first_plan.items[0].reason,
            },
            anchor_action=AnchorAction.ACCEPTED,
            business_result=BusinessResult.GOOD,
            lift=Decimal("0.12"),
            trust_delta=update.trust_delta,
            final_trust_score=update.new_state.trust_score,
        )
    )
    second_plan = plan_service.generate_plan(
        anchor_id=DEMO_ANCHOR_ID,
        room_id=DEMO_ROOM_ID,
        products=products,
        trace_id=f"{trace_id}-second",
    )
    visible_tools = ToolMaskPolicy(get_default_tool_registry()).visible_tools(
        update.new_state.trust_score,
        LifecycleStage.PRE_LIVE,
    )

    print("Phase 3A memory and trust demo")
    print(f"anchor_id: {DEMO_ANCHOR_ID}")
    print(f"room_id: {DEMO_ROOM_ID}")
    print(f"trace_id: {trace_id}")
    print(f"original_trust_score: {original_trust.trust_score}")
    print("first_plan_top3:")
    for item in first_plan.items[:3]:
        print(f"  {item.rank}. {item.product_id} {item.product_name} - {item.reason}")
    print(f"feedback: {AnchorAction.ACCEPTED.value}/{BusinessResult.GOOD.value}")
    print(f"trust_delta: {update.trust_delta}")
    print(f"new_trust_score: {update.new_state.trust_score}")
    print(f"decision_trace_id: {decision_id}")
    print("second_plan_top3:")
    for item in second_plan.items[:3]:
        print(f"  {item.rank}. {item.product_id} {item.product_name} - {item.reason}")
    print(f"visible_pre_live_tools: {', '.join(visible_tools)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
