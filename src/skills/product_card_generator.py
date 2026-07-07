"""Phase 2A 商品讲解手卡生成器。

手卡生成仍采用确定性模板，不调用 LLM。模板只使用本地样例货盘字段，
不会编造商品功效、真实平台数据或真实用户反馈。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.skills.product_catalog import CatalogProduct


class ProductCard(BaseModel):
    """面向主播副屏展示的单个商品手卡。"""

    model_config = ConfigDict(frozen=True)

    product_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    talking_points: list[str] = Field(..., min_length=1)
    opening_script: str = Field(..., min_length=1)
    price_hint: str = Field(..., min_length=1)
    risk_tips: list[str] = Field(..., min_length=1)


def generate_product_card(product: CatalogProduct) -> ProductCard:
    """为商品生成可直接讲解的确定性手卡。"""

    talking_points = _normalize_talking_points(product)
    return ProductCard(
        product_id=product.product_id,
        title=f"{product.name}｜{product.category}手卡",
        talking_points=talking_points,
        opening_script=f"接下来这款是 {product.name}，适合从 {talking_points[0]} 这个点切入讲解。",
        price_hint=f"本地样例货盘价格为 {product.price} 元，实际直播口径需以人工确认后的价格为准。",
        risk_tips=_build_risk_tips(product),
    )


def _normalize_talking_points(product: CatalogProduct) -> list[str]:
    """补齐 3 条讲解点，避免缺字段商品导致 CLI 展示空白。"""

    points = [point for point in product.selling_points if point]
    fallback_points = [
        f"{product.category}场景适配",
        "库存信息来自本地样例货盘",
        "适合作为直播间结构化讲解素材",
    ]
    for point in fallback_points:
        if len(points) >= 3:
            break
        points.append(point)
    return points[:3]


def _build_risk_tips(product: CatalogProduct) -> list[str]:
    """生成合规提示，提醒主播不要把样例数据说成真实承诺。"""

    tips = [
        "避免绝对化用语，不承诺全网最低、永久有效或百分百效果。",
        "不得编造真实用户评价、真实成交数据或平台未提供的功效证明。",
    ]
    if product.inventory < 10:
        tips.append("库存较低，讲解时需要提醒以实时库存为准。")
    return tips
