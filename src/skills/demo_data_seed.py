"""Phase 2A 脱敏样例数据初始化。

seed 数据只服务本地演示和集成测试，不包含真实商品、真实主播或真实用户信息。
脚本可以重复执行，所有主键均使用稳定 ID，并通过 upsert 保持幂等。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from src.config.settings import Settings


@dataclass(frozen=True)
class SeedResult:
    """seed 执行结果摘要，供 CLI 展示和测试断言使用。"""

    anchor_count: int
    room_count: int
    product_count: int


DEMO_ROOM_ID = "room-demo-001"
DEMO_ANCHOR_ID = "anchor-demo-001"


DEMO_PRODUCTS = [
    ("p001", "轻盈保温杯", "家居", "89.90", 30, "0.1800", "0.2000", ["引流款", "新品"], ["杯身轻", "保温时间长", "适合通勤"]),
    ("p002", "折叠收纳箱", "家居", "59.90", 45, "0.1200", "0.1800", ["氛围款"], ["节省空间", "容量清晰", "适合衣柜整理"]),
    ("p003", "多功能料理锅", "厨房", "199.00", 18, "0.0800", "0.3500", ["利润款"], ["一锅多用", "适合小家庭", "清洁方便"]),
    ("p004", "柔雾护眼台灯", "家电", "129.00", 22, "0.1100", "0.2400", ["常规款"], ["三档亮度", "桌面不占位", "适合夜间阅读"]),
    ("p005", "便携筋膜球", "运动", "39.90", 80, "0.2600", "0.1200", ["引流款"], ["体积小", "使用门槛低", "适合办公室放松"]),
    ("p006", "棉柔浴巾套装", "家纺", "79.90", 60, "0.1600", "0.1900", ["氛围款"], ["触感柔软", "吸水性好", "适合家庭囤货"]),
    ("p007", "桌面理线器", "数码", "29.90", 120, "0.3000", "0.0800", ["引流款"], ["价格友好", "桌面整洁", "安装简单"]),
    ("p008", "空气炸锅纸托", "厨房", "49.90", 90, "0.2200", "0.1500", ["氛围款"], ["减少清洗", "适配常见锅型", "适合组合讲解"]),
    ("p009", "轻奢香薰蜡烛", "生活", "109.00", 16, "0.0700", "0.3200", ["利润款"], ["包装精致", "适合礼赠", "提升直播间氛围"]),
    ("p010", "儿童绘画套装", "母婴", "69.90", 35, "0.1400", "0.2100", ["常规款"], ["颜色丰富", "收纳方便", "亲子互动场景"]),
]


def initialize_phase2_schema(settings: Settings) -> None:
    """执行 Phase 2A schema 初始化 SQL。"""

    sql = Path("docker/init_phase2_pre_live.sql").read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
        connection.commit()


def seed_phase2_demo_data(settings: Settings) -> SeedResult:
    """写入可重复执行的脱敏样例数据。"""

    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO live_agent_anchors(anchor_id, display_name, style_tags)
                VALUES (%(anchor_id)s, %(display_name)s, %(style_tags)s)
                ON CONFLICT (anchor_id)
                DO UPDATE SET display_name = EXCLUDED.display_name, style_tags = EXCLUDED.style_tags;
                """,
                {
                    "anchor_id": DEMO_ANCHOR_ID,
                    "display_name": "Demo 主播",
                    "style_tags": Jsonb(["稳健讲解", "重视合规", "偏好结构化手卡"]),
                },
            )
            cursor.execute(
                """
                INSERT INTO live_agent_live_rooms(room_id, anchor_id, title, lifecycle, scheduled_at)
                VALUES (%(room_id)s, %(anchor_id)s, %(title)s, %(lifecycle)s, %(scheduled_at)s)
                ON CONFLICT (room_id)
                DO UPDATE SET
                    anchor_id = EXCLUDED.anchor_id,
                    title = EXCLUDED.title,
                    lifecycle = EXCLUDED.lifecycle,
                    scheduled_at = EXCLUDED.scheduled_at;
                """,
                {
                    "room_id": DEMO_ROOM_ID,
                    "anchor_id": DEMO_ANCHOR_ID,
                    "title": "LiveAgent Phase 2A 播前样例场",
                    "lifecycle": "PRE_LIVE",
                    "scheduled_at": datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc),
                },
            )

            for index, product in enumerate(DEMO_PRODUCTS, start=1):
                product_id, name, category, price, inventory, conversion_rate, commission_rate, tags, selling_points = product
                cursor.execute(
                    """
                    INSERT INTO live_agent_products(
                        product_id, name, category, price, inventory,
                        conversion_rate, commission_rate, tags, selling_points, is_active
                    )
                    VALUES (
                        %(product_id)s, %(name)s, %(category)s, %(price)s, %(inventory)s,
                        %(conversion_rate)s, %(commission_rate)s, %(tags)s, %(selling_points)s, TRUE
                    )
                    ON CONFLICT (product_id)
                    DO UPDATE SET
                        name = EXCLUDED.name,
                        category = EXCLUDED.category,
                        price = EXCLUDED.price,
                        inventory = EXCLUDED.inventory,
                        conversion_rate = EXCLUDED.conversion_rate,
                        commission_rate = EXCLUDED.commission_rate,
                        tags = EXCLUDED.tags,
                        selling_points = EXCLUDED.selling_points,
                        is_active = TRUE,
                        updated_at = NOW();
                    """,
                    {
                        "product_id": product_id,
                        "name": name,
                        "category": category,
                        "price": Decimal(price),
                        "inventory": inventory,
                        "conversion_rate": Decimal(conversion_rate),
                        "commission_rate": Decimal(commission_rate),
                        "tags": Jsonb(tags),
                        "selling_points": Jsonb(selling_points),
                    },
                )
                cursor.execute(
                    """
                    INSERT INTO live_agent_room_products(room_id, product_id, display_order)
                    VALUES (%(room_id)s, %(product_id)s, %(display_order)s)
                    ON CONFLICT (room_id, product_id)
                    DO UPDATE SET display_order = EXCLUDED.display_order;
                    """,
                    {"room_id": DEMO_ROOM_ID, "product_id": product_id, "display_order": index},
                )
        connection.commit()
    return SeedResult(anchor_count=1, room_count=1, product_count=len(DEMO_PRODUCTS))
