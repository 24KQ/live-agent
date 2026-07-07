"""Phase 2A 播前货盘查询能力。

本模块只负责把 PostgreSQL 中的脱敏样例商品转换成领域模型，并筛选出
播前可用商品。它不生成排品、不生成话术，也不修改状态；这些职责分别由
排品生成器、手卡生成器和业务流服务承担，避免一个文件承载过多业务。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from src.config.settings import Settings


class CatalogProduct(BaseModel):
    """本地样例货盘中的单个商品。

    字段全部来自本地 PostgreSQL 样例数据，不包含真实用户、真实订单或真实平台
    凭据。价格、库存、转化率和佣金率都使用强校验，防止脏数据进入排品逻辑。
    """

    model_config = ConfigDict(frozen=True)

    product_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    price: Decimal = Field(..., ge=Decimal("0"))
    inventory: int = Field(..., ge=0)
    conversion_rate: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    commission_rate: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    tags: list[str] = Field(default_factory=list)
    selling_points: list[str] = Field(default_factory=list)
    is_active: bool = True


def filter_available_products(products: list[CatalogProduct]) -> list[CatalogProduct]:
    """筛选可用于播前排品的商品。

    只有已启用且库存大于 0 的商品会进入后续排品。返回值按商品 ID 排序，
    保证数据库返回顺序变化时，单元测试和 CLI 演示仍然稳定可复现。
    """

    return sorted(
        [product for product in products if product.is_active and product.inventory > 0],
        key=lambda product: product.product_id,
    )


class ProductCatalogRepository:
    """PostgreSQL 货盘读取仓储。

    仓储层只做只读查询，不做 seed、不做写操作。所有连接参数都来自 Settings，
    避免在代码里硬编码本机账号密码。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def list_room_products(self, room_id: str) -> list[CatalogProduct]:
        """按直播间读取已关联的可用商品列表。

        查询结果先转换为 CatalogProduct，再通过统一筛选函数剔除售罄或停用商品。
        如果数据库不可用，psycopg 异常会向上抛出，让调用方明确失败原因，而不是
        返回一个看似正常的空货盘。
        """

        sql = """
            SELECT
                p.product_id,
                p.name,
                p.category,
                p.price,
                p.inventory,
                p.conversion_rate,
                p.commission_rate,
                p.tags,
                p.selling_points,
                p.is_active
            FROM live_agent_room_products rp
            JOIN live_agent_products p ON p.product_id = rp.product_id
            WHERE rp.room_id = %(room_id)s
            ORDER BY rp.display_order ASC, p.product_id ASC;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, {"room_id": room_id})
                rows = cursor.fetchall()

        products = [self._row_to_product(row) for row in rows]
        return filter_available_products(products)

    @staticmethod
    def _row_to_product(row: dict[str, Any]) -> CatalogProduct:
        """把数据库行转换成领域模型。

        JSONB 字段在 psycopg 中通常会直接返回 list；这里仍做兜底处理，
        防止未来驱动配置变化导致 None 进入 Pydantic 校验。
        """

        return CatalogProduct(
            product_id=row["product_id"],
            name=row["name"],
            category=row["category"],
            price=Decimal(str(row["price"])),
            inventory=int(row["inventory"]),
            conversion_rate=Decimal(str(row["conversion_rate"])),
            commission_rate=Decimal(str(row["commission_rate"])),
            tags=list(row.get("tags") or []),
            selling_points=list(row.get("selling_points") or []),
            is_active=bool(row["is_active"]),
        )
