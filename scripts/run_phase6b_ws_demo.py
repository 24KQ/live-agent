"""Phase 6B WebSocket 推送演示脚本。

启动 API Server 后连接 WebSocket 验证推送。

用法：
    python scripts/run_phase6b_ws_demo.py
"""

from __future__ import annotations

import sys
import os
import json
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime


async def main():
    print("#" * 60)
    print(f"  Phase 6B WebSocket 推送演示")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("#" * 60)
    print()
    print("  请先在另一个终端启动 API Server:")
    print("    python -m uvicorn src.gateway.api_server:app --port 8100")
    print()
    print("  然后运行此脚本")
    print()

    import websockets
    uri = "ws://localhost:8100/ws"

    try:
        async with websockets.connect(uri) as ws:
            print(f"  WebSocket 已连接: {uri}")
            print("  等待 15 秒接收推送消息...")
            print()

            for i in range(3):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(msg)
                    msg_type = data.get("type", "unknown")
                    print(f"  [{i+1}] 收到推送: {msg_type}")
                    if msg_type == "agent_suggestion":
                        suggestion = data.get("payload", {}).get("suggestion", "")
                        print(f"      建议: {suggestion[:60]}...")
                    elif msg_type == "danmaku_update":
                        count = data.get("payload", {}).get("danmaku_count", 0)
                        print(f"      弹幕: {count} 条")
                    elif msg_type == "alert_update":
                        alerts = data.get("payload", {}).get("alerts", [])
                        print(f"      告警: {len(alerts)} 条")
                    elif msg_type == "review_update":
                        print(f"      复盘: LLM 总结")
                except asyncio.TimeoutError:
                    pass

            print()
            print("  演示完成，关闭连接。")
    except ConnectionRefusedError:
        print("  ❌ 连接失败: API Server 未启动")
        print("  请先运行: python -m uvicorn src.gateway.api_server:app --port 8100")


if __name__ == "__main__":
    asyncio.run(main())
