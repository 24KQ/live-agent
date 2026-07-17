"""Phase 6B WebSocket 连接管理器。

管理前端副屏的 WebSocket 连接，支持广播推送。
当没有连接时不执行轮询，不消耗数据库资源。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class WebSocketManager:
    """WebSocket 连接管理器。

    用法：
        manager = WebSocketManager()
        manager.connect(websocket)
        await manager.broadcast({"type": "agent_suggestion", "payload": {...}})
        manager.disconnect(websocket)
    """

    def __init__(self) -> None:
        self._connections: list[Any] = []
        self._connection_scopes: dict[Any, str | None] = {}

    @property
    def active_connections(self) -> int:
        """当前活跃连接数。"""
        return len(self._connections)

    def connect(self, websocket: Any, *, scope: str | None = None) -> None:
        """注册一个新的 WebSocket 连接，可选绑定到单一业务 scope。"""
        self._connections.append(websocket)
        self._connection_scopes[websocket] = scope

    def disconnect(self, websocket: Any) -> None:
        """移除一个 WebSocket 连接。

        如果连接不在列表中，静默忽略。
        """
        if websocket in self._connections:
            self._connections.remove(websocket)
        self._connection_scopes.pop(websocket, None)

    async def broadcast(self, message: dict[str, Any], *, scope: str | None = None) -> None:
        """向所有活跃连接广播 JSON 消息。

        当提供 scope 时，只向同 scope 连接发送；旧调用不提供 scope，保持历史全局
        广播行为。自动添加 timestamp 字段。
        发送失败的连接会被自动移除。
        """
        if not self._connections:
            return

        payload = {
            **message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        failed: list[Any] = []
        for ws in self._connections:
            if scope is not None and self._connection_scopes.get(ws) != scope:
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                failed.append(ws)

        # 移除失败连接
        for ws in failed:
            self.disconnect(ws)
