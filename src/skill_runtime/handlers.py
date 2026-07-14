"""Phase 11B 统一 Skill Handler 装配。

本模块把播前、播中批次一能力收敛到单个局部工厂。Handler 只做领域编排：
平台状态经业务域 Port 读取，确定性能力复用既有领域函数，失败事实原样交回
Executor。这里不做重试、不做 Legacy fallback，也不修改 Graph checkpoint 拓扑。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skill_runtime.executor import _SkillHandler, _SkillHandlerResult
from src.skill_runtime.models import (
    AdapterRequest,
    FailureFact,
    SkillExecutionContext,
)
from src.skill_runtime.platform_ports import (
    AdapterResult,
    LiveOperationsPort,
    LiveSessionPort,
    ProductPricingPort,
)
from src.skills.danmaku_aggregator import (
    DanmakuQuestionCategory,
    DanmakuQuestionGroup,
    aggregate_danmaku_questions,
)
from src.skills.danmaku_events import DanmakuEvent
from src.skills.danmaku_reply_generator import generate_danmaku_reply
from src.skills.live_plan_generator import generate_live_plan
from src.skills.live_plan_generator import LivePlanDraft
from src.skills.on_live_prompt import generate_sold_out_prompt
from src.skills.product_card_generator import generate_product_card
from src.skills.product_catalog import CatalogProduct
from src.state.models import LiveRoomState, Product


@dataclass(frozen=True)
class SkillRuntimeDependencies:
    """统一 Handler 工厂的局部依赖。

    platform 同时实现三个业务域 Port 是 Phase 11B Fake 的当前装配方式；真实平台
    接入时可以传入三个不同对象。legacy_pre_live_service 只供仍需复用播前领域生成
    逻辑的纯业务 Handler 使用；建播与售罄已分别收敛到会话和播中运营 Port。
    """

    platform: ProductPricingPort | LiveSessionPort | LiveOperationsPort
    legacy_pre_live_service: PreLiveBusinessFlowService | None = None

    @property
    def product_pricing_port(self) -> ProductPricingPort:
        """商品与价格 Port 视图。"""
        return self.platform  # type: ignore[return-value]

    @property
    def live_session_port(self) -> LiveSessionPort:
        """直播会话 Port 视图。"""
        return self.platform  # type: ignore[return-value]

    @property
    def live_operations_port(self) -> LiveOperationsPort:
        """播中运营 Port 视图。"""
        return self.platform  # type: ignore[return-value]


def build_skill_handlers(dependencies: SkillRuntimeDependencies) -> dict[str, _SkillHandler]:
    """为一个 Runtime 实例构建局部 Handler 映射。

    返回值包含全部 13 个 Skill 的装配入口。批次一、批次二和批次三均已接入统一
    执行契约；每个外部能力只经对应业务域 Port，禁止静默回退旧执行路径。
    """
    batch_one: dict[str, _SkillHandler] = {
        "query_products": _QueryProductsHandler(dependencies.product_pricing_port),
        "generate_live_plan": _GenerateLivePlanHandler(dependencies.legacy_pre_live_service),
        "generate_product_card": _GenerateProductCardHandler(dependencies.legacy_pre_live_service),
        "suggest_price_change": _SuggestPriceChangeHandler(),
        "create_live_plan_draft": _CreateLivePlanDraftHandler(
            dependencies.product_pricing_port,
            dependencies.legacy_pre_live_service,
        ),
        "recommend_backup_product": _RecommendBackupProductHandler(dependencies.live_operations_port),
        "generate_on_live_prompt": _GenerateOnLivePromptHandler(dependencies.live_operations_port),
        "aggregate_danmaku_questions": _AggregateDanmakuQuestionsHandler(),
        "generate_danmaku_reply": _GenerateDanmakuReplyHandler(),
        "on_live_context_collect": _OnLiveContextCollectHandler(dependencies.live_operations_port),
    }
    return {
        **batch_one,
        "setup_live_session": _SetupLiveSessionHandler(dependencies.live_session_port),
        "handle_sold_out_event": _HandleSoldOutEventHandler(dependencies.live_operations_port),
        "set_product_price": _SetProductPriceHandler(dependencies.product_pricing_port),
    }


class _QueryProductsHandler(_SkillHandler):
    """通过 ProductPricingPort 查询可信货盘快照。"""

    def __init__(self, port: ProductPricingPort) -> None:
        self._port = port

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> AdapterResult | _SkillHandlerResult | dict[str, Any]:
        result = await self._port.list_products(_request(skill_id, arguments, context))
        if isinstance(result, FailureFact):
            return result
        return {
            "products": [
                _catalog_snapshot_from_platform(product)
                for product in result.output.get("products", [])
            ]
        }


class _GenerateLivePlanHandler(_SkillHandler):
    """基于显式商品快照生成确定性播前计划。"""

    def __init__(self, service: PreLiveBusinessFlowService | None = None) -> None:
        self._service = service

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> dict[str, Any]:
        products = [CatalogProduct.model_validate(item) for item in arguments.get("products", [])]
        # 兼容装配下继续走旧播前服务，以保留已验收的 ToolCallAudit 链路；
        # 无服务依赖的 Fake/单元测试则复用纯确定性领域函数。
        if self._service is not None:
            plan = self._service.generate_plan(context.room_id, products, context.trace_id)
        else:
            plan = generate_live_plan(context.room_id, products, context.trace_id)
        return {"plan": plan.model_dump(mode="json")}


class _GenerateProductCardHandler(_SkillHandler):
    """基于单商品快照生成确定性手卡。"""

    def __init__(self, service: PreLiveBusinessFlowService | None = None) -> None:
        self._service = service

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> dict[str, Any]:
        product = CatalogProduct.model_validate(arguments.get("product", {}))
        if self._service is not None:
            card = self._service.generate_card(context.room_id, product, context.trace_id)
        else:
            card = generate_product_card(product)
        return {"card": card.model_dump(mode="json")}


class _SuggestPriceChangeHandler(_SkillHandler):
    """生成改价建议文本，不执行任何价格写入。"""

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> dict[str, Any]:
        return {
            "suggestion": {
                "product_id": arguments["product_id"],
                "suggested_price": arguments["suggested_price"],
                "reason": "仅生成播前改价建议，实际改价必须走高风险 set_product_price。",
            }
        }


class _CreateLivePlanDraftHandler(_SkillHandler):
    """读取 Port 货盘后生成计划草案，保持只读语义。"""

    def __init__(
        self,
        port: ProductPricingPort,
        service: PreLiveBusinessFlowService | None = None,
    ) -> None:
        self._port = port
        self._service = service

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> AdapterResult | dict[str, Any]:
        result = await self._port.list_products(_request(skill_id, arguments, context))
        if isinstance(result, FailureFact):
            return result
        products = [
            CatalogProduct.model_validate(_catalog_snapshot_from_platform(product))
            for product in result.output.get("products", [])
        ]
        if self._service is not None:
            plan = self._service.generate_plan(context.room_id, products, context.trace_id)
        else:
            plan = generate_live_plan(context.room_id, products, context.trace_id)
        return {"plan": plan.model_dump(mode="json")}


class _RecommendBackupProductHandler(_SkillHandler):
    """经 LiveOperationsPort 获取商品上下文后推荐备选商品。"""

    def __init__(self, port: LiveOperationsPort) -> None:
        self._port = port

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> AdapterResult | dict[str, Any]:
        result = await self._port.resolve_product_context(_request(skill_id, arguments, context))
        if isinstance(result, FailureFact):
            return result
        sold_out, backup = _products_from_context_output(result.output)
        # 复用既有确定性推荐器需要完整 LiveRoomState。Port 已提供可信快照，这里只
        # 负责把快照恢复为领域模型，不再读取旧 Graph State。
        state = LiveRoomState(
            room_id=context.room_id,
            lifecycle=context.lifecycle,
            products=[product for product in (sold_out, backup) if product is not None],
        )
        from src.skills.backup_product_recommender import recommend_backup_product

        recommended = recommend_backup_product(state, sold_out_product_id=sold_out.product_id)
        return {"backup_product": recommended.model_dump(mode="json")}


class _GenerateOnLivePromptHandler(_SkillHandler):
    """经 Port 解析商品快照后生成播中主播提示。"""

    def __init__(self, port: LiveOperationsPort) -> None:
        self._port = port

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> AdapterResult | dict[str, Any]:
        result = await self._port.resolve_product_context(_request(skill_id, arguments, context))
        if isinstance(result, FailureFact):
            return result
        sold_out, backup = _products_from_context_output(result.output)
        prompt = generate_sold_out_prompt(sold_out_product=sold_out, backup_product=backup)
        return {"prompt": prompt.model_dump(mode="json")}


class _AggregateDanmakuQuestionsHandler(_SkillHandler):
    """把显式弹幕事件快照聚合为确定性问题分组。"""

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> dict[str, Any]:
        events = [DanmakuEvent.model_validate(event) for event in arguments.get("events", [])]
        groups = aggregate_danmaku_questions(events, window_seconds=5)
        return {"groups": [group.model_dump(mode="json") for group in groups]}


class _GenerateDanmakuReplyHandler(_SkillHandler):
    """为单个聚合问题生成主播参考回复。"""

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        group = DanmakuQuestionGroup(
            room_id=arguments["room_id"],
            trace_id=arguments["trace_id"],
            category=DanmakuQuestionCategory(arguments["category"]),
            summary=arguments["summary"],
            count=int(arguments.get("count", 1)),
            sample_contents=list(arguments.get("sample_contents", [])),
            window_start=now,
            window_end=now + timedelta(seconds=5),
        )
        reply = generate_danmaku_reply(group)
        return {"reply": reply.model_dump(mode="json")}


class _OnLiveContextCollectHandler(_SkillHandler):
    """通过 LiveOperationsPort 收集播中上下文事实。"""

    def __init__(self, port: LiveOperationsPort) -> None:
        self._port = port

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> AdapterResult | dict[str, Any]:
        result = await self._port.current_context(_request(skill_id, arguments, context))
        if isinstance(result, FailureFact):
            return result
        return {
            "inventory_alerts": result.output.get("inventory_alerts", []),
            "danmaku_summary": result.output.get("danmaku_summary", []),
        }


class _SetupLiveSessionHandler(_SkillHandler):
    """通过 LiveSessionPort 执行建播准备。

    审批、幂等键和 Attempt 意图均由 SkillExecutor 在调用本 Handler 前处理。本类只
    负责把已验证的计划快照交给会话 Port，并把平台确认的 session 事实映射回旧
    Facade 仍需要的 allowed/setup_status 字段。
    """

    def __init__(self, port: LiveSessionPort) -> None:
        self._port = port

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> AdapterResult | dict[str, Any]:
        plan = LivePlanDraft.model_validate(arguments.get("plan", {}))
        result = await self._port.prepare_session(
            _request(
                skill_id,
                {
                    "plan": plan.model_dump(mode="json"),
                    "idempotency_key": context.idempotency_key,
                },
                context,
            )
        )
        if isinstance(result, FailureFact):
            return result
        session = result.output.get("session", {})
        output = {
            "allowed": True,
            "setup_status": str(session.get("status") or "prepared"),
            "session": session,
        }
        audit_id = result.output.get("audit_id")
        if isinstance(audit_id, str):
            return _SkillHandlerResult(output=output, audit_id=audit_id)
        return output


class _HandleSoldOutEventHandler(_SkillHandler):
    """通过 LiveOperationsPort 处理售罄状态变化。

    售罄是写操作，但恢复和重试策略不属于 Handler。Executor 已先写 Attempt 意图；
    Port 返回成功事实或 FailureFact 后，本类只补充确定性主播提示，确保同一幂等键
    重放时不会再次调用 Port。
    """

    def __init__(self, port: LiveOperationsPort) -> None:
        self._port = port

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> AdapterResult | dict[str, Any]:
        result = await self._port.mark_sold_out(_request(skill_id, arguments, context))
        if isinstance(result, FailureFact):
            return result
        sold_out, backup = _products_from_context_output(result.output)
        prompt = generate_sold_out_prompt(sold_out_product=sold_out, backup_product=backup)
        return {
            "sold_out_product": sold_out.model_dump(mode="json"),
            "backup_product": None if backup is None else backup.model_dump(mode="json"),
            "prompt": prompt.model_dump(mode="json"),
        }


class _SetProductPriceHandler(_SkillHandler):
    """执行单次、带资源版本保护的高风险改价。

    SkillExecutor 已在进入本 Handler 前校验精确 Skill 版本、Schema、幂等键和可信
    审批，并写入 Operation intent。本类因此不能预读商品、重试、睡眠或 fallback，
    只能把已冻结业务参数与可信 Context 组装为一次 AdapterRequest 后调用 Port。
    ProductPricingPort 返回的成功事实或 FailureFact 必须原样交回 Executor，以保留
    VERSION_CONFLICT、RATE_LIMITED 和 SIDE_EFFECT_UNKNOWN 的恢复语义。
    """

    def __init__(self, port: ProductPricingPort) -> None:
        self._port = port

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> AdapterResult:
        """仅执行一次 CAS 改价 Port 调用，禁止在 Handler 层隐藏恢复策略。"""
        return await self._port.set_price(_request(skill_id, arguments, context))


def _request(
    skill_id: str,
    arguments: dict[str, Any],
    context: SkillExecutionContext,
) -> AdapterRequest:
    """为只读 Port 调用构造可信 AdapterRequest。

    批次一没有外部写副作用，因此未强制要求幂等键。operation_id 与 attempt_id
    仍保持稳定可审计形状，后续写批次会由 Attempt Store 负责唯一 Operation。
    """
    return AdapterRequest(
        operation_id=f"{context.trace_id}:{skill_id}",
        attempt_id=context.attempt_id or f"{context.trace_id}:{skill_id}:attempt",
        room_id=context.room_id,
        idempotency_key=context.idempotency_key,
        deadline_at=context.deadline_at,
        payload={**dict(arguments), "__trace_id": context.trace_id},
    )


def _catalog_snapshot_from_platform(snapshot: dict[str, Any]) -> dict[str, Any]:
    """把平台商品快照补齐为 CatalogProduct 可校验结构。"""
    return {
        "product_id": snapshot["product_id"],
        "name": snapshot["name"],
        "category": snapshot.get("category") or "默认分类",
        "price": str(snapshot["price"]),
        "inventory": int(snapshot["inventory"]),
        "conversion_rate": str(snapshot.get("conversion_rate") or "0"),
        "commission_rate": str(snapshot.get("commission_rate") or "0"),
        "tags": list(snapshot.get("tags") or []),
        "selling_points": list(snapshot.get("selling_points") or []),
        "is_active": bool(snapshot.get("is_active", True)),
    }


def _product_from_platform(snapshot: dict[str, Any]) -> Product:
    """把平台商品快照恢复为播中领域 Product。"""
    return Product(
        product_id=snapshot["product_id"],
        name=snapshot["name"],
        price=Decimal(str(snapshot["price"])),
        inventory=int(snapshot["inventory"]),
        is_active=bool(snapshot.get("is_active", True)),
        conversion_rate=Decimal(str(snapshot.get("conversion_rate") or "0")),
        tags=list(snapshot.get("tags") or []),
    )


def _products_from_context_output(output: dict[str, Any]) -> tuple[Product, Product | None]:
    """读取 Port 上下文输出并恢复售罄商品与可选备选商品。"""
    sold_out = _product_from_platform(output["sold_out_product"])
    backup_raw = output.get("backup_product")
    backup = None if backup_raw is None else _product_from_platform(backup_raw)
    return sold_out, backup
