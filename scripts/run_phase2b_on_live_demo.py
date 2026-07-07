"""运行 Phase 2B 播中售罄事件闭环演示。

运行方式：
    python scripts/run_phase2b_on_live_demo.py

脚本只使用本地脱敏样例数据和确定性事件，不接 LLM、不接真实淘宝 API、
不启动长期 Kafka consumer。
"""

from __future__ import annotations

from pathlib import Path
import sys


# 直接执行脚本时，Python 默认只把 scripts 目录加入 sys.path。这里显式加入
# 仓库根目录，保证可以稳定导入 src 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.on_live_flow import OnLiveFlowService
from src.skills.demo_data_seed import DEMO_ROOM_ID, initialize_phase2_schema, seed_phase2_demo_data
from src.skills.on_live_events import InventoryEvent, OnLiveEventType
from src.skills.product_catalog import CatalogProduct, ProductCatalogRepository
from src.state.models import LifecycleStage, LiveRoomState, Product


def build_on_live_state(products: list[CatalogProduct]) -> LiveRoomState:
    """把 Phase 2A 数据库货盘转换成 Phase 2B 播中内存状态。

    当前 Reducer 仍以内存状态为主，因此 CLI 演示需要把 CatalogProduct 转换成
    `Product`。真实商品持久化和状态回写会放在后续阶段推进。
    """

    state_products = [
        Product(
            product_id=product.product_id,
            name=product.name,
            price=product.price,
            inventory=product.inventory,
            is_active=product.is_active,
            conversion_rate=product.conversion_rate,
            tags=product.tags,
        )
        for product in products
    ]
    return LiveRoomState(
        room_id=DEMO_ROOM_ID,
        lifecycle=LifecycleStage.ON_LIVE,
        products=state_products,
        current_product_id="p001",
    )


def main() -> int:
    """执行播中售罄事件演示并返回进程退出码。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)

    products = ProductCatalogRepository(settings).list_room_products(DEMO_ROOM_ID)
    state = build_on_live_state(products)
    trace_id = "trace-phase2b-demo"
    event = InventoryEvent(
        room_id=DEMO_ROOM_ID,
        product_id="p001",
        event_type=OnLiveEventType.SOLD_OUT,
        trace_id=trace_id,
    )

    result = OnLiveFlowService(ToolCallAuditStore(settings)).handle_sold_out_event(state, event)

    sold_out_product = result.updated_state.get_product("p001")
    print("Phase 2B on-live sold-out demo")
    print(f"room_id: {DEMO_ROOM_ID}")
    print(f"trace_id: {trace_id}")
    print(f"sold_out_product: {sold_out_product.product_id} {sold_out_product.name}")
    print(f"inventory_after_event: {sold_out_product.inventory}")
    print(f"is_active_after_event: {sold_out_product.is_active}")
    if result.backup_product is not None:
        print(f"backup_product: {result.backup_product.product_id} {result.backup_product.name}")
        print(f"current_product_id: {result.updated_state.current_product_id}")
    else:
        print("backup_product: none")
        print("current_product_id: unchanged")
    print(f"prompt_severity: {result.prompt.severity}")
    print(f"prompt: {result.prompt.message}")
    print(f"audit_ids: {', '.join(result.audit_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
