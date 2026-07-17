# -*- coding: utf-8 -*-
"""Phase 4C LiveAgent Web 副屏 API Server。

FastAPI 应用，从 PostgreSQL 真实读取业务数据。
"""

from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, model_validator
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.config.settings import get_settings
from src.gateway.harness_dashboard_service import (
    create_default_harness_dashboard_service,
    create_in_memory_harness_dashboard_service,
)
from src.gateway.harness_session_store import HarnessSessionNotFoundError
from src.gateway.agent_evaluation_service import AgentEvaluationService, AgentEvaluationWorker
from src.audit.tool_call_audit import ToolCallAuditStore
from src.gateway.agent_evaluation_store import (
    EvaluationRunNotFoundError,
    PostgresAgentEvaluationStore,
    initialize_agent_evaluation_schema,
)
from src.gateway.operator_auth import authenticate_request, authorize_action, OperatorRole, OperatorAuthError, OperatorPermissionError, extract_idempotency_key
from src.gateway.harness_session_store import PostgresHarnessSessionStore
from src.core.agent_evaluation import AgentRuleEvaluator
from src.core.agent_replay import AgentReplayService
from src.gateway.websocket_manager import WebSocketManager
from src.skills.product_catalog import ProductCatalogRepository
from src.plan_engine.preemption import PreemptionEvidenceRef

app = FastAPI(title="LiveAgent Dashboard", version="0.4.0")
settings = get_settings()
websocket_manager = WebSocketManager()
_harness_dashboard_service = None
_agent_evaluation_service = None
_agent_evaluation_worker = None


class HarnessStartRequest(BaseModel):
    """Web 副屏启动 Harness Agent 会话的请求体。"""

    room_id: str = Field(..., min_length=1)
    trace_id: str | None = None
    anchor_id: str | None = "anchor-demo"
    preemption_evidence_refs: list[PreemptionEvidenceRef] = Field(default_factory=list)
    final_suggestion_fact: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _evidence_and_suggestion_are_closed(self) -> "HarnessStartRequest":
        """PlanEngine 证据与建议必须同时出现，并引用同一已应用事实。"""

        if bool(self.preemption_evidence_refs) != bool(self.final_suggestion_fact):
            raise ValueError("preemption evidence 与 final_suggestion_fact 必须同时提供")
        if self.preemption_evidence_refs and self.final_suggestion_fact not in {
            evidence.final_suggestion_fact for evidence in self.preemption_evidence_refs
        }:
            raise ValueError("final_suggestion_fact 与 EvidenceRef 不一致")
        return self


class HarnessApprovalRequest(BaseModel):
    """Web 副屏提交人工审批结果的请求体。"""

    trace_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    decision: Literal["approved", "rejected"]
    operator_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)


class EvaluationCreateRequest(BaseModel):
    """创建 Agent 异步评估任务的请求体。"""

    trace_id: str = Field(..., min_length=1)
    profile: str = Field(default="production_hybrid", min_length=1)


class EvaluationReviewRequest(BaseModel):
    """提交人工复核 overlay 的请求体。"""

    operator_id: str = Field(..., min_length=1)
    conclusion: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)


def set_harness_dashboard_service(service) -> None:
    """替换 HarnessDashboardService，供单元测试注入内存版本。

    生产运行时不调用该函数，默认懒加载 PostgreSQL 持久化版本。
    """

    global _harness_dashboard_service
    _harness_dashboard_service = service


def get_harness_dashboard_service():
    """获取副屏 Harness 服务。

    默认使用 PostgreSQL store + PostgreSQL checkpointer；如果调用方显式注入了
    内存版本，则用于单元测试或无数据库演示。
    """

    global _harness_dashboard_service
    if _harness_dashboard_service is None:
        _harness_dashboard_service = create_default_harness_dashboard_service()
    return _harness_dashboard_service


def set_agent_evaluation_service(service) -> None:
    """替换 AgentEvaluationService，供单元测试注入内存版本。"""

    global _agent_evaluation_service
    _agent_evaluation_service = service


def get_agent_evaluation_service():
    """获取 Agent Evaluation 服务。

    默认使用 PostgreSQL 作为评估事实源和任务队列；单元测试通过 setter 注入
    内存版本，避免依赖开发者本机数据库。
    """

    global _agent_evaluation_service
    if _agent_evaluation_service is None:
        initialize_agent_evaluation_schema(settings)
        _agent_evaluation_service = AgentEvaluationService(store=PostgresAgentEvaluationStore(settings))
    return _agent_evaluation_service


def set_agent_evaluation_worker(worker) -> None:
    """替换评估 Worker，供 API 单元测试注入 fake replay。"""

    global _agent_evaluation_worker
    _agent_evaluation_worker = worker


def get_agent_evaluation_worker():
    """获取评估 Worker。

    默认 Worker 使用空依赖的 replay service，只适合作为本地兜底；生产和测试应
    通过 setter 注入带真实 store/checkpoint 的实例。
    """

    global _agent_evaluation_worker
    if _agent_evaluation_worker is None:
        service = get_agent_evaluation_service()
        _agent_evaluation_worker = AgentEvaluationWorker(
            store=service.store,
            replay_service=AgentReplayService(
                session_store=PostgresHarnessSessionStore(settings),
                audit_store=ToolCallAuditStore(settings),
            ),
            evaluator=AgentRuleEvaluator(),
        )
    return _agent_evaluation_worker


async def _broadcast_harness_status(payload: dict) -> None:
    """向副屏推送最新 Harness Agent 状态。"""

    await websocket_manager.broadcast({"type": "agent_harness_update", "payload": payload})


async def _broadcast_evaluation_status(payload: dict) -> None:
    """向运维页面推送 Agent Evaluation 状态摘要。"""

    await websocket_manager.broadcast({"type": "agent_evaluation_update", "payload": payload})


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "LiveAgent"}


@app.get("/evaluation")
async def evaluation_page():
    """Agent Evaluation 运维页面入口。"""

    page = Path(__file__).resolve().parent.parent.parent / "front" / "evaluation.html"
    if not page.exists():
        return JSONResponse(status_code=404, content={"error": "evaluation page not found"})
    return FileResponse(str(page))


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


@app.post("/api/agent/harness/start")
async def start_harness_session(request: HarnessStartRequest):
    """启动一条 Web 可观测的播中 Harness Agent 会话。

    返回值包含 LangGraph 节点路径、审计状态和 trace_id。旧 Harness approval 不能
    授予经营写权限；Phase 14 的可信 OperatorDecision 将由受控工作台链路提供。
    """

    try:
        status = get_harness_dashboard_service().start_session(
            room_id=request.room_id,
            trace_id=request.trace_id,
            anchor_id=request.anchor_id,
            preemption_evidence_refs=request.preemption_evidence_refs,
            final_suggestion_fact=request.final_suggestion_fact,
        )
        await _broadcast_harness_status(status)
        return status
    except Exception as exc:  # noqa: BLE001 - API 入口需要把底层异常转换成明确 JSON。
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/agent/harness/status")
async def get_harness_status(trace_id: str):
    """读取指定 trace_id 的 Harness Agent 会话状态。"""

    try:
        return get_harness_dashboard_service().get_status(trace_id)
    except HarnessSessionNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"harness session {trace_id} not found"})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/agent/harness/approval")
async def submit_harness_approval(http_request: Request, request: HarnessApprovalRequest):
    """提交 Web 人审结果，并用同一 thread_id 恢复 LangGraph。"""

    try:
        # Phase 7B: 操作员鉴权 — 需 operator 及以上角色
        identity = authenticate_request(dict(http_request.headers))
        authorize_action(identity, OperatorRole.OPERATOR)

        status = get_harness_dashboard_service().submit_approval(
            trace_id=request.trace_id,
            room_id=request.room_id,
            tool_name=request.tool_name,
            decision=request.decision,
            operator_id=request.operator_id,
            reason=request.reason,
        )
        await _broadcast_harness_status(status)
        return status
    except HarnessSessionNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"harness session {request.trace_id} not found"})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/agent/evaluations", status_code=202)
async def create_agent_evaluation(request: EvaluationCreateRequest):
    """创建 Agent 回放评估任务。

    评估任务默认异步执行，本端点只负责幂等入队并返回 HTTP 202。调用方可以
    通过 `/api/agent/evaluations/{evaluation_id}` 查询 Worker 处理结果。
    """

    try:
        payload = get_agent_evaluation_service().create_evaluation(
            trace_id=request.trace_id,
            profile=request.profile,
        )
        await _broadcast_evaluation_status(payload)
        return payload
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/agent/evaluations/{evaluation_id}")
async def get_agent_evaluation(evaluation_id: str):
    """读取 Agent Evaluation 任务状态和评分摘要。"""

    try:
        return get_agent_evaluation_service().get_evaluation(evaluation_id)
    except EvaluationRunNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"evaluation {evaluation_id} not found"})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/agent/replays/{trace_id}")
async def get_agent_replay(trace_id: str):
    """读取最近一次持久化的 Agent 回放快照。"""

    try:
        payload = get_agent_evaluation_service().get_latest_replay(trace_id)
        if payload is None:
            return JSONResponse(status_code=404, content={"error": f"replay for {trace_id} not found"})
        return payload
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/agent/evaluations/{evaluation_id}/reviews")
async def create_agent_evaluation_review(evaluation_id: str, http_request: Request, request: EvaluationReviewRequest):
    """提交人工复核 overlay，不覆盖原始机器评分。"""

    try:
        # Phase 7B: 操作员鉴权 — 需 reviewer 及以上角色
        identity = authenticate_request(dict(http_request.headers))
        authorize_action(identity, OperatorRole.REVIEWER)

        payload = get_agent_evaluation_service().add_review(
            evaluation_id=evaluation_id,
            operator_id=request.operator_id,
            conclusion=request.conclusion,
            reason=request.reason,
        )
        await _broadcast_evaluation_status({"evaluation_id": evaluation_id, **payload})
        return payload
    except EvaluationRunNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"evaluation {evaluation_id} not found"})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": str(exc)})


# Phase 7B: OperatorAuthError 和 OperatorPermissionError 的全局异常处理
@app.exception_handler(OperatorAuthError)
async def operator_auth_handler(http_request: Request, exc: OperatorAuthError):
    """认证失败返回 401。"""
    return JSONResponse(status_code=401, content={"error": str(exc)})


@app.exception_handler(OperatorPermissionError)
async def operator_permission_handler(http_request: Request, exc: OperatorPermissionError):
    """权限不足返回 403。"""
    return JSONResponse(status_code=403, content={"error": str(exc)})


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


@app.websocket("/ws")
async def dashboard_websocket(websocket: WebSocket):
    """副屏 WebSocket 入口。

    连接建立后只负责接收保活消息并向连接池注册；业务数据由后台任务和审批接口通过
    `WebSocketManager.broadcast()` 推送。
    """

    await websocket.accept()
    websocket_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        websocket_manager.disconnect(websocket)
    except Exception:
        websocket_manager.disconnect(websocket)


async def _json_payload(value):
    """把端点返回值整理成可广播 JSON。

    部分历史端点在异常时返回 JSONResponse；后台推送不应该把 Response 对象直接发给前端，
    因此这里统一降级为错误 payload。
    """

    if isinstance(value, JSONResponse):
        return {"error": "endpoint returned JSONResponse", "status_code": value.status_code}
    return value


async def _push_agent_suggestion() -> None:
    """周期推送旧版 Agent 建议，兼容现有前端面板。"""

    while True:
        await asyncio.sleep(5)
        if websocket_manager.active_connections <= 0:
            continue
        payload = await _json_payload(await get_agent_suggestion(room_id="dashboard-room"))
        await websocket_manager.broadcast({"type": "agent_suggestion", "payload": payload})


async def _push_harness_status() -> None:
    """周期推送最近一条 Harness 会话状态。

    当还没有启动 6C 会话时静默跳过，避免副屏一打开就被无意义错误刷屏。
    """

    while True:
        await asyncio.sleep(3)
        if websocket_manager.active_connections <= 0:
            continue
        try:
            payload = get_harness_dashboard_service().latest_for_room("room-dashboard-001")
        except Exception:
            continue
        await _broadcast_harness_status(payload)


async def _push_danmaku() -> None:
    """周期推送弹幕聚合摘要。"""

    while True:
        await asyncio.sleep(5)
        if websocket_manager.active_connections <= 0:
            continue
        payload = await _json_payload(await get_danmaku_summary(room_id="dashboard-room"))
        await websocket_manager.broadcast({"type": "danmaku_update", "payload": payload})


async def _push_alerts() -> None:
    """周期推送库存/售罄告警。"""

    while True:
        await asyncio.sleep(5)
        if websocket_manager.active_connections <= 0:
            continue
        payload = await _json_payload(await get_alerts(room_id="dashboard-room"))
        await websocket_manager.broadcast({"type": "alert_update", "payload": payload})


async def _push_review() -> None:
    """周期推送播后 LLM 复盘摘要。"""

    while True:
        await asyncio.sleep(10)
        if websocket_manager.active_connections <= 0:
            continue
        payload = await _json_payload(await get_llm_review(room_id="dashboard-room"))
        await websocket_manager.broadcast({"type": "review_update", "payload": payload})




@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动后台推送任务。"""
    tasks = [
        asyncio.create_task(_push_agent_suggestion()),
        asyncio.create_task(_push_harness_status()),
        asyncio.create_task(_push_danmaku()),
        asyncio.create_task(_push_alerts()),
        asyncio.create_task(_push_review()),
    ]
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app.router.lifespan_context = lifespan

front_dir = Path(__file__).resolve().parent.parent.parent / "front"
if front_dir.exists():
    app.mount("/", StaticFiles(directory=str(front_dir), html=True), name="static")
