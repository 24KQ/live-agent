"""Phase 6A 前端种子数据脚本。

写入弹幕聚合、决策记录，确保前端有数据可展示。
幂等：可重复运行不报错。

用法：
    python scripts/seed_frontend_data.py
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone, timedelta

from src.config.settings import get_settings
from psycopg.types.json import Jsonb
from decimal import Decimal


SEED_ROOM = "room-dashboard-001"
SEED_TRACE = "trace-dashboard-seed"


def seed_danmaku_aggregates(conn):
    """写入弹幕聚合种子数据。"""
    base_time = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
    seeds = [
        ("price", "价格相关问题", 12, ["这个多少钱", "价格是多少", "有没有优惠"]),
        ("stock", "库存相关问题", 8, ["还有库存吗", "卖完了吗", "什么时候补货"]),
        ("promotion", "优惠活动相关问题", 5, ["有券吗", "满减多少", "赠品是什么"]),
    ]

    for cat, summary, count, samples in seeds:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM live_agent_danmaku_aggregates "
                "WHERE room_id = %(room_id)s AND category = %(cat)s AND summary = %(summary)s",
                {"room_id": SEED_ROOM, "cat": cat, "summary": summary},
            )
            if cur.fetchone():
                continue

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO live_agent_danmaku_aggregates "
                "(room_id, trace_id, category, summary, count, sample_contents, window_start, window_end) "
                "VALUES (%(room_id)s, %(trace_id)s, %(cat)s, %(summary)s, %(count)s, "
                "%(samples)s::jsonb, %(ws)s, %(we)s)",
                {
                    "room_id": SEED_ROOM,
                    "trace_id": SEED_TRACE,
                    "cat": cat,
                    "summary": summary,
                    "count": count,
                    "samples": Jsonb(samples),
                    "ws": base_time - timedelta(minutes=5),
                    "we": base_time,
                },
            )
    conn.commit()
    print(f"  Danmaku aggregates seeded: {len(seeds)} groups")



def ensure_anchor(conn):
    """确保 anchor 表有数据。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM live_agent_anchors WHERE anchor_id = 'anchor-demo'"
        )
        if cur.fetchone():
            return
        cur.execute(
            "INSERT INTO live_agent_anchors (anchor_id, display_name) "
            "VALUES ('anchor-demo', '演示主播')"
        )
    conn.commit()
    print("  Anchor created: anchor-demo")



def ensure_room(conn):
    """确保 live_rooms 和 room_products 表有数据。"""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM live_agent_live_rooms WHERE room_id = %s", (SEED_ROOM,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO live_agent_live_rooms (room_id, anchor_id, title, lifecycle, scheduled_at) "
                "VALUES (%s, 'anchor-demo', '演示直播间', 'ON_LIVE', NOW())",
                (SEED_ROOM,)
            )
            conn.commit()
            print(f"  Room created: {SEED_ROOM}")


def seed_decision_traces(conn):
    """写入决策记录种子数据。"""
    base_time = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
    seeds = [
        ("accepted", "good", Decimal("0.05"), Decimal("0.03"), Decimal("0.75")),
        ("accepted", "good", Decimal("0.05"), Decimal("0.02"), Decimal("0.76")),
        ("rejected", "anchor_right", Decimal("-0.05"), Decimal("0.00"), Decimal("0.74")),
        ("accepted", "bad", Decimal("-0.10"), Decimal("0.01"), Decimal("0.72")),
        ("rejected", "agent_right", Decimal("0.03"), Decimal("0.00"), Decimal("0.73")),
    ]

    for i, (action, result, delta, lift, final) in enumerate(seeds):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM live_agent_decision_trace "
                "WHERE room_id = %(room_id)s AND trace_id = %(trace_id)s "
                "AND anchor_action = %(action)s AND business_result = %(result)s",
                {
                    "room_id": SEED_ROOM,
                    "trace_id": f"{SEED_TRACE}-{i}",
                    "action": action,
                    "result": result,
                },
            )
            if cur.fetchone():
                continue

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO live_agent_decision_trace "
                "(trace_id, anchor_id, room_id, recommendation, anchor_action, business_result, "
                "lift, trust_delta, final_trust_score, created_at) "
                "VALUES (%(trace_id)s, %(anchor)s, %(room_id)s, %(rec)s::jsonb, %(action)s, %(result)s, "
                "%(lift)s, %(delta)s, %(final)s, %(created)s)",
                {
                    "trace_id": f"{SEED_TRACE}-{i}",
                    "anchor": "anchor-demo",
                    "room_id": SEED_ROOM,
                    "rec": Jsonb({"summary": "播中建议"}),
                    "action": action,
                    "result": result,
                    "lift": lift,
                    "delta": delta,
                    "final": final,
                    "created": base_time - timedelta(minutes=30 - i * 5),
                },
            )
    conn.commit()
    print(f"  Decision traces seeded: {len(seeds)} records")


def ensure_room_products(conn):
    """确保 room 在产品关联表中有数据。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM live_agent_room_products WHERE room_id = %(room_id)s",
            {"room_id": SEED_ROOM},
        )
        if cur.fetchone():
            print("  Room products already exist, skipping")
            return

        # 先确认产品表有数据
        cur.execute("SELECT product_id FROM live_agent_products LIMIT 3")
        products = cur.fetchall()
        if not products:
            from src.skills.product_catalog import ProductCatalogRepository
            from src.config.settings import get_settings
            s = get_settings()
            repo = ProductCatalogRepository(s)
            catalog = repo.list_room_products("room-001")
            for p in catalog:
                cur.execute(
                    "INSERT INTO live_agent_products "
                    "(product_id, name, category, price, tags, inventory) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (product_id) DO NOTHING",
                    (p.product_id, p.name, p.category, float(p.price), p.tags, p.inventory),
                )
            products = cur.fetchall()
            if not products:
                cur.execute("SELECT product_id FROM live_agent_products LIMIT 3")
                products = cur.fetchall()

        for row in products:
            cur.execute(
                "INSERT INTO live_agent_room_products (room_id, product_id, display_order) "
                "VALUES (%s, %s, 1) ON CONFLICT DO NOTHING",
                (SEED_ROOM, row["product_id"]),
            )
        conn.commit()
        print(f"  Room products seeded for room {SEED_ROOM}")


def main():
    import psycopg
    from psycopg.rows import dict_row

    settings = get_settings()
    print(f"Seeding frontend data for room: {SEED_ROOM}")

    with psycopg.connect(**settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
        seed_danmaku_aggregates(conn)
        ensure_anchor(conn)
        ensure_room(conn)
        seed_decision_traces(conn)
        ensure_room_products(conn)

    print("Done.")


if __name__ == "__main__":
    main()
