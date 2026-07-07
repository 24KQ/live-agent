"""运行 Phase 2A 播前业务闭环演示。"""

from pathlib import Path
import sys


# 直接执行 `python scripts/run_phase2_pre_live_demo.py` 时，需要把仓库根目录
# 加入导入路径；否则 Python 只会从 scripts 目录查找模块。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skills.demo_data_seed import DEMO_ROOM_ID, initialize_phase2_schema, seed_phase2_demo_data
from src.skills.product_catalog import ProductCatalogRepository


def main() -> int:
    """初始化样例数据并演示播前准备流程。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)

    trace_id = "trace-phase2-demo"
    service = PreLiveBusinessFlowService(
        catalog_repository=ProductCatalogRepository(settings),
        audit_store=ToolCallAuditStore(settings),
    )
    result = service.prepare_room(
        room_id=DEMO_ROOM_ID,
        trace_id=trace_id,
        confirmed_setup=True,
    )

    print("Phase 2A pre-live demo")
    print(f"room_id: {DEMO_ROOM_ID}")
    print(f"trace_id: {trace_id}")
    print(f"products: {len(result.products)}")
    print("plan:")
    for item in result.plan.items[:5]:
        print(f"  {item.rank}. {item.product_name} ({item.role}) - {item.reason}")
    print("cards:")
    for card in result.cards:
        print(f"  {card.title}: {' / '.join(card.talking_points)}")
    print(f"setup_gate: {result.setup_gate.decision}, allowed={result.setup_gate.allowed}")
    print(f"setup_audit_id: {result.setup_audit_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
