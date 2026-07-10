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
    """?????????? live_agent_danmaku_aggregates ????? 50 ??"""
    try:
        import psycopg
        from psycopg.rows import dict_row
        rid = room_id or "room-001"
        with psycopg.connect(**settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT category, summary, count, sample_contents, window_start "
                    "FROM live_agent_danmaku_aggregates "
                    "WHERE room_id = %(room_id)s "
                    "ORDER BY window_start DESC LIMIT 50;",
                    {"room_id": rid}
                )
                rows = cur.fetchall()
        if not rows:
            return {"danmaku_count": 0, "question_groups": []}
        return {
            "danmaku_count": sum(r["count"] for r in rows),
            "question_groups": [
                {"question": r["summary"], "count": r["count"],
                 "category": r["category"], "suggested_reply": ""}
                for r in rows
            ],
        }
    except Exception as exc:
        from fastapi.responses import JSONResponse
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




@app.get("/api/agent/suggestion")
async def get_agent_suggestion(room_id: str = ""):
    """触发播中 Agent 决策并返回建议。

    内联运行 Phase 5C on_live_agent_graph，返回当前建议。
    数据库无事件时 Agent 返回 finish 路由和空建议。
    """
    try:
        rid = room_id or "room-001"
        trace_id = f"trace-dashboard-{int(__import__('time').time())}"

        # 收集播中上下文：从数据库读弹幕聚合和库存
        import psycopg
        from psycopg.rows import dict_row
        danmaku_summary = []
        inventory_alerts = []

        try:
            with psycopg.connect(**settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT category, summary, count FROM live_agent_danmaku_aggregates "
                        "WHERE room_id = %(room_id)s ORDER BY window_start DESC LIMIT 5;",
                        {"room_id": rid}
                    )
                    for row in cur.fetchall():
                        danmaku_summary.append({
                            "category": row["category"],
                            "count": row["count"],
                            "summary": row["summary"],
                        })
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT p.product_id, p.name, p.inventory "
                        "FROM live_agent_room_products rp "
                        "JOIN live_agent_products p ON p.product_id = rp.product_id "
                        "WHERE rp.room_id = %(room_id)s AND p.inventory < 30 "
                        "ORDER BY p.inventory ASC;",
                        {"room_id": rid}
                    )
                    for row in cur.fetchall():
                        inventory_alerts.append({
                            "product_id": row["product_id"],
                            "product_name": row["name"],
                            "severity": "warning" if int(row["inventory"]) > 0 else "sold_out",
                        })
        except Exception:
            # DB 不可用时用默认空列表
            pass

        from src.core.on_live_agent_graph import (
            build_on_live_agent_graph,
            create_initial_on_live_state,
        )
        from src.core.on_live_agent_graph import _LocalServiceExecutor

        # 用真实服务 executor
        from src.core.on_live_flow import OnLiveFlowService
        from src.core.danmaku_flow import DanmakuFlowService
        from src.audit.tool_call_audit import ToolCallAuditStore
        audit_store = ToolCallAuditStore(settings=settings)
        on_live_service = OnLiveFlowService(audit_store=audit_store)
        danmaku_service = DanmakuFlowService(audit_store=audit_store)

        executor = _LocalServiceExecutor(
            on_live_service=on_live_service,
            danmaku_service=danmaku_service,
        )

        state = create_initial_on_live_state(
            room_id=rid,
            trace_id=trace_id,
            trust_score=0.7,
            danmaku_summary=danmaku_summary,
            inventory_alerts=inventory_alerts,
        )
        graph = build_on_live_agent_graph(executor=executor)
        result = graph.invoke(state)

        suggestion = result.get("suggestion") or ""
        if not suggestion:
            # 生成一个基于上下文的可读建议
            if inventory_alerts:
                suggestion = f"检测到 {len(inventory_alerts)} 个库存异常，建议检查备选商品并准备切换。"
            elif danmaku_summary:
                top = max(danmaku_summary, key=lambda d: d.get("count", 0))
                suggestion = f"弹幕高频问题：{top.get('summary', top.get('category', '未知'))}，建议主播重点回应。"
            else:
                suggestion = "直播运行正常，暂无需要干预的事项。"

        return {
            "suggestion": suggestion,
            "route": result.get("planner_route", "finish"),
            "goal": result.get("goal", ""),
            "has_alerts": len(inventory_alerts) > 0,
            "danmaku_count": sum(d.get("count", 0) for d in danmaku_summary),
            "timestamp": __import__('datetime').datetime.now().isoformat(),
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/review/llm/{room_id}")
async def get_llm_review(room_id: str):
    """用 LLM 生成播后自然语言复盘总结。

    从 decision_trace 表读取数据，传给 LLMPostLiveSummary。
    LLM 不可用时降级到结构化模板。
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(**settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT trace_id, anchor_action, business_result, trust_delta "
                    "FROM live_agent_decision_trace "
                    "WHERE room_id = %(room_id)s "
                    "ORDER BY created_at DESC;",
                    {"room_id": room_id}
                )
                rows = cur.fetchall()

        from src.skills.post_live_attribution import PostLiveAttribution
        from src.skills.post_live_review import PostLiveReview
        from src.skills.llm_post_live_summary import LLMPostLiveSummary

        traces = [
            {
                "anchor_action": r["anchor_action"],
                "business_result": r["business_result"],
                "trust_delta": Decimal(str(r["trust_delta"])),
            }
            for r in rows
        ]

        # 归因
        attr = PostLiveAttribution.calculate(traces)
        # 复盘
        review = PostLiveReview.review(traces)

        attribution_dict = {
            "total_decisions": attr.total_decisions,
            "adoption_rate": float(attr.adoption_rate),
            "accuracy_rate": float(attr.accuracy_rate),
            "unattributable_count": attr.unattributable_count,
        }

        # LLM 总结
        summarizer = LLMPostLiveSummary(settings=settings)
        llm_summary = summarizer.generate(
            attribution=attribution_dict,
            issues=review.get("issues", []),
        )

        return {
            "summary": llm_summary,
            "structured": {
                "total_decisions": attr.total_decisions,
                "adoption_rate": str(attr.adoption_rate),
                "accuracy_rate": str(attr.accuracy_rate),
                "trust_delta_total": str(review.get("trust_delta_total", Decimal("0"))),
                "issues": review.get("issues", []),
            },
            "decision_count": len(rows),
        }
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


front_dir = Path(__file__).resolve().parent.parent.parent / "front"
if front_dir.exists():
    app.mount("/", StaticFiles(directory=str(front_dir), html=True), name="static")

