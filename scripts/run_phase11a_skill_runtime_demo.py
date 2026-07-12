# -*- coding: utf-8 -*-
"""Phase 11A Skill Runtime 路由迁移演示。

该脚本只使用固定商品快照与进程内审计 Store，不读取数据库连接、密钥或完整环境。
四个场景分别重新装配不可变 ``RoutePolicy``，展示 generation 前移、全量前移以及
setup 从全 Runtime 明确回滚到 Legacy 的过程。
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
import sys
from typing import Any

# 直接执行 ``python scripts/<name>.py`` 时，解释器默认只加入 scripts 目录；这里在
# 导入项目包前加入解析后的仓库根目录，使直接入口与 run_all 子进程入口行为一致。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audit.tool_call_audit import AuditEvent
from src.core.human_approval import (
    HumanApprovalDecision,
    HumanApprovalRequest,
    HumanApprovalResponse,
    validate_human_approval_response,
)
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
from src.skill_runtime.models import _build_human_interrupt_approval
from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService
from src.skill_runtime.pre_live_handlers import build_pre_live_handlers
from src.skill_runtime.routing import RouteConfig, RoutePolicy
from src.skills.product_catalog import CatalogProduct
from src.state.models import RiskLevel


class _FixedCatalogRepository:
    """仅返回脚本内固定商品快照的 Repository，彻底隔离 PostgreSQL。"""

    def __init__(self, products: tuple[CatalogProduct, ...]) -> None:
        self._products = products

    def list_room_products(self, room_id: str) -> list[CatalogProduct]:
        """返回列表副本；room_id 仅遵循生产 Repository 的查询接口。"""
        del room_id
        return list(self._products)


class _InMemoryAuditStore:
    """模拟 ``ToolCallAuditStore`` 行结构和幂等写入语义的内存实现。

    setup 审批链会先按 trace 查询 pending/resume 事件，再通过请求载荷中的
    ``idempotency_key`` 去重；因此这里保留生产服务实际读取的全部字段。
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def record_event(self, event: AuditEvent) -> str:
        """写入事件；同一工具和非空幂等键重复调用时返回首次 audit_id。"""
        idempotency_key = event.idempotency_key or event.request_payload.get("idempotency_key")
        if idempotency_key is not None:
            for existing in self._events:
                if existing["tool_name"] == event.tool_name and existing["idempotency_key"] == idempotency_key:
                    return existing["audit_id"]

        audit_id = f"audit-memory-{len(self._events) + 1}"
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
        """按写入顺序返回指定 trace 的审计行副本。"""
        return [dict(event) for event in self._events if event["trace_id"] == trace_id]


def _fixed_products() -> tuple[CatalogProduct, ...]:
    """构造四类角色均可稳定出现的固定演示货盘。"""
    return (
        CatalogProduct(
            product_id="demo-traffic",
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
            product_id="demo-profit",
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
            product_id="demo-atmosphere",
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
            product_id="demo-regular",
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


def _assemble_service(
    policy: RoutePolicy,
) -> tuple[RoutedPreLiveBusinessService, _InMemoryAuditStore]:
    """为单个场景装配独立内存依赖和显式实例级 Runtime Handler。

    即使当前 policy 全走 Legacy，仍显式构造 Executor，使四个场景的装配形状一致；
    Handler 始终绑定当前内存业务服务，不会触发默认 Settings 或 PostgreSQL。
    """
    repository = _FixedCatalogRepository(_fixed_products())
    audit_store = _InMemoryAuditStore()
    business_service = PreLiveBusinessFlowService(repository, audit_store)  # type: ignore[arg-type]
    executor = SyncSkillExecutorAdapter(
        SkillExecutor(handlers=build_pre_live_handlers(business_service))
    )
    return (
        RoutedPreLiveBusinessService(
            policy=policy,
            legacy_service=business_service,
            skill_executor=executor,
        ),
        audit_store,
    )


def _run_scenario(name: str, policy: RoutePolicy) -> dict[str, Any]:
    """执行一条完整播前链，并返回不含密钥和环境信息的固定摘要。"""
    room_id = f"room-{name}"
    trace_id = f"trace-{name}"
    service, audit_store = _assemble_service(policy)

    products = service.query_products(room_id, trace_id)
    plan = service.generate_plan(room_id, products, trace_id)
    cards = service.generate_cards(room_id, plan, products, trace_id)

    # 先记录 pending，再校验恢复响应并记录 resume；resume audit_id 是 Runtime 信任的证据。
    approval_request = HumanApprovalRequest(
        trace_id=trace_id,
        room_id=room_id,
        tool_name="setup_live_session",
        risk_level=RiskLevel.HIGH,
        action="confirm_setup_live_session",
        plan_item_ids=[item.product_id for item in plan.items],
        message="确认按固定演示排品方案模拟建播。",
    )
    service.record_setup_approval_event(approval_request, None)
    approval_response = validate_human_approval_response(
        approval_request,
        HumanApprovalResponse(
            trace_id=trace_id,
            room_id=room_id,
            tool_name="setup_live_session",
            decision=HumanApprovalDecision.APPROVED,
            operator_id="phase11a-demo-operator",
            reason="批准固定内存演示建播。",
        ),
    )
    resume_audit_id = service.record_setup_approval_event(approval_request, approval_response)
    setup_idempotency_key = f"{trace_id}:setup:approved"
    gate, setup_audit_id = service.setup_live_session(
        room_id=room_id,
        plan=plan,
        trace_id=trace_id,
        confirmed_setup=True,
        idempotency_key=setup_idempotency_key,
        approval_context=_build_human_interrupt_approval(
            decision="APPROVED",
            operator_id=approval_response.operator_id,
            approval_audit_id=resume_audit_id,
        ),
    )
    audit_count_after_setup = len(audit_store.list_events_by_trace_id(trace_id))

    # 使用完全相同的可信审批证据和幂等键重放 setup。Legacy 与 Runtime 路由都必须
    # 返回首次 audit_id，保持 gate 已允许，并且不能向审计链追加第二条业务副作用。
    replay_gate, replay_audit_id = service.setup_live_session(
        room_id=room_id,
        plan=plan,
        trace_id=trace_id,
        confirmed_setup=True,
        idempotency_key=setup_idempotency_key,
        approval_context=_build_human_interrupt_approval(
            decision="APPROVED",
            operator_id=approval_response.operator_id,
            approval_audit_id=resume_audit_id,
        ),
    )
    audit_count_after_replay = len(audit_store.list_events_by_trace_id(trace_id))
    assert gate.allowed is True, f"{name}: 首次 setup 未通过安全门禁"
    assert replay_gate.allowed is True, f"{name}: 幂等重放 setup 未保持允许状态"
    assert setup_audit_id is not None, f"{name}: 首次 setup 缺少审计 ID"
    assert replay_audit_id == setup_audit_id, f"{name}: 幂等重放未复用首次审计 ID"
    assert audit_count_after_replay == audit_count_after_setup, f"{name}: 幂等重放增加了审计事件"

    return {
        "scenario": name,
        "generation_route": policy.generation.value,
        "setup_route": policy.setup.value,
        "product_count": len(products),
        "plan_item_count": len(plan.items),
        "card_count": len(cards),
        "setup_status": "prepared" if replay_gate.allowed else "pending_confirmation",
        "audit_count": audit_count_after_replay,
    }


def _format_summary(summary: dict[str, Any]) -> str:
    """按验收字段固定顺序格式化单行摘要，便于人工阅读和脚本采集。"""
    keys = (
        "scenario",
        "generation_route",
        "setup_route",
        "product_count",
        "plan_item_count",
        "card_count",
        "setup_status",
        "audit_count",
    )
    return " ".join(f"{key}={summary[key]}" for key in keys)


def run_demo_scenarios(*, emit: bool = True) -> list[dict[str, Any]]:
    """按迁移时间线重新装配并执行四个不可变路由策略。

    最后一个策略在 ``all_runtime`` 之后重新创建，明确表达仅将 setup 回滚到 Legacy；
    任何已有 ``RoutePolicy`` 对象都不会被修改。
    """
    scenarios = (
        ("all_legacy", RoutePolicy(generation=RouteConfig.LEGACY, setup=RouteConfig.LEGACY)),
        (
            "generation_runtime_setup_legacy",
            RoutePolicy(generation=RouteConfig.SKILL_RUNTIME, setup=RouteConfig.LEGACY),
        ),
        (
            "all_runtime",
            RoutePolicy(generation=RouteConfig.SKILL_RUNTIME, setup=RouteConfig.SKILL_RUNTIME),
        ),
        (
            "setup_rollback_to_legacy",
            RoutePolicy(generation=RouteConfig.SKILL_RUNTIME, setup=RouteConfig.LEGACY),
        ),
    )
    summaries = [_run_scenario(name, policy) for name, policy in scenarios]
    if emit:
        for summary in summaries:
            print(_format_summary(summary))
    return summaries


def main() -> int:
    """运行演示并以进程退出码零表示四个场景全部完成。"""
    run_demo_scenarios()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
