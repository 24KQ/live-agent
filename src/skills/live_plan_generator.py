"""Phase 2A 确定性播前排品生成器。

当前阶段不接 LLM，因此排品完全由可测试的规则生成。这样可以先验证货盘、
工具门禁、审计和 CLI 演示闭环，后续再把 LLM 放到受控边界内替换生成策略。
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.skills.product_catalog import CatalogProduct


class LivePlanItem(BaseModel):
    """排品方案中的单个商品位。"""

    model_config = ConfigDict(frozen=True)

    rank: int = Field(..., ge=1)
    product_id: str = Field(..., min_length=1)
    product_name: str = Field(..., min_length=1)
    role: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)


class LivePlanDraft(BaseModel):
    """播前排品草案。"""

    model_config = ConfigDict(frozen=True)

    room_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    items: list[LivePlanItem] = Field(..., min_length=1)


def generate_live_plan(room_id: str, products: list[CatalogProduct], trace_id: str) -> LivePlanDraft:
    """基于货盘生成确定性排品草案。

    规则优先保证可解释：先挑引流款，再挑利润款，再挑氛围款，剩余商品作为常规款。
    每个分组内部使用转化率、库存、佣金率和价格组成的简单分数排序，避免生成结果
    依赖数据库物理顺序。
    """

    if not products:
        raise ValueError("products must not be empty when generating a live plan")

    role_order = ["引流款", "利润款", "氛围款", "常规款"]
    grouped_products = {
        role: sorted(
            [product for product in products if _classify_role(product) == role],
            key=_plan_score,
            reverse=True,
        )
        for role in role_order
    }

    # 播前方案的前三个位置要尽量覆盖不同角色，避免样例货盘里某一类商品过多时，
    # 开场连续推荐同质商品，影响主播对整场节奏的理解。
    selected: list[CatalogProduct] = []
    selected_ids: set[str] = set()
    for role in role_order:
        if grouped_products[role]:
            representative = grouped_products[role][0]
            selected.append(representative)
            selected_ids.add(representative.product_id)

    for role in role_order:
        for product in grouped_products[role]:
            if product.product_id in selected_ids:
                continue
            selected.append(product)
            selected_ids.add(product.product_id)

    items = [
        LivePlanItem(
            rank=index,
            product_id=product.product_id,
            product_name=product.name,
            role=_classify_role(product),
            reason=_build_reason(product, _classify_role(product)),
        )
        for index, product in enumerate(selected, start=1)
    ]
    return LivePlanDraft(room_id=room_id, trace_id=trace_id, items=items)


def _classify_role(product: CatalogProduct) -> str:
    """按标签和关键指标给商品分配直播间角色。"""

    tag_text = " ".join(product.tags)
    if "引流" in tag_text or product.price <= Decimal("49.90") or product.conversion_rate >= Decimal("0.20"):
        return "引流款"
    if "利润" in tag_text or product.commission_rate >= Decimal("0.25"):
        return "利润款"
    if "氛围" in tag_text or product.inventory >= 50:
        return "氛围款"
    return "常规款"


def _plan_score(product: CatalogProduct) -> Decimal:
    """计算分组内排序分数。

    该分数只用于同角色商品排序，不对外暴露为业务承诺，避免主播误把它理解成真实
    平台推荐分。
    """

    inventory_factor = Decimal(min(product.inventory, 100)) / Decimal("100")
    price_penalty = product.price / Decimal("1000")
    return product.conversion_rate * Decimal("100") + product.commission_rate * Decimal("10") + inventory_factor - price_penalty


def _build_reason(product: CatalogProduct, role: str) -> str:
    """生成可审计、可读的排品理由。"""

    return (
        f"{role}：库存 {product.inventory}，转化率 {product.conversion_rate}，"
        f"佣金率 {product.commission_rate}，适合在播前方案中承担该商品位。"
    )
