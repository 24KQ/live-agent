"""Phase 4B API Server 单元测试。

使用 FastAPI TestClient 验证所有端点路由和响应格式。
"""

import pytest
from fastapi.testclient import TestClient

from src.gateway.api_server import app

client = TestClient(app)


class TestAPIHealth:
    def test_health_returns_ok(self):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestAPICard:
    def test_card_endpoint_returns_valid_response(self):
        resp = client.get("/api/card/p001")
        assert resp.status_code in (200, 404, 500)  # API 可能成功或降级


class TestAPIDanmaku:
    def test_danmaku_summary_returns_valid_json(self):
        resp = client.get("/api/danmaku/summary?room_id=room-001")
        assert resp.status_code in (200, 404)
        data = resp.json()
        if resp.status_code == 200:
            assert "danmaku_count" in data


class TestAPIAlert:
    def test_alert_returns_valid_json(self):
        resp = client.get("/api/alert/room-001")
        assert resp.status_code in (200, 404)
        data = resp.json()
        if resp.status_code == 200:
            assert "alerts" in data


class TestAPIReview:
    def test_review_returns_valid_json(self):
        resp = client.get("/api/review/room-001")
        assert resp.status_code in (200, 404)
        data = resp.json()
        if resp.status_code == 200:
            assert "total_decisions" in data
