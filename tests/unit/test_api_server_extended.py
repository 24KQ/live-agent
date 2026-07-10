"""Phase 6A 前端扩展 API 单元测试。

测试新增的 Agent 建议 API、LLM 复盘 API、弹幕 fallback 数据。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from src.gateway.api_server import app


client = TestClient(app)


class TestAgentSuggestionAPI:

    def test_agent_suggestion_returns_json(self):
        """Agent 建议 API 返回有效 JSON。"""
        resp = client.get("/api/agent/suggestion", params={"room_id": "room-001"})
        assert resp.status_code == 200
        data = resp.json()
        assert "suggestion" in data
        assert "route" in data
        assert "goal" in data
        assert "timestamp" in data


class TestLLMReviewAPI:

    def test_llm_review_returns_summary(self):
        """LLM 复盘 API 返回自然语言总结。"""
        resp = client.get("/api/review/llm/room-001")
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert "structured" in data


class TestDanmakuFallback:

    def test_danmaku_fallback_returns_data_when_db_empty(self):
        """数据库无弹幕时返回 seed 模拟数据。"""
        resp = client.get("/api/danmaku/summary", params={"room_id": "room-empty"})
        assert resp.status_code == 200
        data = resp.json()
        # 应有 question_groups
        assert "question_groups" in data


class TestAlertFallback:

    def test_alert_fallback_returns_data_when_db_empty(self):
        """数据库无产品时返回 seed 模拟告警。"""
        resp = client.get("/api/alert/room-empty")
        assert resp.status_code == 200
        data = resp.json()
        assert "alerts" in data
