"""Phase 11B 有状态 Fake Platform 测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.skill_runtime.fake_platform import (
    FakeFaultKind,
    FakeFaultRule,
    FakeLiveCommercePlatform,
    FakePlatformFixture,
    FakePlatformProduct,
)
from src.skill_runtime.models import (
    AdapterRequest,
    FailureCategory,
    FailureFact,
    SideEffectState,
)


def _fixture(
    *,
    products: tuple[FakePlatformProduct, ...] | None = None,
    faults: tuple[FakeFaultRule, ...] = (),
) -> FakePlatformFixture:
    """构造单商品独立 Fixture，避免每个测试共享全局平台状态。"""
    return FakePlatformFixture(
        room_id="room-001",
        products=products
        or (
            FakePlatformProduct(
                product_id="p001",
                name="测试商品",
                price=Decimal("39.90"),
                inventory=10,
                version=1,
            ),
        ),
        faults=faults,
    )


def _request(*, expected_version: int = 1) -> AdapterRequest:
    """构造带充分 deadline 的同一改价请求。"""
    return AdapterRequest(
        operation_id="operation-001",
        attempt_id="attempt-001",
        room_id="room-001",
        idempotency_key="price-idem-001",
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15),
        payload={
            "product_id": "p001",
            "price": "19.90",
            "expected_version": expected_version,
        },
    )


def test_price_compare_and_set_conflict_does_not_mutate_state() -> None:
    """过期版本必须返回 VERSION_CONFLICT，且不能发生最后写入获胜。"""
    platform = FakeLiveCommercePlatform.from_fixture(_fixture())
    before = platform.product("p001")

    result = asyncio.run(platform.set_price(_request(expected_version=99)))

    assert isinstance(result, FailureFact)
    assert result.category == FailureCategory.VERSION_CONFLICT
    assert result.side_effect_state == SideEffectState.NOT_SENT
    assert platform.product("p001") == before


def test_unknown_after_send_preserves_mutation_evidence() -> None:
    """发送后未知仍保留实际状态变更，调用方必须进入对账而不能自动重试。"""
    platform = FakeLiveCommercePlatform.from_fixture(
        _fixture(
            faults=(
                FakeFaultRule(
                    operation_name="set_price",
                    resource_key="p001",
                    call_index=1,
                    kind=FakeFaultKind.UNKNOWN_AFTER_SEND,
                ),
            )
        )
    )

    result = asyncio.run(platform.set_price(_request()))

    assert isinstance(result, FailureFact)
    assert result.category == FailureCategory.SIDE_EFFECT_UNKNOWN
    assert result.side_effect_state == SideEffectState.UNKNOWN
    assert platform.product("p001").price == Decimal("19.90")


def test_sold_out_unknown_after_send_preserves_inventory_mutation() -> None:
    """售罄请求发送后未知时，库存变更存在但结果必须进入对账语义。"""
    platform = FakeLiveCommercePlatform.from_fixture(
        _fixture(
            faults=(
                FakeFaultRule(
                    operation_name="mark_sold_out",
                    resource_key="p001",
                    call_index=1,
                    kind=FakeFaultKind.UNKNOWN_AFTER_SEND,
                ),
            )
        )
    )

    result = asyncio.run(platform.mark_sold_out(_request()))

    assert isinstance(result, FailureFact)
    assert result.category == FailureCategory.SIDE_EFFECT_UNKNOWN
    assert result.side_effect_state == SideEffectState.UNKNOWN
    assert platform.product("p001").inventory == 0
    assert platform.product("p001").is_active is False


def test_fixture_instances_do_not_share_product_state() -> None:
    """不同 Fixture 实例的状态必须完全隔离，避免测试或演示之间互相污染。"""
    first = FakeLiveCommercePlatform.from_fixture(_fixture())
    second = FakeLiveCommercePlatform.from_fixture(_fixture())

    result = asyncio.run(first.set_price(_request()))

    assert not isinstance(result, FailureFact)
    assert first.product("p001").price == Decimal("19.90")
    assert second.product("p001").price == Decimal("39.90")


def test_rate_limit_failure_exposes_retry_after_seconds() -> None:
    """声明式限流必须提供结构化等待时间，不能以异常文本传递重试建议。"""
    platform = FakeLiveCommercePlatform.from_fixture(
        _fixture(
            faults=(
                FakeFaultRule(
                    operation_name="list_products",
                    resource_key="room-001",
                    call_index=1,
                    kind=FakeFaultKind.RATE_LIMITED,
                    retry_after_seconds=7,
                ),
            )
        )
    )

    result = asyncio.run(platform.list_products(_request()))

    assert isinstance(result, FailureFact)
    assert result.category == FailureCategory.RATE_LIMITED
    assert result.retry_after_seconds == 7
    assert result.side_effect_state == SideEffectState.NOT_SENT


def test_sold_out_returns_next_active_product_as_backup() -> None:
    """售罄后应从同一实例的现存可售商品中确定性选择备选商品。"""
    backup = FakePlatformProduct(
        product_id="p002",
        name="备选商品",
        price=Decimal("59.90"),
        inventory=8,
        version=1,
    )
    platform = FakeLiveCommercePlatform.from_fixture(
        _fixture(products=(_fixture().products[0], backup))
    )

    result = asyncio.run(platform.mark_sold_out(_request()))

    assert not isinstance(result, FailureFact)
    assert result.output["sold_out_product"]["product_id"] == "p001"
    assert result.output["backup_product"]["product_id"] == "p002"


def test_prepare_session_replays_the_same_session_for_one_idempotency_key() -> None:
    """相同建播幂等键只能得到原会话，不能创建第二个外部建播副作用。"""
    platform = FakeLiveCommercePlatform.from_fixture(_fixture())

    first = asyncio.run(platform.prepare_session(_request()))
    second = asyncio.run(platform.prepare_session(_request()))

    assert not isinstance(first, FailureFact)
    assert not isinstance(second, FailureFact)
    assert first.output == second.output
    assert first.output["session"]["session_id"] == "session-1"
