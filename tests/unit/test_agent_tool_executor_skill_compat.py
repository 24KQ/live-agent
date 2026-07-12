"""Phase 11A AgentToolExecutor 与 Skill Runtime 兼容收敛测试。

本文件只验证旧 Agent 工具入口到统一 Skill Runtime 的兼容边界：旧参数必须先
转换成完整、冻结的领域快照，四个核心工具只能调用一次同步适配器，未迁移工具
仍保留 legacy 派发。测试使用记录型替身隔离数据库，避免把兼容行为和外部环境
可用性混在一起。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from src.config.tool_registry import get_default_tool_registry
from src.core.agent_tool_executor import AgentToolExecutor
from src.skill_runtime.compatibility import CompatibilityArgumentNormalizer
from src.skill_runtime.models import (
    SkillExecutionResult,
    SkillExecutionStatus,
    SkillErrorCode,
)
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


def _product(product_id: str, name: str) -> CatalogProduct:
    """构造字段完整的冻结商品，确保测试会发现兼容层遗漏任何快照字段。"""
    return CatalogProduct(
        product_id=product_id,
        name=name,
        category="日用",
        price=Decimal("39.90"),
        inventory=100,
        conversion_rate=Decimal("0.15"),
        commission_rate=Decimal("0.05"),
        tags=["引流"],
        selling_points=["耐用", "易清洁"],
        is_active=True,
    )


class RecordingService:
    """记录兼容补全所需的只读货盘和排品调用，不执行真实数据库访问。"""

    def __init__(self) -> None:
        self.products = [_product("p001", "测试商品A"), _product("p002", "测试商品B")]
        self.calls: list[tuple[Any, ...]] = []

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """返回固定货盘，并记录兼容层是否为旧 ID 参数补全了快照。"""
        self.calls.append(("query_products", room_id, trace_id))
        return self.products

    def generate_plan(
        self,
        room_id: str,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> LivePlanDraft:
        """生成包含真实字段的计划草案，供 plan_item_ids 转换测试使用。"""
        self.calls.append(("generate_plan", room_id, trace_id))
        return LivePlanDraft(
            room_id=room_id,
            trace_id=trace_id,
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product.product_id,
                    product_name=product.name,
                    role="引流款",
                    reason=f"兼容计划-{product.product_id}",
                )
                for index, product in enumerate(products, start=1)
            ],
        )


class RecordingSkillExecutor:
    """记录每个 SkillCall，并返回可配置的结构化 Runtime 结果。"""

    def __init__(
        self,
        *,
        status: SkillExecutionStatus = SkillExecutionStatus.SUCCESS,
        error_code: SkillErrorCode | None = None,
        summary: str = "runtime summary",
        audit_id: str | None = "audit-runtime-001",
    ) -> None:
        self.calls: list[Any] = []
        self.status = status
        self.error_code = error_code
        self.summary = summary
        self.audit_id = audit_id

    def execute(self, call: Any) -> SkillExecutionResult:
        """严格记录一次调用；输出内容不参与本层 Observation 映射。"""
        self.calls.append(call)
        return SkillExecutionResult(
            skill_id=call.skill_id,
            version=call.version,
            status=self.status,
            error_code=self.error_code,
            output={} if self.status == SkillExecutionStatus.SUCCESS else None,
            summary=self.summary,
            audit_id=self.audit_id,
        )


class RaisingSkillExecutor:
    """模拟携带敏感文本的 Runtime 异常，验证失败边界不会泄露内部数据。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def execute(self, call: Any) -> SkillExecutionResult:
        self.calls.append(call)
        raise RuntimeError("db password=TOP_SECRET product_id=p-secret")


class RaisingService(RecordingService):
    """模拟隐藏货盘服务以 ValueError 失败，验证异常来源不会被误判为输入错误。"""

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """服务异常携带敏感文本，兼容入口只能返回固定执行失败摘要。"""
        raise ValueError("service token=TOP_SECRET-service")


def test_normalizer_moves_identifiers_and_builds_complete_immutable_products() -> None:
    """旧标识进入 context，商品对象转换成字段完整且不可修改的 JSON 快照。"""
    service = RecordingService()
    call = CompatibilityArgumentNormalizer(service).normalize(
        tool_name="generate_live_plan",
        arguments={
            "room_id": "untrusted-room",
            "trace_id": "untrusted-trace",
            "products": service.products,
        },
        room_id="room-001",
        trace_id="trace-001",
        lifecycle="PRE_LIVE",
    )

    assert call.context.room_id == "room-001"
    assert call.context.trace_id == "trace-001"
    assert call.context.compatibility_enriched is True
    assert call.context.model_dump(mode="json")["compatibility_enriched"] is True
    assert set(call.arguments) == {"products"}
    assert call.arguments["products"][0] == service.products[0].model_dump(mode="json")
    assert set(call.arguments["products"][0]) == set(CatalogProduct.model_fields)
    with pytest.raises(TypeError):
        call.arguments["products"][0]["name"] = "篡改"


def test_normalizer_resolves_product_id_to_single_product_snapshot() -> None:
    """旧 product_id 必须解析成目标商品的完整快照，而不是把整个货盘传入 Handler。"""
    service = RecordingService()
    call = CompatibilityArgumentNormalizer(service).normalize(
        tool_name="generate_product_card",
        arguments={"product_id": "p002"},
        room_id="room-002",
        trace_id="trace-002",
        lifecycle="PRE_LIVE",
    )

    assert call.arguments == {"product": service.products[1].model_dump(mode="json")}
    assert service.calls == [("query_products", "room-002", "trace-002")]
    assert call.context.compatibility_enriched is True


def test_normalizer_builds_setup_plan_and_moves_idempotency_key() -> None:
    """setup 的旧商品 ID 列表转换为真实计划，幂等键只能存在于可信 context。"""
    service = RecordingService()
    call = CompatibilityArgumentNormalizer(service).normalize(
        tool_name="setup_live_session",
        arguments={
            "room_id": "untrusted-room",
            "trace_id": "untrusted-trace",
            "plan_item_ids": ["p002", "p001"],
            "idempotency_key": "idem-setup-001",
            "confirmed_setup": True,
        },
        room_id="room-003",
        trace_id="trace-003",
        lifecycle="PRE_LIVE",
    )

    plan = LivePlanDraft.model_validate(call.arguments["plan"])
    assert [item.product_id for item in plan.items] == ["p002", "p001"]
    assert [item.rank for item in plan.items] == [1, 2]
    assert plan.room_id == "room-003"
    assert plan.trace_id == "trace-003"
    assert call.context.idempotency_key == "idem-setup-001"
    assert call.context.approval is None
    assert call.context.compatibility_enriched is True
    assert set(call.arguments) == {"plan"}


def _explicit_plan(room_id: str, trace_id: str) -> LivePlanDraft:
    """构造调用方显式提供的完整计划，用于测试可信上下文绑定规则。"""
    return LivePlanDraft(
        room_id=room_id,
        trace_id=trace_id,
        items=[
            LivePlanItem(
                rank=1,
                product_id="p001",
                product_name="测试商品A",
                role="引流款",
                reason="显式计划",
            )
        ],
    )


def test_normalizer_rejects_explicit_plan_with_room_mismatch_without_echo() -> None:
    """完整计划的 room_id 与可信参数不一致时必须拒绝，且不得回显攻击值。"""
    attacker_room = "attacker-room-TOP_SECRET"

    with pytest.raises(ValueError) as captured:
        CompatibilityArgumentNormalizer(RecordingService()).normalize(
            tool_name="setup_live_session",
            arguments={
                "plan": _explicit_plan(attacker_room, "trace-trusted"),
                "idempotency_key": "idem-room-mismatch",
            },
            room_id="room-trusted",
            trace_id="trace-trusted",
            lifecycle="PRE_LIVE",
        )

    assert str(captured.value) == "计划快照与可信执行上下文不一致"
    assert attacker_room not in str(captured.value)


def test_normalizer_rejects_explicit_plan_with_trace_mismatch_without_echo() -> None:
    """完整计划的 trace_id 与可信参数不一致时必须拒绝，且不得回显攻击值。"""
    attacker_trace = "attacker-trace-TOP_SECRET"

    with pytest.raises(ValueError) as captured:
        CompatibilityArgumentNormalizer(RecordingService()).normalize(
            tool_name="setup_live_session",
            arguments={
                "plan": _explicit_plan("room-trusted", attacker_trace),
                "idempotency_key": "idem-trace-mismatch",
            },
            room_id="room-trusted",
            trace_id="trace-trusted",
            lifecycle="PRE_LIVE",
        )

    assert str(captured.value) == "计划快照与可信执行上下文不一致"
    assert attacker_trace not in str(captured.value)


def test_normalizer_accepts_explicit_plan_matching_trusted_context() -> None:
    """room_id 与 trace_id 均匹配时保留调用方快照，不静默重写计划内容。"""
    plan = _explicit_plan("room-trusted", "trace-trusted")

    call = CompatibilityArgumentNormalizer(RecordingService()).normalize(
        tool_name="setup_live_session",
        arguments={"plan": plan, "idempotency_key": "idem-plan-match"},
        room_id="room-trusted",
        trace_id="trace-trusted",
        lifecycle="PRE_LIVE",
    )

    assert call.arguments["plan"] == plan.model_dump(mode="json")


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("query_products", {"room_id": "legacy-room"}),
        ("generate_live_plan", {"products": [_product("p001", "测试商品A")]}),
        ("generate_product_card", {"product_id": "p001"}),
        (
            "setup_live_session",
            {"plan_item_ids": ["p001"], "idempotency_key": "idem-core-001"},
        ),
    ],
)
def test_each_core_tool_calls_sync_skill_executor_exactly_once(
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """四个核心工具各自产生且只产生一个 Runtime 调用，禁止维护第二套 dispatch。"""
    service = RecordingService()
    runtime = RecordingSkillExecutor(
        status=(
            SkillExecutionStatus.PENDING
            if tool_name == "setup_live_session"
            else SkillExecutionStatus.SUCCESS
        ),
        error_code=(
            SkillErrorCode.APPROVAL_REQUIRED
            if tool_name == "setup_live_session"
            else None
        ),
    )
    executor = AgentToolExecutor(
        registry=get_default_tool_registry(),
        pre_live_service=service,
        skill_executor=runtime,
    )

    observation = executor.execute(tool_name, arguments, "room-core", "trace-core")

    assert observation.status in {"success", "pending"}
    assert len(runtime.calls) == 1
    assert runtime.calls[0].skill_id == tool_name
    assert runtime.calls[0].context.compatibility_enriched is True


def test_setup_without_trusted_approval_stays_pending_even_when_legacy_flag_is_true() -> None:
    """业务参数里的 confirmed_setup 不属于可信证据，不能让 setup 越过人审门禁。"""
    service = RecordingService()
    runtime = RecordingSkillExecutor(
        status=SkillExecutionStatus.PENDING,
        error_code=SkillErrorCode.APPROVAL_REQUIRED,
        summary="高风险 Skill 需要审批",
        audit_id=None,
    )
    executor = AgentToolExecutor(
        get_default_tool_registry(),
        service,
        skill_executor=runtime,
    )

    observation = executor.execute(
        "setup_live_session",
        {
            "plan_item_ids": ["p001"],
            "idempotency_key": "idem-pending-001",
            "confirmed_setup": True,
        },
        "room-pending",
        "trace-pending",
    )

    assert observation.status == "pending"
    assert runtime.calls[0].context.approval is None


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("query_products", {"unexpected": "TOP_SECRET-query"}),
        ("generate_live_plan", {"unexpected": "TOP_SECRET-plan"}),
        (
            "generate_product_card",
            {"product_id": "p001", "unexpected": "TOP_SECRET-card"},
        ),
        (
            "setup_live_session",
            {
                "plan_item_ids": ["p001"],
                "idempotency_key": "idem-unknown-key",
                "unexpected": "TOP_SECRET-setup",
            },
        ),
    ],
)
def test_core_compatibility_rejects_unknown_keys_before_enrichment_or_runtime(
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """未知旧参数必须在任何隐藏查询、计划生成和 Runtime 调用前失败关闭。"""
    service = RecordingService()
    runtime = RecordingSkillExecutor()
    executor = AgentToolExecutor(
        get_default_tool_registry(),
        service,
        skill_executor=runtime,
    )

    observation = executor.execute(
        tool_name,
        arguments,
        "room-unknown-key",
        "trace-unknown-key",
    )

    assert observation.status == "error"
    assert observation.summary == "INVALID_ARGUMENTS: invalid compatibility arguments"
    assert "TOP_SECRET" not in observation.summary
    assert service.calls == []
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("tool_name", "arguments", "secret"),
    [
        (
            "generate_product_card",
            {"product": {"product_id": "incomplete-TOP_SECRET"}},
            "incomplete-TOP_SECRET",
        ),
        (
            "generate_product_card",
            {"product_id": "unknown-TOP_SECRET"},
            "unknown-TOP_SECRET",
        ),
        (
            "setup_live_session",
            {
                "plan": _explicit_plan("mismatch-TOP_SECRET", "trace-input-error"),
                "idempotency_key": "idem-plan-mismatch",
            },
            "mismatch-TOP_SECRET",
        ),
        (
            "generate_live_plan",
            {"products": object()},
            "object",
        ),
    ],
)
def test_compatibility_input_errors_are_classified_and_sanitized(
    tool_name: str,
    arguments: dict[str, Any],
    secret: str,
) -> None:
    """领域校验、未知商品、计划错配和输入类型错误统一映射为脱敏参数错误。"""
    service = RecordingService()
    runtime = RecordingSkillExecutor()
    executor = AgentToolExecutor(
        get_default_tool_registry(),
        service,
        skill_executor=runtime,
    )

    observation = executor.execute(
        tool_name,
        arguments,
        "room-input-error",
        "trace-input-error",
    )

    assert observation.status == "error"
    assert observation.summary == "INVALID_ARGUMENTS: invalid compatibility arguments"
    assert secret not in observation.summary
    assert runtime.calls == []


def test_runtime_error_maps_status_summary_audit_and_stable_error_code() -> None:
    """Runtime 的受控错误字段必须完整映射，错误码用稳定前缀供旧 planner 识别。"""
    runtime = RecordingSkillExecutor(
        status=SkillExecutionStatus.ERROR,
        error_code=SkillErrorCode.INVALID_ARGUMENTS,
        summary="参数不合法",
        audit_id="audit-error-001",
    )
    executor = AgentToolExecutor(
        get_default_tool_registry(),
        RecordingService(),
        skill_executor=runtime,
    )

    observation = executor.execute(
        "query_products", {}, "room-error", "trace-error"
    )

    assert observation.status == "error"
    assert observation.summary == "INVALID_ARGUMENTS: 参数不合法"
    assert observation.audit_id == "audit-error-001"


def test_runtime_exception_does_not_fallback_to_legacy_core_dispatch() -> None:
    """Runtime 异常应固定摘要且不 fallback，也不能泄露异常中的敏感文本。"""
    service = RecordingService()
    runtime = RaisingSkillExecutor()
    executor = AgentToolExecutor(
        get_default_tool_registry(),
        service,
        skill_executor=runtime,
    )

    observation = executor.execute(
        "query_products", {}, "room-no-fallback", "trace-no-fallback"
    )

    assert observation.status == "error"
    assert observation.summary == "HANDLER_FAILED: skill runtime execution failed"
    assert "TOP_SECRET" not in observation.summary
    assert "password" not in observation.summary
    assert "p-secret" not in observation.summary
    assert len(runtime.calls) == 1
    assert service.calls == []


def test_compatibility_service_value_error_remains_sanitized_handler_failure() -> None:
    """隐藏服务的非输入 ValueError 必须保留执行失败分类，且不得调用 Runtime。"""
    runtime = RecordingSkillExecutor()
    executor = AgentToolExecutor(
        get_default_tool_registry(),
        RaisingService(),
        skill_executor=runtime,
    )

    observation = executor.execute(
        "generate_live_plan",
        {},
        "room-service-error",
        "trace-service-error",
    )

    assert observation.status == "error"
    assert observation.summary == "HANDLER_FAILED: skill runtime execution failed"
    assert "TOP_SECRET" not in observation.summary
    assert runtime.calls == []


NON_CORE_LEGACY_CASES: tuple[tuple[str, dict[str, Any], str, str, str], ...] = (
    (
        "suggest_price_change",
        {"product_id": "p001", "suggested_price": "35.90"},
        "PRE_LIVE",
        "error",
        "not dispatchable",
    ),
    (
        "set_product_price",
        {"product_id": "p001", "price": "35.90"},
        "PRE_LIVE",
        "pending",
        "requires human approval",
    ),
    (
        "create_live_plan_draft",
        {"room_id": "room-legacy"},
        "PRE_LIVE",
        "error",
        "not dispatchable",
    ),
    (
        "handle_sold_out_event",
        {
            "room_id": "room-legacy",
            "product_id": "p001",
            "trace_id": "trace-legacy",
            "idempotency_key": "idem-sold-out-001",
        },
        "ON_LIVE",
        "error",
        "not dispatchable",
    ),
    (
        "recommend_backup_product",
        {"room_id": "room-legacy", "sold_out_product_id": "p001"},
        "ON_LIVE",
        "success",
        "recommended backup",
    ),
    (
        "generate_on_live_prompt",
        {"room_id": "room-legacy", "sold_out_product_id": "p001"},
        "ON_LIVE",
        "success",
        "generated on-live prompt",
    ),
    (
        "aggregate_danmaku_questions",
        {"room_id": "room-legacy", "trace_id": "trace-legacy", "events": []},
        "ON_LIVE",
        "error",
        "not dispatchable",
    ),
    (
        "generate_danmaku_reply",
        {
            "room_id": "room-legacy",
            "trace_id": "trace-legacy",
            "category": "价格",
            "summary": "用户询问优惠",
        },
        "ON_LIVE",
        "error",
        "not dispatchable",
    ),
    (
        "on_live_context_collect",
        {"room_id": "room-legacy", "trace_id": "trace-legacy"},
        "ON_LIVE",
        "success",
        "collected context",
    ),
)


@pytest.mark.parametrize(
    ("tool_name", "arguments", "lifecycle", "expected_status", "summary_fragment"),
    NON_CORE_LEGACY_CASES,
)
def test_every_non_core_skill_keeps_legacy_dispatch(
    tool_name: str,
    arguments: dict[str, Any],
    lifecycle: str,
    expected_status: str,
    summary_fragment: str,
) -> None:
    """默认 Catalog 的九个非核心工具均保持既有门禁/派发语义且不调用 Runtime。"""
    runtime = RecordingSkillExecutor()
    executor = AgentToolExecutor(
        get_default_tool_registry(),
        RecordingService(),
        skill_executor=runtime,
    )

    observation = executor.execute(
        tool_name,
        arguments,
        "room-legacy",
        "trace-legacy",
        lifecycle=lifecycle,
    )

    assert observation.status == expected_status
    assert summary_fragment in observation.summary
    assert runtime.calls == []


def test_non_core_legacy_cases_cover_exactly_the_default_catalog_remainder() -> None:
    """参数化清单必须随默认 Catalog 变化而失败，防止新增非核心工具漏测路由。"""
    from src.skill_runtime.catalog import get_default_skill_catalog

    expected_non_core = {case[0] for case in NON_CORE_LEGACY_CASES}
    catalog_non_core = {
        manifest.skill_id
        for manifest in get_default_skill_catalog()
        if manifest.skill_id not in {
            "query_products",
            "generate_live_plan",
            "generate_product_card",
            "setup_live_session",
        }
    }

    assert catalog_non_core == expected_non_core
