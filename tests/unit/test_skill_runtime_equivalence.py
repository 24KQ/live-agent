# -*- coding: utf-8 -*-
"""Phase 11A Skill Runtime 隔离等价性与无外部依赖 Demo 测试。

本模块刻意在测试边界内定义 Repository 与审计 Store 替身，确保测试不会读取
PostgreSQL、环境变量或生产默认 Handler。Legacy 与 Runtime 使用内容相同的冻结货盘，
但它们的 Repository、业务服务和 Store 均为独立对象，从而能识别依赖串线和审计污染。
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from typing import Any

from src.audit.tool_call_audit import AuditEvent
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService
from src.skill_runtime.pre_live_handlers import build_pre_live_handlers
from src.skill_runtime.routing import RouteConfig, RoutePolicy
from src.skills.product_catalog import CatalogProduct


class FakeRepository:
    """返回构造时冻结商品快照的内存 Repository。

    每次查询都返回新的列表，避免调用方修改列表结构时反向污染另一套执行栈；其中的
    ``CatalogProduct`` 本身是不可变 Pydantic 模型，可以安全共享同一份业务快照内容。
    """

    def __init__(self, products: tuple[CatalogProduct, ...]) -> None:
        self._products = products
        self.queried_room_ids: list[str] = []

    def list_room_products(self, room_id: str) -> list[CatalogProduct]:
        """记录查询房间并返回固定商品快照的列表副本。"""
        self.queried_room_ids.append(room_id)
        return list(self._products)


class InMemoryAuditStore:
    """保留生产审计行形状与幂等查询语义的内存 Store。

    生产服务会通过 ``list_events_by_trace_id`` 按工具名和请求载荷中的幂等键查重；
    此处同时识别 ``AuditEvent.idempotency_key`` 与旧载荷键，并按写入顺序返回事件。
    Store 标签进入 audit_id，仅用于明确证明两套审计链互不共享，不参与语义比较。
    """

    def __init__(self, label: str) -> None:
        self._label = label
        self._events: list[dict[str, Any]] = []

    def record_event(self, event: AuditEvent) -> str:
        """以生产字段名保存事件；相同工具与非空幂等键复用首次 audit_id。"""
        idempotency_key = event.idempotency_key or event.request_payload.get("idempotency_key")
        if idempotency_key is not None:
            for existing in self._events:
                if existing["tool_name"] == event.tool_name and existing["idempotency_key"] == idempotency_key:
                    return existing["audit_id"]

        audit_id = f"audit-{self._label}-{len(self._events) + 1}"
        row = asdict(event)
        row.update(
            {
                "audit_id": audit_id,
                "action_type": event.action_type.value,
                "risk_level": event.risk_level.value,
                "gate_decision": event.gate_decision.value,
                "idempotency_key": idempotency_key,
                "request_payload": dict(event.request_payload),
                "result_payload": dict(event.result_payload),
            }
        )
        self._events.append(row)
        return audit_id

    def list_events_by_trace_id(self, trace_id: str) -> list[dict[str, Any]]:
        """按写入顺序返回指定 trace 的行副本，保持调用方查询不污染 Store。"""
        return [dict(event) for event in self._events if event["trace_id"] == trace_id]


def _fixed_products() -> tuple[CatalogProduct, ...]:
    """构造覆盖引流、利润、氛围和常规角色的确定性商品快照。"""
    return (
        CatalogProduct(
            product_id="product-traffic",
            name="轻盈随行杯",
            category="家居",
            price=Decimal("29.90"),
            inventory=40,
            conversion_rate=Decimal("0.22"),
            commission_rate=Decimal("0.08"),
            tags=["引流"],
            selling_points=["杯身轻便", "密封易携带"],
        ),
        CatalogProduct(
            product_id="product-profit",
            name="多功能料理锅",
            category="厨电",
            price=Decimal("299.00"),
            inventory=30,
            conversion_rate=Decimal("0.12"),
            commission_rate=Decimal("0.30"),
            tags=["利润"],
            selling_points=["一锅多用", "温控清晰", "易于清洁"],
        ),
        CatalogProduct(
            product_id="product-atmosphere",
            name="家庭分享装纸巾",
            category="日用",
            price=Decimal("69.00"),
            inventory=90,
            conversion_rate=Decimal("0.10"),
            commission_rate=Decimal("0.10"),
            tags=["氛围"],
            selling_points=["家庭分享装"],
        ),
        CatalogProduct(
            product_id="product-regular",
            name="桌面收纳盒",
            category="家居",
            price=Decimal("89.00"),
            inventory=20,
            conversion_rate=Decimal("0.08"),
            commission_rate=Decimal("0.12"),
            tags=["常规"],
            selling_points=["分区收纳", "桌面适配"],
        ),
    )


def _run_generation(service: Any, room_id: str, trace_id: str) -> tuple[list[Any], Any, list[Any]]:
    """按迁移边界依次执行查询、排品和前三张手卡，不触发 setup 路由。"""
    products = service.query_products(room_id, trace_id)
    plan = service.generate_plan(room_id, products, trace_id)
    cards = service.generate_cards(room_id, plan, products, trace_id)
    return products, plan, cards


def _normalize_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去除随机 audit_id，仅比较迁移必须保持的完整审计业务语义。"""
    compared_fields = (
        "trace_id",
        "room_id",
        "tool_name",
        "action_type",
        "risk_level",
        "gate_decision",
        "operator_decision",
        "idempotency_key",
        "request_payload",
        "result_payload",
    )
    return [{field: event[field] for field in compared_fields} for event in events]


def test_legacy_and_runtime_generation_are_equivalent_and_isolated() -> None:
    """两套独立依赖执行相同快照时，领域结果与规范化审计必须完全等价。"""
    room_id = "room-phase11a-equivalence"
    trace_id = "trace-phase11a-equivalence"
    products_snapshot = _fixed_products()

    legacy_repository = FakeRepository(products_snapshot)
    runtime_repository = FakeRepository(products_snapshot)
    legacy_store = InMemoryAuditStore("legacy")
    runtime_store = InMemoryAuditStore("runtime")
    legacy_service = PreLiveBusinessFlowService(legacy_repository, legacy_store)  # type: ignore[arg-type]
    runtime_business_service = PreLiveBusinessFlowService(runtime_repository, runtime_store)  # type: ignore[arg-type]

    # Runtime 必须注入当前栈的业务服务，禁止走会从 Settings 创建 PostgreSQL 的默认 Handler。
    runtime_executor = SyncSkillExecutorAdapter(
        SkillExecutor(handlers=build_pre_live_handlers(runtime_business_service))
    )
    runtime_service = RoutedPreLiveBusinessService(
        policy=RoutePolicy(generation=RouteConfig.SKILL_RUNTIME, setup=RouteConfig.LEGACY),
        legacy_service=runtime_business_service,
        skill_executor=runtime_executor,
    )

    legacy_products, legacy_plan, legacy_cards = _run_generation(legacy_service, room_id, trace_id)
    runtime_products, runtime_plan, runtime_cards = _run_generation(runtime_service, room_id, trace_id)

    assert legacy_repository is not runtime_repository
    assert legacy_store is not runtime_store
    assert legacy_service is not runtime_business_service
    assert legacy_products == runtime_products
    assert [item.product_id for item in legacy_plan.items] == [item.product_id for item in runtime_plan.items]
    assert [card.model_dump(mode="json") for card in legacy_cards] == [
        card.model_dump(mode="json") for card in runtime_cards
    ]

    legacy_events = legacy_store.list_events_by_trace_id(trace_id)
    runtime_events = runtime_store.list_events_by_trace_id(trace_id)
    assert _normalize_events(legacy_events) == _normalize_events(runtime_events)
    assert all(event["audit_id"].startswith("audit-legacy-") for event in legacy_events)
    assert all(event["audit_id"].startswith("audit-runtime-") for event in runtime_events)
    assert len(legacy_events) == len(runtime_events) == 5


def test_demo_exposes_four_reassembled_route_scenarios() -> None:
    """无外部依赖 Demo 必须按迁移顺序返回四个完整场景摘要。"""
    from scripts.run_phase11a_skill_runtime_demo import run_demo_scenarios

    summaries = run_demo_scenarios(emit=False)
    assert [summary["scenario"] for summary in summaries] == [
        "all_legacy",
        "generation_runtime_setup_legacy",
        "all_runtime",
        "setup_rollback_to_legacy",
    ]
    assert [(summary["generation_route"], summary["setup_route"]) for summary in summaries] == [
        ("LEGACY", "LEGACY"),
        ("SKILL_RUNTIME", "LEGACY"),
        ("SKILL_RUNTIME", "SKILL_RUNTIME"),
        ("SKILL_RUNTIME", "LEGACY"),
    ]
    assert all(summary["product_count"] == 4 for summary in summaries)
    assert all(summary["plan_item_count"] == 4 for summary in summaries)
    assert all(summary["card_count"] == 3 for summary in summaries)
    assert all(summary["setup_status"] == "prepared" for summary in summaries)
    assert all(summary["audit_count"] == 8 for summary in summaries)


def test_demo_script_runs_directly_without_external_services() -> None:
    """直接脚本入口必须能定位项目包，并且只输出四条场景摘要。"""
    project_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(project_root / "scripts" / "run_phase11a_skill_runtime_demo.py")],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output_lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(output_lines) == 4
    assert output_lines[0].startswith("scenario=all_legacy ")
    assert output_lines[-1].startswith("scenario=setup_rollback_to_legacy ")
