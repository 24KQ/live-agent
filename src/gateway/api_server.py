"""Phase 4B LiveAgent Web 副屏 API Server。

FastAPI 应用，暴露现有业务能力为 REST API。
端口默认 8100，启动：python -m uvicorn src.gateway.api_server:app --port 8100
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path


app = FastAPI(title="LiveAgent Dashboard", version="0.1.0")


# ---- Health ----

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "LiveAgent"}


# ---- 手卡 API ----

@app.get("/api/card/{product_id}")
async def get_card(product_id: str):
    """获取指定商品的讲解手卡（LLM 生成 + 降级）。"""
    try:
        from src.skills.llm_card_generator import LLMCardGenerator
        from src.skills.product_catalog import CatalogProduct
        from decimal import Decimal

        # 用模拟商品数据展示（后续接真实货盘）
        product = CatalogProduct(
            product_id=product_id,
            name="智能净水壶",
            category="厨房电器",
            price=299.00,
            inventory=150,
            conversion_rate=0.12,
            commission_rate=0.10,
            tags=["高利润", "爆款"],
        )
        gen = LLMCardGenerator()
        card = gen.generate_card_with_fallback(product)
        return {
            "product_id": card.product_id,
            "title": card.title,
            "talking_points": card.talking_points,
            "opening_script": card.opening_script,
            "price_hint": card.price_hint,
            "risk_tips": card.risk_tips,
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ---- 弹幕聚合 API ----

@app.get("/api/danmaku/summary")
async def get_danmaku_summary(room_id: str = ""):
    """获取指定直播间的弹幕聚合摘要（高频问题 + 建议回复）。"""
    try:
        from src.skills.danmaku_aggregator import aggregate_danmaku_questions
        from src.skills.danmaku_reply_generator import generate_danmaku_reply
        from src.skills.danmaku_events import DanmakuEvent
        from datetime import datetime, timezone

        # 模拟少量弹幕数据展示（后续接 Kafka consumer）
        sim_events = [
            DanmakuEvent(
                room_id=room_id,
                viewer_id="v1",
                content="这个产品能用多久",
                event_time=datetime.now(timezone.utc),
                trace_id="demo-batch",
            ),
            DanmakuEvent(
                room_id=room_id,
                viewer_id="v2",
                content="价格还能便宜吗",
                event_time=datetime.now(timezone.utc),
                trace_id="demo-batch",
            ),
        ]
        groups = aggregate_danmaku_questions(sim_events, window_seconds=5)
        replies = [generate_danmaku_reply(g) for g in groups]

        return {
            "danmaku_count": len(sim_events),
            "question_groups": [
                {
                    "question": grp.summary,
                    "count": grp.count,
                    "suggested_reply": next(
                        (r.reply_text for r in replies if grp.summary in r.reply_text),
                        "",
                    ),
                }
                for grp in groups
            ],
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ---- 告警 API ----

@app.get("/api/alert/{room_id}")
async def get_alerts(room_id: str):
    """获取当前直播间的库存/售罄/流量告警。"""
    return {
        "room_id": room_id,
        "alerts": [
            {"product_id": "p001", "type": "low_stock", "message": "库存低于 10 件，建议推荐备选品"},
        ],
        "backup_products": [],
    }


# ---- 复盘 API ----

@app.get("/api/review/{room_id}")
async def get_review(room_id: str):
    """获取指定直播间的播后复盘报告。"""
    try:
        from src.skills.post_live_attribution import PostLiveAttribution
        from src.skills.post_live_review import PostLiveReview

        traces = [
            {"anchor_action": "accepted", "business_result": "good", "trust_delta": 0.05},
            {"anchor_action": "rejected", "business_result": "agent_right", "trust_delta": 0.03},
        ]
        attr = PostLiveAttribution.calculate(traces)
        report = PostLiveReview.review(traces)

        return {
            "room_id": room_id,
            "total_decisions": report["total_decisions"],
            "adoption_rate": str(attr.adoption_rate),
            "accuracy_rate": str(attr.accuracy_rate),
            "trust_delta_total": str(report["trust_delta_total"]),
            "issues": report["issues"],
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ---- 静态文件 (前端) ----

front_dir = Path(__file__).resolve().parent.parent.parent / "front"
if front_dir.exists():
    app.mount("/", StaticFiles(directory=str(front_dir), html=True), name="static")
