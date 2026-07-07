"""Phase 1 播前最小闭环演示脚本。

运行方式：
    python scripts/run_phase1_pre_live_demo.py

脚本不会接入 LLM，也不会访问真实淘宝 API。它只构造本地模拟商品，
演示改价工具如何经过 hard-gate、Reducer 和 PostgreSQL 审计。
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import psycopg

# 直接执行脚本时，确保可以从仓库根目录导入 src 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audit.tool_call_audit import ToolCallAuditStore
from src.config.settings import get_settings
from src.core.pre_live_flow import PreLiveFlowService
from src.state.models import LiveRoomState, Product


def init_audit_table() -> None:
    """初始化 Phase 1 审计表。

    演示脚本每次运行前都执行一次建表 SQL，保证新环境可以直接体验。
    SQL 使用 IF NOT EXISTS 和 advisory lock，重复运行不会破坏已有数据。
    """

    settings = get_settings()
    sql = (PROJECT_ROOT / "docker" / "init_phase1_audit.sql").read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
        connection.commit()


def build_demo_state() -> LiveRoomState:
    """构造 Phase 1 演示用的播前状态。"""

    return LiveRoomState(
        room_id="room-demo-001",
        products=[
            Product(
                product_id="p001",
                name="轻盈保温杯",
                price=Decimal("99.00"),
                inventory=20,
                tags=["引流款"],
            )
        ],
    )


def main() -> int:
    """执行演示并返回进程退出码。"""

    init_audit_table()
    service = PreLiveFlowService(ToolCallAuditStore(get_settings()))
    state = build_demo_state()

    print("Phase 1 播前最小闭环演示")
    print("1. 查询货盘")
    for product in service.query_products(state):
        print(f"   - {product['product_id']} {product['name']} price={product['price']} inventory={product['inventory']}")

    print("2. 请求改价但未确认，预期进入 hard-gate")
    pending = service.request_price_change(
        state=state,
        product_id="p001",
        new_price=Decimal("89.90"),
        confirmed=False,
        trace_id="demo-phase1-pending",
    )
    print(f"   - allowed={pending.gate_result.allowed}")
    print(f"   - requires_confirmation={pending.gate_result.requires_confirmation}")
    print(f"   - price_after_pending={pending.updated_state.get_product('p001').price}")
    print(f"   - trace_id={pending.trace_id}")

    print("3. 主播确认后再次执行改价，预期 Reducer 更新状态并写审计")
    approved = service.request_price_change(
        state=state,
        product_id="p001",
        new_price=Decimal("89.90"),
        confirmed=True,
        trace_id="demo-phase1-approved",
    )
    print(f"   - allowed={approved.gate_result.allowed}")
    print(f"   - price_after_approved={approved.updated_state.get_product('p001').price}")
    print(f"   - audit_id={approved.audit_id}")
    print(f"   - trace_id={approved.trace_id}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
