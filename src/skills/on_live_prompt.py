"""Phase 2B 播中主播提示生成。

提示文案使用确定性模板，不调用 LLM。模板必须明确风险和人工确认口径，避免
系统在播中场景里替主播做不可控承诺。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.state.models import Product


class OnLivePrompt(BaseModel):
    """播中提示结果。"""

    model_config = ConfigDict(frozen=True)

    message: str = Field(..., min_length=1)
    severity: str = Field(..., min_length=1)
    backup_product_id: str | None = None


def generate_sold_out_prompt(sold_out_product: Product, backup_product: Product | None) -> OnLivePrompt:
    """根据售罄商品和备选商品生成主播提示。"""

    if backup_product is None:
        return OnLivePrompt(
            message=(
                f"当前商品「{sold_out_product.name}」已售罄，暂无可用备选商品。"
                "建议立即暂停该商品讲解并进入人工接管。"
            ),
            severity="critical",
            backup_product_id=None,
        )

    return OnLivePrompt(
        message=(
            f"当前商品「{sold_out_product.name}」已售罄，系统建议切到「{backup_product.name}」。"
            "请主播确认库存和讲解口径后再继续推荐。"
        ),
        severity="warning",
        backup_product_id=backup_product.product_id,
    )
