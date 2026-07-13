"""Phase 11B 批次一统一 Handler 装配测试。

这些测试只覆盖 Task 5 的低风险与确定性能力迁移：Handler 必须从局部
Dependencies 装配出来，平台状态只能通过业务域 Port 读取，不能回到旧 Graph
state、全局注册表或隐式 Legacy fallback。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from src.skill_runtime.fake_platform import (
    FakeLiveCommercePlatform,
    FakePlatformFixture,
    FakePlatformProduct,
)
from src.skill_runtime.models import (
    AdapterRequest,
    SkillExecutionContext,
    SkillExecutionRoute,
)
from src.state.models import LifecycleStage


BATCH_ONE_SKILL_IDS = {
    "query_products",
    "generate_live_plan",
    "generate_product_card",
    "suggest_price_change",
    "create_live_plan_draft",
    "recommend_backup_product",
    "generate_on_live_prompt",
    "aggregate_danmaku_questions",
    "generate_danmaku_reply",
    "on_live_context_collect",
}


def _context(lifecycle: LifecycleStage = LifecycleStage.PRE_LIVE) -> SkillExecutionContext:
    """构造批次一测试用可信上下文，deadline 只由测试控制，不放入业务参数。"""
    return SkillExecutionContext(
        room_id="room-001",
        trace_id="trace-001",
        lifecycle=lifecycle,
        execution_route=SkillExecutionRoute.SKILL_RUNTIME,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15),
    )


def _platform() -> FakeLiveCommercePlatform:
    """构造包含售罄商品与备选商品的独立 Fake 平台。"""
    return FakeLiveCommercePlatform.from_fixture(
        FakePlatformFixture(
            room_id="room-001",
            products=(
                FakePlatformProduct(
                    product_id="p001",
                    name="售罄商品",
                    price=Decimal("39.90"),
                    inventory=0,
                    version=3,
                    is_active=False,
                ),
                FakePlatformProduct(
                    product_id="p002",
                    name="备选商品",
                    price=Decimal("59.90"),
                    inventory=8,
                    version=1,
                ),
            ),
        )
    )


@pytest.mark.parametrize("skill_id", sorted(BATCH_ONE_SKILL_IDS))
def test_batch_one_handlers_are_registered(skill_id: str) -> None:
    """统一工厂必须一次性装配批次一 10 个 Handler。"""
    from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers

    handlers = build_skill_handlers(SkillRuntimeDependencies(platform=_platform()))

    assert skill_id in handlers


def test_query_products_uses_product_port_snapshot() -> None:
    """query_products 应读取 ProductPricingPort 的可信快照，而不是旧播前服务。"""
    from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers

    handlers = build_skill_handlers(SkillRuntimeDependencies(platform=_platform()))

    result = asyncio.run(
        handlers["query_products"].execute(
            "query_products",
            {},
            _context(),
        )
    )

    assert result["products"][0]["product_id"] == "p002"


def test_recommend_backup_product_uses_live_operations_context() -> None:
    """备选推荐必须经 LiveOperationsPort 解析商品上下文后复用确定性排序规则。"""
    from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers

    handlers = build_skill_handlers(SkillRuntimeDependencies(platform=_platform()))

    result = asyncio.run(
        handlers["recommend_backup_product"].execute(
            "recommend_backup_product",
            {"room_id": "room-001", "sold_out_product_id": "p001"},
            _context(LifecycleStage.ON_LIVE),
        )
    )

    assert result["backup_product"]["product_id"] == "p002"


def test_generate_on_live_prompt_uses_resolved_product_snapshots() -> None:
    """主播提示应从 Port 返回的可信快照生成，不从 arguments 伪造商品对象。"""
    from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers

    handlers = build_skill_handlers(SkillRuntimeDependencies(platform=_platform()))

    result = asyncio.run(
        handlers["generate_on_live_prompt"].execute(
            "generate_on_live_prompt",
            {
                "room_id": "room-001",
                "sold_out_product_id": "p001",
                "backup_product_id": "p002",
            },
            _context(LifecycleStage.ON_LIVE),
        )
    )

    assert "售罄商品" in result["prompt"]["message"]
    assert "备选商品" in result["prompt"]["message"]
    assert result["prompt"]["severity"] == "warning"


def test_on_live_context_collect_reads_current_context_port() -> None:
    """播中上下文收集只读取 Port 的库存告警和弹幕摘要事实。"""
    from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers

    handlers = build_skill_handlers(SkillRuntimeDependencies(platform=_platform()))

    result = asyncio.run(
        handlers["on_live_context_collect"].execute(
            "on_live_context_collect",
            {"room_id": "room-001", "trace_id": "trace-001"},
            _context(LifecycleStage.ON_LIVE),
        )
    )

    assert result["inventory_alerts"][0]["product_id"] == "p001"
    assert result["danmaku_summary"] == []
