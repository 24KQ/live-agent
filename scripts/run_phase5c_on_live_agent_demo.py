"""Phase 5C 播中 Agent 动态决策 CLI 演示。

演示三种播中场景：
1. 正常直播：弹幕少量，无告警 → Agent 不做干预
2. 弹幕价格集中：大量价格问题 → Agent 建议主播强调优惠
3. 库存告警：商品售罄 → Agent 建议切换备用商品

用法：
    python scripts/run_phase5c_on_live_agent_demo.py
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime

from src.core.on_live_agent_graph import (
    build_on_live_agent_graph,
    create_initial_on_live_state,
)


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def demo_scenario(name: str, danmaku: list, alerts: list, trust_score: float = 0.7):
    """运行单个播中场景并输出结果。"""
    print(f"\n{'-' * 50}")
    print(f"  场景：{name}")
    print(f"{'-' * 50}")
    print(f"  弹幕摘要：{danmaku}")
    print(f"  库存告警：{alerts}")
    print(f"  信任分：{trust_score}")

    state = create_initial_on_live_state(
        room_id="room-5c-demo",
        trace_id=f"trace-5c-{name[:4].lower()}",
        trust_score=trust_score,
        danmaku_summary=danmaku,
        inventory_alerts=alerts,
    )

    graph = build_on_live_agent_graph()
    result = graph.invoke(state)

    print(f"\n  决策结果：")
    print(f"    路由：{result.get('planner_route', 'N/A')}")
    print(f"    目标：{result.get('goal', 'N/A')}")
    print(f"    建议：{result.get('suggestion', '无')}")
    print(f"    执行工具：{result.get('executed_tools', [])}")
    print(f"    状态：{result.get('setup_status', 'N/A')}")
    print(f"    错误：{result.get('error', '无')}")
    print(f"    执行节点：{result.get('completed_nodes', [])}")


def main():
    print(f"\n{'#' * 60}")
    print(f"  Phase 5C 播中 Agent 动态决策演示")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#' * 60}")

    # 场景 1：正常直播
    demo_scenario(
        name="正常直播-无事件",
        danmaku=[],
        alerts=[],
    )

    # 场景 2：弹幕价格集中
    demo_scenario(
        name="弹幕价格集中",
        danmaku=[
            {"category": "price", "count": 15, "summary": "价格相关问题"},
            {"category": "stock", "count": 2, "summary": "库存相关问题"},
        ],
        alerts=[],
    )

    # 场景 3：库存告警
    demo_scenario(
        name="库存告警",
        danmaku=[
            {"category": "price", "count": 3, "summary": "价格相关问题"},
        ],
        alerts=[
            {"product_id": "prod-001", "product_name": "热销杯子", "severity": "warning"},
        ],
    )

    # 场景 4：低信任分
    demo_scenario(
        name="低信任分",
        danmaku=[
            {"category": "price", "count": 12, "summary": "价格相关问题"},
        ],
        alerts=[
            {"product_id": "prod-002", "product_name": "限量T恤", "severity": "critical"},
        ],
        trust_score=0.3,
    )

    print(f"\n{'#' * 60}")
    print(f"  演示完成")
    print(f"{'#' * 60}")


if __name__ == "__main__":
    main()
