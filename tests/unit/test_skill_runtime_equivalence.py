# -*- coding: utf-8 -*-
"""Phase 11A Skill Runtime 隔离等价性与无外部依赖 Demo 测试。

本模块刻意在测试边界内定义 Repository 与审计 Store 替身，确保测试不会读取
PostgreSQL、环境变量或生产默认 Handler。Legacy 与 Runtime 使用内容相同的冻结货盘，
但它们的 Repository、业务服务和 Store 均为独立对象，从而能识别依赖串线和审计污染。
"""

from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from src.audit import tool_call_audit
from src.audit.tool_call_audit import AuditEvent
from src.core.security_hooks import GateDecision
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService
from src.skill_runtime.pre_live_handlers import build_pre_live_handlers
from src.skill_runtime.routing import RouteConfig, RoutePolicy
from src.skills.product_catalog import CatalogProduct
from src.state.models import ActionType, RiskLevel


class FakeRepository:
    """返回构造时冻结商品快照的内存 Repository。

    Repository 在构造和查询边界都复制完整模型，确保可变的标签、卖点等嵌套容器
    不会跨执行栈或跨查询共享；两套栈只共享值相同这一事实，不共享任何商品对象。
    """

    def __init__(self, products: tuple[CatalogProduct, ...]) -> None:
        # 构造时先切断调用方快照与 Repository 内部快照的对象关系，避免来源对象随后
        # 修改 tags/selling_points 时改变 Repository 已接收的测试夹具。
        self._products = tuple(self._clone_product(product) for product in products)
        self.queried_room_ids: list[str] = []

    def list_room_products(self, room_id: str) -> list[CatalogProduct]:
        """记录查询房间，并为本次调用重建不共享嵌套容器的商品快照。"""
        self.queried_room_ids.append(room_id)
        return [self._clone_product(product) for product in self._products]

    @staticmethod
    def _clone_product(product: CatalogProduct) -> CatalogProduct:
        """经 JSON 领域快照重新校验模型，实现商品及其嵌套列表的真正深复制。"""
        return CatalogProduct.model_validate(product.model_dump(mode="json"))


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
        """以生产字段形状保存事件，并按完整业务事实判定幂等重放。"""
        idempotency_key = event.idempotency_key or event.request_payload.get("idempotency_key")
        if idempotency_key is not None:
            for existing in self._events:
                if existing["tool_name"] == event.tool_name and existing["idempotency_key"] == idempotency_key:
                    # 比较器必须覆盖作用域、门禁、请求和结果等全部事实；仅凭工具名和
                    # 幂等键复用会掩盖迁移前后对异事实调用的处理差异。
                    if tool_call_audit._event_matches_stored_fact(
                        event,
                        existing,
                        effective_idempotency_key=idempotency_key,
                    ):
                        return existing["audit_id"]
                    raise tool_call_audit.IdempotencyConflictError(
                        "conflicting audit idempotency replay"
                    )

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


def test_equivalence_store_rejects_same_key_with_different_audit_fact() -> None:
    """测试比较器不得把同工具同键但 trace 或载荷不同的事件当作等价重放。"""

    store = InMemoryAuditStore("semantic-conflict")
    original = AuditEvent(
        trace_id="trace-equivalence-original",
        room_id="room-equivalence",
        tool_name="setup_live_session",
        action_type=ActionType.SETUP_LIVE_SESSION,
        risk_level=RiskLevel.HIGH,
        gate_decision=GateDecision.HARD_GATE,
        operator_decision="approved",
        idempotency_key="idem-equivalence-conflict",
        request_payload={"plan_item_ids": ["p001"]},
        result_payload={"status": "prepared"},
    )
    store.record_event(original)

    with pytest.raises(tool_call_audit.IdempotencyConflictError):
        store.record_event(
            replace(
                original,
                trace_id="trace-equivalence-conflicting",
                request_payload={"plan_item_ids": ["p002"]},
            )
        )


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


def test_fake_repositories_deeply_isolate_nested_product_snapshots() -> None:
    """任一 Repository 返回值的嵌套列表变更不得污染来源、另一栈或后续查询。"""
    source_products = _fixed_products()
    legacy_repository = FakeRepository(source_products)
    runtime_repository = FakeRepository(source_products)

    legacy_first_read = legacy_repository.list_room_products("room-isolation")
    runtime_first_read = runtime_repository.list_room_products("room-isolation")

    assert legacy_first_read[0] is not runtime_first_read[0]
    assert legacy_first_read[0].tags is not runtime_first_read[0].tags
    assert legacy_first_read[0].selling_points is not runtime_first_read[0].selling_points

    legacy_first_read[0].tags.append("legacy-only")
    legacy_first_read[0].selling_points.append("legacy-only-point")

    assert "legacy-only" not in source_products[0].tags
    assert "legacy-only" not in runtime_first_read[0].tags
    assert "legacy-only" not in legacy_repository.list_room_products("room-isolation")[0].tags
    assert "legacy-only-point" not in source_products[0].selling_points
    assert "legacy-only-point" not in runtime_first_read[0].selling_points


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


def test_all_runtime_demo_replays_setup_with_the_same_audit(monkeypatch: Any) -> None:
    """全 Runtime 场景必须把同一幂等键传入 Facade，并复用首次 setup 审计。"""
    from scripts.run_phase11a_skill_runtime_demo import _run_scenario

    original_setup = RoutedPreLiveBusinessService.setup_live_session
    setup_calls: list[dict[str, Any]] = []

    def recording_setup(self: RoutedPreLiveBusinessService, *args: Any, **kwargs: Any) -> Any:
        """调用真实 Facade 后记录输入幂等键和返回值，不替换 Runtime 或 Store 行为。"""
        result = original_setup(self, *args, **kwargs)
        setup_calls.append({"idempotency_key": kwargs.get("idempotency_key"), "result": result})
        return result

    monkeypatch.setattr(RoutedPreLiveBusinessService, "setup_live_session", recording_setup)
    summary = _run_scenario(
        "all_runtime",
        RoutePolicy(generation=RouteConfig.SKILL_RUNTIME, setup=RouteConfig.SKILL_RUNTIME),
    )

    assert len(setup_calls) == 2
    assert setup_calls[0]["idempotency_key"] is not None
    assert setup_calls[0]["idempotency_key"] == setup_calls[1]["idempotency_key"]
    first_gate, first_audit_id = setup_calls[0]["result"]
    replay_gate, replay_audit_id = setup_calls[1]["result"]
    assert first_gate.allowed is True
    assert replay_gate.allowed is True
    assert first_audit_id is not None
    assert replay_audit_id == first_audit_id
    assert summary["audit_count"] == 8


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
