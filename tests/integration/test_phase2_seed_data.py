"""Phase 2A 样例数据初始化集成测试。"""

from src.config.settings import get_settings
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data
from src.skills.product_catalog import ProductCatalogRepository


def test_seed_phase2_demo_data_creates_room_products() -> None:
    """seed 脚本应创建 1 个直播间并关联 10 个脱敏样例商品。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)

    products = ProductCatalogRepository(settings).list_room_products("room-demo-001")

    assert len(products) == 10
    assert products[0].product_id == "p001"
    assert all(product.inventory > 0 for product in products)
    assert all("手机" not in point for product in products for point in product.selling_points)
