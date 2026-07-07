"""Phase 2B 播中备选商品推荐测试。"""

from decimal import Decimal

import pytest

from src.skills.backup_product_recommender import BackupProductNotFoundError, recommend_backup_product
from src.state.models import LifecycleStage, LiveRoomState, Product


def make_state() -> LiveRoomState:
    """构造播中状态，包含售罄商品、可用备选和不可用商品。"""

    return LiveRoomState(
        room_id="room-demo-001",
        lifecycle=LifecycleStage.ON_LIVE,
        current_product_id="p001",
        products=[
            Product(product_id="p001", name="轻盈保温杯", price=Decimal("89.90"), inventory=0, is_active=False),
            Product(product_id="p002", name="桌面理线器", price=Decimal("29.90"), inventory=30, is_active=True, conversion_rate=Decimal("0.30")),
            Product(product_id="p003", name="已下架商品", price=Decimal("59.90"), inventory=20, is_active=False),
        ],
    )


def test_recommend_backup_product_skips_sold_out_product() -> None:
    """备选商品不能推荐刚刚售罄的商品。"""

    backup = recommend_backup_product(make_state(), sold_out_product_id="p001")

    assert backup.product_id == "p002"
    assert backup.inventory > 0
    assert backup.is_active is True


def test_recommend_backup_product_rejects_missing_sold_out_product() -> None:
    """售罄商品不存在时应明确失败，避免错误事件悄悄通过。"""

    with pytest.raises(BackupProductNotFoundError):
        recommend_backup_product(make_state(), sold_out_product_id="p999")


def test_recommend_backup_product_reports_no_available_backup() -> None:
    """没有可用备选商品时，需要进入人工接管分支。"""

    state = LiveRoomState(
        room_id="room-demo-001",
        lifecycle=LifecycleStage.ON_LIVE,
        products=[
            Product(product_id="p001", name="轻盈保温杯", price=Decimal("89.90"), inventory=0, is_active=False),
        ],
    )

    with pytest.raises(BackupProductNotFoundError):
        recommend_backup_product(state, sold_out_product_id="p001")
