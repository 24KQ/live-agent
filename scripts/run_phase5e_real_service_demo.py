"""Phase 5E Agent 接真实服务 CLI 演示。

演示三种场景：
1. 弹幕聚合 — 调用真实 DanmakuFlowService
2. 库存告警 — 调用真实 OnLiveFlowService
3. 推荐备用 — 调用真实 recommend_backup_product

用法：
    python scripts/run_phase5e_real_service_demo.py
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone, timedelta
from decimal import Decimal

from src.core.on_live_agent_graph import (
    OnLiveAgentGraphState,
    build_on_live_agent_graph,
    create_initial_on_live_state,
    _LocalServiceExecutor,
)
from src.core.danmaku_flow import DanmakuFlowService
from src.core.on_live_flow import OnLiveFlowService
from src.audit.tool_call_audit import ToolCallAuditStore, AuditEvent
from src.state.models import LiveRoomState, Product


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def main():
    print(f"\n{'#' * 60}")
    print(f"  Phase 5E Agent 接真实服务演示")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#' * 60}")

    # 构造基础状态
    products = [
        Product(product_id="prod-001", name="热销水杯", price=Decimal("29.9"), inventory=0),
        Product(product_id="prod-002", name="保温杯", price=Decimal("49.9"), inventory=200),
        Product(product_id="prod-003", name="玻璃杯", price=Decimal("19.9"), inventory=150),
    ]
    state = LiveRoomState(
        room_id="room-5e-demo",
        lifecycle="ON_LIVE",
        products=products,
        current_product_id="prod-001",
    )

    # 构造审计存储
    from src.config.settings import Settings, get_settings
    try:
        settings = get_settings()
    except Exception:
        settings = Settings(_env_file=".env", _env_file_encoding="utf-8")
    audit_store = ToolCallAuditStore(settings=settings)
    on_live_service = OnLiveFlowService(audit_store=audit_store)
    danmaku_service = DanmakuFlowService(audit_store=audit_store)

    executor = _LocalServiceExecutor(
        on_live_service=on_live_service,
        danmaku_service=danmaku_service,
    )

    # 场景 1: 弹幕聚合
    section("场景 1: 弹幕聚合（DanmakuFlowService 真实调用）")
    from src.skills.danmaku_events import DanmakuEvent
    basetime = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
    danmaku_events = [
        DanmakuEvent(room_id="room-5e-demo", viewer_id="v1", content="这个多少钱",
                     event_time=basetime, trace_id="trace-5e"),
        DanmakuEvent(room_id="room-5e-demo", viewer_id="v2", content="价格是多少",
                     event_time=basetime + timedelta(seconds=1), trace_id="trace-5e"),
        DanmakuEvent(room_id="room-5e-demo", viewer_id="v3", content="还有库存吗",
                     event_time=basetime + timedelta(seconds=2), trace_id="trace-5e"),
    ]
    agent_state = create_initial_on_live_state(
        room_id="room-5e-demo",
        trace_id="trace-5e-demo-1",
        trust_score=0.7,
        danmaku_summary=[{"category": "price", "count": 2, "summary": "价格问题"}],
        inventory_alerts=[],
    )
    graph = build_on_live_agent_graph(executor=executor)
    result = graph.invoke(agent_state)
    print(f"  路由: {result.get('planner_route', 'N/A')}")
    print(f"  目标: {result.get('goal', 'N/A')}")
    print(f"  建议: {result.get('suggestion', '无')}")

    # 场景 2: 库存告警
    section("场景 2: 库存告警（OnLiveFlowService 真实调用）")
    fresh_state = state.model_copy(deep=True)
    agent_state2 = create_initial_on_live_state(
        room_id="room-5e-demo",
        trace_id="trace-5e-demo-2",
        trust_score=0.7,
        danmaku_summary=[],
        inventory_alerts=[
            {"product_id": "prod-001", "product_name": "热销水杯", "severity": "warning"},
        ],
    )
    result2 = graph.invoke(agent_state2)
    print(f"  路由: {result2.get('planner_route', 'N/A')}")
    print(f"  建议: {result2.get('suggestion', '无')}")

    # 场景 3: 兼容旧 _DefaultExecutor（向后兼容验证）
    section("场景 3: 向后兼容（无 service 时退回 _DefaultExecutor）")
    default_graph = build_on_live_agent_graph()
    agent_state3 = create_initial_on_live_state(
        room_id="room-5e-demo",
        trace_id="trace-5e-demo-3",
        trust_score=0.7,
        danmaku_summary=[],
        inventory_alerts=[],
    )
    result3 = default_graph.invoke(agent_state3)
    # 无事件时应走 finish，不退到 error
    assert result3.get("error") is None, f"Default executor failed: {result3.get('error')}"
    print(f"  路由: {result3.get('planner_route', 'N/A')}")
    print(f"  目标: {result3.get('goal', 'N/A')}")
    print(f"  错误: {result3.get('error', '无')}")
    print(f"  兼容性验证: OK 无异常")

    print(f"\n{'#' * 60}")
    print(f"  演示完成")
    print(f"{'#' * 60}")


if __name__ == "__main__":
    main()
