# -*- coding: utf-8 -*-
"""Phase 4C LiveAgent Web 副屏 API Server。

FastAPI 应用，从 PostgreSQL 真实读取业务数据。
"""

from __future__ import annotations
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src.config.settings import get_settings
from src.skills.product_catalog import ProductCatalogRepository

app = FastAPI(title="LiveAgent Dashboard", version="0.4.0")
settings = get_settings()


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "LiveAgent"}


@app.get("/api/card/{product_id}")
async def get_card(product_id: str):
    try:
        from src.skills.llm_card_generator import LLMCardGenerator
        repo = ProductCatalogRepository(settings)
        all_products = repo.list_room_products("room-001")
        product = next((p for p in all_products if p.product_id == product_id), None)
        if product is None:
            return JSONResponse(status_code=404, content={"error": f"product {product_id} not found"})
        gen = LLMCardGenerator()
        card = gen.generate_card_with_fallback(product)
        return {
            "product_id": card.product_id, "title": card.title,
            "talking_points": card.talking_points, "opening_script": card.opening_script,
            "price_hint": card.price_hint, "risk_tips": card.risk_tips,
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/danmaku/summary")
async def get_danmaku_summary(room_id: str = ""):
    try:
        from src.skills.danmaku_aggregator import aggregate_danmaku_questions
        from src.skills.danmaku_events import DanmakuEvent
        from datetime import datetime, timezone
        rid = room_id or "room-001"
        sim_events = [
            DanmakuEvent(room_id=rid, viewer_id="v1",
                         content="这个产品能用多久",
                         event_time=datetime.now(timezone.utc), trace_id="demo-batch"),
            DanmakuEvent(room_id=rid, viewer_id="v2",
                         content="价格还能便宜吗",
                         event_time=datetime.now(timezone.utc), trace_id="demo-batch"),
        ]
        groups = aggregate_danmaku_questions(sim_events, window_seconds=5)
        return {
            "danmaku_count": len(sim_events),
            "question_groups": [
                {"question": grp.summary, "count": grp.count, "suggested_reply": ""}
                for grp in groups
            ],
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/alert/{room_id}")
async def get_alerts(room_id: str):
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(**settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT p.product_id, p.name, p.inventory "
                    "FROM live_agent_room_products rp "
                    "JOIN live_agent_products p ON p.product_id = rp.product_id "
                    "WHERE rp.room_id = %(room_id)s "
                    "ORDER BY p.inventory ASC;",
                    {"room_id": room_id}
                )
                rows = cur.fetchall()
        alerts = []
        backup_ids = []
        for row in rows:
            inv = int(row["inventory"])
            pid = row["product_id"]
            if inv == 0:
                alerts.append({"product_id": pid, "type": "sold_out",
                              "message": f"{row['name']} 已售罄"})
            elif inv < 30:
                alerts.append({"product_id": pid, "type": "low_stock",
                              "message": f"{row['name']} 库存仅剩 {inv} 件"})
            else:
                continue
            backup_ids.append(pid)
        return {"room_id": room_id, "alerts": alerts, "backup_products": backup_ids}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/review/{room_id}")
async def get_review(room_id: str):
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(**settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT trace_id, anchor_action, business_result, trust_delta, "
                    "lift, final_trust_score, created_at::text as created_at "
                    "FROM live_agent_decision_trace "
                    "WHERE room_id = %(room_id)s "
                    "ORDER BY created_at DESC;",
                    {"room_id": room_id}
                )
                rows = cur.fetchall()
        if not rows:
            return {"room_id": room_id, "total_decisions": 0,
                    "message": "本场暂无决策记录"}
        from src.skills.post_live_attribution import PostLiveAttribution
        traces = [{"anchor_action": r["anchor_action"],
                   "business_result": r["business_result"]} for r in rows]
        attr = PostLiveAttribution.calculate(traces)
        total_delta = sum(Decimal(str(r["trust_delta"])) for r in rows)
        return {
            "room_id": room_id, "total_decisions": attr.total_decisions,
            "adoption_rate": str(attr.adoption_rate),
            "accuracy_rate": str(attr.accuracy_rate),
            "trust_delta_total": str(total_delta),
            "decision_count": len(rows),
            "recent_decisions": [
                {"trace_id": r["trace_id"], "anchor_action": r["anchor_action"],
                 "business_result": r["business_result"],
                 "trust_delta": r["trust_delta"],
                 "created_at": r["created_at"]}
                for r in rows[:10]
            ],
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


front_dir = Path(__file__).resolve().parent.parent.parent / "front"
if front_dir.exists():
    app.mount("/", StaticFiles(directory=str(front_dir), html=True), name="static")
