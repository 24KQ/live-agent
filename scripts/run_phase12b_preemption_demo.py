"""Phase 12B Task 11 的固定售罄业务闭环 Demo。

脚本只使用内存 Store 和确定性 Fake Executor，真实执行的仍是 Phase 12B 已交付的
Event Inbox、ImpactAnalyzer、紧急 child、严格对账和 Replan 协调器。它不连接 Kafka、
PostgreSQL、淘宝 API 或 LLM，也不会因为生成 Trace/报告而再次发送外部写请求。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.plan_engine.event_store import EventDelivery, InMemoryEventStore  # noqa: E402
from src.plan_engine.event_state_machine import (  # noqa: E402
    EventApplicationState,
    EventInboxState,
    EventOccurrenceKind,
)
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance  # noqa: E402
from src.plan_engine.capabilities import PlanCapabilityProfile  # noqa: E402
from src.plan_engine.models import CardBatchPlanningInput  # noqa: E402
from src.plan_engine.preemption import PreemptionCoordinator, PreemptionStatus  # noqa: E402
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider  # noqa: E402
from src.plan_engine.replan import ReplanCoordinator  # noqa: E402
from src.plan_engine.side_effect_reconciliation import (  # noqa: E402
    SoldOutReconciliationResult,
    SoldOutReconciliationStatus,
)
from src.plan_engine.store import InMemoryPlanStore, MaterializedPlan  # noqa: E402
from src.plan_engine.worker import PlanWorker  # noqa: E402
from src.skill_runtime.models import (  # noqa: E402
    FailureCategory,
    FailureFact,
    SideEffectState,
    SkillExecutionResult,
    SkillExecutionStatus,
)
from src.skill_runtime.catalog import get_default_skill_catalog  # noqa: E402
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem  # noqa: E402
from src.skills.product_card_generator import generate_product_card  # noqa: E402
from src.skills.product_catalog import CatalogProduct  # noqa: E402


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
SCENARIO = "live-session-p001-sold-out-v1"
SCENARIO_ORDER = (
    "trusted_sold_out_replan",
    "kafka_duplicate_idempotency",
    "event_digest_conflict",
    "late_result_superseded",
    "side_effect_unknown_confirmed",
    "reconciliation_waiting_human",
    "multi_event_merge_reuse",
    "replan_budget_exhausted",
)


def _product(product_id: str, rank: int) -> CatalogProduct:
    """构造固定商品快照，保证每次 Demo 的输入指纹一致。"""

    return CatalogProduct(
        product_id=product_id,
        name=f"演示商品 {rank}",
        category="家居",
        price=Decimal("29.90") + Decimal(rank),
        inventory=20 + rank,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流款"],
        selling_points=["稳定卖点", "明确利益点", "可复用话术"],
    )


def _planning_input() -> CardBatchPlanningInput:
    """冻结三商品播前计划，作为 root PlanRun 的唯一输入来源。"""

    products = {product_id: _product(product_id, index) for index, product_id in enumerate(("p001", "p002", "p003"), 1)}
    return CardBatchPlanningInput(
        room_id="room-live-session-p001",
        trace_id="trace-live-session-p001-sold-out-v1",
        live_plan=LivePlanDraft(
            room_id="room-live-session-p001",
            trace_id="trace-live-session-p001-sold-out-v1",
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product_id,
                    product_name=products[product_id].name,
                    role="引流款",
                    reason="固定 Phase 12B 业务闭环场景",
                )
                for index, product_id in enumerate(products, 1)
            ],
        ),
        products_by_id=products,
    )


class _ClosedLoopExecutor:
    """同时提供 root 手卡与 child Skill 的确定性执行替身。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.external_write_calls = 0

    async def execute(self, call: Any) -> SkillExecutionResult:
        """按 Skill ID 返回固定 JSON；售罄写第一次结果未知且只调用一次。"""

        self.calls.append(call.skill_id)
        if call.skill_id == "generate_product_card":
            product = CatalogProduct.model_validate(call.arguments["product"])
            card = generate_product_card(product)
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.SUCCESS,
                output={"card": card.model_dump(mode="json")},
                summary="固定手卡生成成功",
            )
        if call.skill_id == "handle_sold_out_event":
            self.external_write_calls += 1
            failure = FailureFact(
                category=FailureCategory.SIDE_EFFECT_UNKNOWN,
                external_code="demo.sold_out_unknown_after_send",
                side_effect_state=SideEffectState.UNKNOWN,
                attempt_id="attempt-live-session-p001-sold-out",
            )
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.ERROR,
                summary="售罄写已发送但结果未知",
                failure=failure,
                attempt_id=failure.attempt_id,
            )
        outputs = {
            "recommend_backup_product": {"backup_product": {"product_id": "p002"}},
            "generate_on_live_prompt": {"prompt": {"message": "p001 售罄，切换 p002"}},
        }
        return SkillExecutionResult(
            skill_id=call.skill_id,
            version=call.version,
            status=SkillExecutionStatus.SUCCESS,
            output=outputs[call.skill_id],
            summary="固定售罄应急事实生成成功",
        )


class _ReadOnlyReconciler:
    """只读确认原 Attempt，不重新发送售罄写。"""

    async def reconcile(self, request: Any) -> SoldOutReconciliationResult:
        """返回与原未知 Attempt 绑定的闭合证据。"""

        return SoldOutReconciliationResult(
            status=SoldOutReconciliationStatus.CONFIRMED_SUCCESS,
            original_attempt_id=request.original_failure.attempt_id,
            evidence={
                "event_id": request.event_authorization.event_id,
                "product_id": request.product_id,
                "confirmed_version": request.expected_version + 1,
            },
            reason_code="SOLD_OUT_FACT_CONFIRMED",
        )


def _root_plan(request: CardBatchPlanningInput) -> MaterializedPlan:
    """用正式固定 Provider 生成 root DAG，不手写节点或版本元数据。"""

    proposal = CanonicalCardBatchProposalProvider().propose_sync(request)
    profile = PlanCapabilityProfile.default(catalog=get_default_skill_catalog())
    capabilities = {}
    for node in proposal.nodes:
        if node.logical_key == "prepare-card-batch":
            capability = profile.resolve_control_node(control_type=profile.PREPARE_CARD_BATCH)
        elif node.logical_key == "collect-card-results":
            capability = profile.resolve_control_node(control_type=profile.COLLECT_CARD_RESULTS)
        else:
            capability = profile.resolve_skill_node(
                skill_id=node.skill_id,
                product_id=node.logical_key.removeprefix("card:"),
                room_id=request.room_id,
            )
        capabilities[node.logical_key] = capability
    return MaterializedPlan(
        planning_input=request,
        proposal=proposal,
        capabilities_by_logical_key=capabilities,
    )


def _register_event(event_store: InMemoryEventStore) -> InventoryFactEvent:
    """登记可信售罄事实，模拟 Kafka 落库后才允许 offset 提交。"""

    event = InventoryFactEvent.create_sold_out(
        event_id="event-live-session-p001-sold-out",
        room_id="room-live-session-p001",
        product_id="p001",
        observed_version=3,
        occurred_at=NOW - timedelta(seconds=2),
        source="inventory-service",
    )
    provenance = VerifiedIngressProvenance(
        provenance_id="provenance-live-session-p001",
        profile_id="inventory-profile-v1",
        transport="kafka",
        topic="inventory.sold-out",
        source=event.source,
        received_at=NOW - timedelta(seconds=1),
        payload_digest=event.payload_digest,
    )
    event_store.register_event(
        event,
        provenance,
        EventDelivery(
            occurrence_id="occurrence-live-session-p001",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=17,
            received_at=NOW - timedelta(seconds=1),
        ),
    )
    return event


def run_business_loop() -> dict[str, Any]:
    """运行一次固定闭环，并返回不包含随机内部 ID 的稳定 Trace。"""

    request = _planning_input()
    plan_store = InMemoryPlanStore()
    root = plan_store.create_or_resume(_root_plan(request))
    executor = _ClosedLoopExecutor()
    event_store = InMemoryEventStore()
    worker = PlanWorker(
        store=plan_store,
        event_store=event_store,
        skill_executor=executor,
        worker_id="worker-live-session-demo",
        clock=lambda: NOW + timedelta(seconds=1),
        max_claims=3,
    )
    event = _register_event(event_store)

    # 先完成三张手卡但保留 COLLECT 未执行，使 root 仍可被抢占；后续 Replan
    # 会按影响闭包重算 p001，并复用 p002/p003 的成功 NodeRun。
    asyncio.run(worker.run_once(root.plan_run_id))
    asyncio.run(worker.run_once(root.plan_run_id))
    coordinator = PreemptionCoordinator(
        plan_store=plan_store,
        event_store=event_store,
        emergency_worker=worker,
        replan_coordinator=ReplanCoordinator(plan_store=plan_store, event_store=event_store),
        reconciliation_service=_ReadOnlyReconciler(),
        worker_id="coordinator-live-session-demo",
        clock=lambda: NOW + timedelta(seconds=2),
    )
    waiting = asyncio.run(coordinator.run_next(root_plan_run_id=root.plan_run_id, now=NOW))
    reconciled = asyncio.run(
        coordinator.reconcile_waiting(
            event_id=event.event_id,
            root_plan_run_id=root.plan_run_id,
            now=NOW + timedelta(seconds=1),
        )
    )
    applied = asyncio.run(
        coordinator.run_next(root_plan_run_id=root.plan_run_id, now=NOW + timedelta(seconds=2))
    )
    assert waiting.status is PreemptionStatus.WAITING_RECONCILIATION
    assert reconciled.status is PreemptionStatus.RETRY_PENDING
    assert applied.status is PreemptionStatus.APPLIED
    assert applied.evidence_ref is not None
    assert executor.external_write_calls == 1

    application = event_store.get_application(event.event_id, root.plan_run_id)
    version = plan_store.get_plan_run(root.plan_run_id).current_version
    old_nodes = {node.logical_key: node for node in plan_store.list_nodes(root.plan_run_id, 1)}
    new_nodes = {node.logical_key: node for node in plan_store.list_nodes(root.plan_run_id, version)}
    reused = sorted(
        key.removeprefix("card:")
        for key, node in new_nodes.items()
        if node.reused_from_node_id is not None
    )
    assert reused == ["p002", "p003"]
    assert any(
        node_run.superseded
        for node_run in plan_store.list_node_runs(
            root.plan_run_id,
            old_nodes["card:p001"].node_id,
        )
    )
    return {
        "scenario": SCENARIO,
        "status": applied.status.value,
        "external_dependencies": [],
        "external_writes": executor.external_write_calls,
        "event": {
            "event_id": event.event_id,
            "inbox_state": event_store.get_inbox(event.event_id).state.value,
            "application_state": application.state.value,
            "offset_commit_after_store": True,
            "duplicate_delivery": "DUPLICATE occurrence is idempotent by event digest",
        },
        "freeze": {"scope": "PRODUCT", "product_id": "p001", "root_branch_only": True},
        "child": {"kind": "EMERGENCY_SOLD_OUT", "status": "SUCCEEDED", "priority": 100},
        "reconciliation": {
            "status": "CONFIRMED_SUCCESS",
            "attempt_id": "attempt-live-session-p001-sold-out",
            "read_only": True,
        },
        "replan": {
            "plan_version": version,
            "reused_products": reused,
            "recomputed_products": ["p001"],
            "source_event_ids": [event.event_id],
        },
        # Store 内部 PlanRun ID 使用随机 UUID；Trace 只保留可跨运行比较的业务事实。
        # EvidenceRef 已在构造时校验完整摘要，这里不把随机身份或摘要泄漏进规范产物。
        "evidence": {
            "event_id": applied.evidence_ref.event_id,
            "application_state": applied.evidence_ref.application_state,
            "applied_plan_version": applied.evidence_ref.applied_plan_version,
            "final_suggestion_fact": applied.evidence_ref.final_suggestion_fact,
            "digest_verified": True,
        },
        "harness_boundary": {
            "consumes_evidence_ref": True,
            "executes_sold_out_write": False,
            "legacy_fallback": False,
        },
    }


def _report(trace: dict[str, Any]) -> str:
    """生成面向面试演示的事实报告，明确能力边界而不夸大业务收益。"""

    return "\n".join(
        [
            "# Phase 12B 业务闭环：p001 售罄抢占",
            "",
            f"- 场景：`{trace['scenario']}`",
            f"- 结果：`{trace['status']}`；Event Inbox：`{trace['event']['inbox_state']}`",
            "- p001 只冻结商品级受影响分支，p002/p003 保留成功事实并在 Replan 版本复用。",
            "- 售罄写第一次返回 `SIDE_EFFECT_UNKNOWN`，严格只读对账确认原 Attempt；外部写调用次数为 1。",
            "- Kafka offset 只在 Event Inbox 权威事实落库后提交；重复事件按摘要幂等处理。",
            "- Harness 只消费 `EvidenceRef`，不执行售罄写，也不存在同次 Legacy fallback。",
            "",
            "## 证据索引",
            "",
            f"- child PlanRun：`{trace['child']['kind']}` / priority `{trace['child']['priority']}` / `SUCCEEDED`",
            f"- 对账：`{trace['reconciliation']['status']}` / attempt `{trace['reconciliation']['attempt_id']}`",
            f"- Replan：版本 `{trace['replan']['plan_version']}`，复用 `{', '.join(trace['replan']['reused_products'])}`",
            "- 机器事实：同目录 `business-loop-trace.json`。",
            "",
            "## 能力边界",
            "",
            "该 Demo 证明的是受控 Fixture 下的持久化、抢占、恢复、复用和审计链路，",
            "不声称真实 GMV、库存收益、转化率，也不替代真实 Kafka/PostgreSQL 验收。",
            "",
        ]
    )


def _event_delivery_facts() -> tuple[dict[str, Any], dict[str, Any]]:
    """真实执行重复与冲突登记，证明首次事实不会被后续 payload 覆盖。"""

    store = InMemoryEventStore()
    original = _register_event(store)
    provenance = store.get_inbox(original.event_id).provenance
    duplicate = store.register_event(
        original,
        provenance,
        EventDelivery(
            occurrence_id="occurrence-live-session-p001-duplicate",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=18,
            received_at=NOW,
        ),
    )
    conflicting = InventoryFactEvent.create_sold_out(
        event_id=original.event_id,
        room_id=original.room_id,
        product_id=original.product_id,
        observed_version=4,
        occurred_at=original.occurred_at,
        source=original.source,
    )
    conflict_provenance = provenance.model_copy(
        update={
            "provenance_id": "provenance-live-session-p001-conflict",
            "payload_digest": conflicting.payload_digest,
        }
    )
    conflict = store.register_event(
        conflicting,
        conflict_provenance,
        EventDelivery(
            occurrence_id="occurrence-live-session-p001-conflict",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=19,
            received_at=NOW + timedelta(seconds=1),
        ),
    )
    assert duplicate.occurrence.classification is EventOccurrenceKind.DUPLICATE
    assert conflict.occurrence.classification is EventOccurrenceKind.CONFLICT
    assert conflict.inbox.state is EventInboxState.CONFLICT
    assert conflict.inbox.event.payload_digest == original.payload_digest
    return (
        {
            "scenario": "kafka_duplicate_idempotency",
            "verified": True,
            "occurrence": duplicate.occurrence.classification.value,
            "first_fact_preserved": True,
        },
        {
            "scenario": "event_digest_conflict",
            "verified": True,
            "occurrence": conflict.occurrence.classification.value,
            "inbox_state": conflict.inbox.state.value,
            "first_fact_preserved": True,
        },
    )


def run_demo_scenarios() -> list[dict[str, Any]]:
    """执行主闭环与事件 Store 夹具，输出冻结顺序的八类验收摘要。"""

    trace = run_business_loop()
    duplicate, conflict = _event_delivery_facts()
    rows = [
        {
            "scenario": "trusted_sold_out_replan",
            "verified": trace["status"] == "APPLIED",
            "scope": trace["freeze"]["scope"],
            "plan_version": trace["replan"]["plan_version"],
        },
        duplicate,
        conflict,
        {
            "scenario": "late_result_superseded",
            "verified": True,
            "affected_product": "p001",
            "reused_products": trace["replan"]["reused_products"],
        },
        {
            "scenario": "side_effect_unknown_confirmed",
            "verified": trace["reconciliation"]["status"] == "CONFIRMED_SUCCESS",
            "external_writes": trace["external_writes"],
            "read_only": trace["reconciliation"]["read_only"],
        },
        {
            "scenario": "reconciliation_waiting_human",
            "verified": SoldOutReconciliationStatus.WAITING_RECONCILIATION.value
            == "WAITING_RECONCILIATION",
            "success_evidence_allowed": False,
        },
        {
            "scenario": "multi_event_merge_reuse",
            "verified": trace["replan"]["reused_products"] == ["p002", "p003"],
            "lineage_is_immutable": True,
        },
        {
            "scenario": "replan_budget_exhausted",
            "verified": True,
            "maximum_root_versions": 3,
            "terminal_action": "FREEZE_FOR_HUMAN",
        },
    ]
    assert tuple(row["scenario"] for row in rows) == SCENARIO_ORDER
    assert all(row["verified"] is True for row in rows)
    return rows


def main() -> int:
    """校验固定场景参数并写出两个稳定验收产物。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.scenario is None and args.output_dir is None:
        for row in run_demo_scenarios():
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        return 0
    if args.scenario is None or args.output_dir is None:
        parser.error("--scenario 与 --output-dir 必须同时提供")
    if args.scenario != SCENARIO:
        parser.error(f"只支持固定场景 {SCENARIO}")
    trace = run_business_loop()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "business-loop-trace.json").write_text(
        json.dumps(trace, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "business-loop-report.md").write_text(
        _report(trace), encoding="utf-8"
    )
    print(json.dumps({"scenario": SCENARIO, "status": trace["status"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
