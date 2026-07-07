"""Phase 2B 播中主播提示生成测试。"""

from decimal import Decimal

from src.skills.on_live_prompt import generate_sold_out_prompt
from src.state.models import Product


def test_generate_sold_out_prompt_mentions_sold_out_and_backup_product() -> None:
    """售罄提示应明确当前商品、备选商品和人工确认口径。"""

    sold_out = Product(product_id="p001", name="轻盈保温杯", price=Decimal("89.90"), inventory=0, is_active=False)
    backup = Product(product_id="p002", name="桌面理线器", price=Decimal("29.90"), inventory=30, is_active=True)

    prompt = generate_sold_out_prompt(sold_out_product=sold_out, backup_product=backup)

    assert "轻盈保温杯" in prompt.message
    assert "桌面理线器" in prompt.message
    assert "请主播确认" in prompt.message
    assert prompt.severity == "warning"


def test_generate_sold_out_prompt_supports_manual_takeover_when_no_backup() -> None:
    """没有备选商品时，提示应转为人工接管，而不是编造推荐。"""

    sold_out = Product(product_id="p001", name="轻盈保温杯", price=Decimal("89.90"), inventory=0, is_active=False)

    prompt = generate_sold_out_prompt(sold_out_product=sold_out, backup_product=None)

    assert "轻盈保温杯" in prompt.message
    assert "人工接管" in prompt.message
    assert prompt.severity == "critical"
