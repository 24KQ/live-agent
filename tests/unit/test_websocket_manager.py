"""Phase 6B WebSocket 管理器单元测试。

测试 WebSocketManager 的连接管理、广播和空连接保护。
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock

from src.gateway.websocket_manager import WebSocketManager


class TestWebSocketManager:

    def setup_method(self):
        self.manager = WebSocketManager()

    def test_connect_adds_connection(self):
        """connect() 应增加连接计数。"""
        mock_ws = MagicMock()
        self.manager.connect(mock_ws)
        assert self.manager.active_connections == 1

    def test_disconnect_removes_connection(self):
        """disconnect() 应移除连接并减少计数。"""
        mock_ws = MagicMock()
        self.manager.connect(mock_ws)
        self.manager.disconnect(mock_ws)
        assert self.manager.active_connections == 0

    def test_disconnect_unknown_does_not_error(self):
        """断开未知连接不报错。"""
        mock_ws = MagicMock()
        self.manager.disconnect(mock_ws)
        assert self.manager.active_connections == 0

    def test_broadcast_sends_to_all(self):
        """广播消息应发送给所有连接。"""
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        self.manager.connect(ws1)
        self.manager.connect(ws2)

        import asyncio
        asyncio.run(self.manager.broadcast({"type": "test", "payload": {}}))

        ws1.send_json.assert_called_once()
        ws2.send_json.assert_called_once()

    def test_broadcast_empty_connections_does_not_error(self):
        """无连接时广播不报错。"""
        import asyncio
        asyncio.run(self.manager.broadcast({"type": "test", "payload": {}}))

    def test_broadcast_sends_correct_format(self):
        """广播消息格式应包含 type、payload、timestamp。"""
        ws = AsyncMock()
        self.manager.connect(ws)

        import asyncio
        asyncio.run(self.manager.broadcast({"type": "agent_suggestion", "payload": {"text": "test"}}))

        sent = ws.send_json.call_args[0][0]
        assert sent["type"] == "agent_suggestion"
        assert "payload" in sent
        assert "timestamp" in sent

    def test_broadcast_supports_agent_harness_update_payload(self):
        """Phase 6C Harness 状态推送应保留节点路径和审批信息。"""
        ws = AsyncMock()
        self.manager.connect(ws)

        import asyncio
        asyncio.run(
            self.manager.broadcast(
                {
                    "type": "agent_harness_update",
                    "payload": {
                        "trace_id": "trace-ws-harness",
                        "completed_nodes": ["load_context", "human_approval_interrupt"],
                        "pending_approval": True,
                    },
                }
            )
        )

        sent = ws.send_json.call_args[0][0]
        assert sent["type"] == "agent_harness_update"
        assert sent["payload"]["pending_approval"] is True
        assert "human_approval_interrupt" in sent["payload"]["completed_nodes"]

    def test_broadcast_skips_disconnected(self):
        """已断开的连接应被跳过并移除。"""
        ws_ok = AsyncMock()
        ws_bad = AsyncMock()
        ws_bad.send_json.side_effect = Exception("disconnected")

        self.manager.connect(ws_ok)
        self.manager.connect(ws_bad)

        import asyncio
        asyncio.run(self.manager.broadcast({"type": "test", "payload": {}}))

        ws_ok.send_json.assert_called_once()
        assert self.manager.active_connections == 1
