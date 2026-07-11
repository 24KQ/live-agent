# -*- coding: utf-8 -*-
"""Phase 9A Kafka 弹幕模拟生产者。

向 Kafka 的 anchor.danmaku 和 anchor.inventory topic 定期发送
结构化模拟事件，供 DanmakuDaemon 消费。

三种播中场景：
- normal: 正常弹幕混合，无库存告警
- price_spike: 大量价格相关弹幕，无库存告警
- inventory_alert: 正常弹幕 + 商品售罄事件

用法：
    python scripts/run_simulator.py
    python scripts/run_simulator.py --scenario price_spike --interval 2
    python scripts/run_simulator.py --scenario inventory_alert
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

from kafka import KafkaProducer

from src.config.settings import get_settings


# 弹幕内容模板，按场景分类
_DANMAKU_TEMPLATES: dict[str, list[str]] = {
    "price": [
        "这个多少钱",
        "太贵了吧",
        "有优惠券吗",
        "券后价多少",
        "还能再便宜点吗",
        "满减活动有吗",
        "比其他平台贵",
        "什么时候打折",
    ],
    "quality": [
        "质量怎么样",
        "会不会褪色",
        "面料好吗",
        "耐穿吗",
        "洗了会缩水吗",
        "做工怎么样",
    ],
    "stock": [
        "还有库存吗",
        "什么时候补货",
        "会不会断货",
        "限量吗",
        "还能拍吗",
    ],
    "general": [
        "主播穿的是什么码",
        "适合什么身材",
        "有黑色吗",
        "发货时间多久",
        "支持七天无理由吗",
        "主播好漂亮",
        "刚进直播间",
        "这个推荐给男生还是女生",
    ],
}


def _build_danmaku_content(scenario: str) -> str:
    """根据场景选择弹幕内容模板。

    normal: 均匀混合各类弹幕
    price_spike: 80% 价格相关弹幕
    inventory_alert: 正常弹幕，与 normal 一致（库存事件单独发）
    """
    if scenario == "price_spike":
        if random.random() < 0.8:
            return random.choice(_DANMAKU_TEMPLATES["price"])
        pool = _DANMAKU_TEMPLATES["quality"] + _DANMAKU_TEMPLATES["general"]
        return random.choice(pool)

    # normal 和 inventory_alert 走均匀分布
    all_templates = (
        _DANMAKU_TEMPLATES["price"]
        + _DANMAKU_TEMPLATES["quality"]
        + _DANMAKU_TEMPLATES["stock"]
        + _DANMAKU_TEMPLATES["general"]
    )
    return random.choice(all_templates)


def _build_danmaku_message(room_id: str, trace_id: str, scenario: str) -> dict:
    """构造一条 DanmakuEvent 兼容的 Kafka 消息。"""
    return {
        "room_id": room_id,
        "viewer_id": f"viewer-{random.randint(1, 100):03d}",
        "content": _build_danmaku_content(scenario),
        "event_time": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace_id,
    }


def _build_inventory_message(room_id: str, trace_id: str) -> dict:
    """构造一条库存售罄事件。"""
    return {
        "room_id": room_id,
        "product_id": f"p{random.randint(1, 5):03d}",
        "event_type": "sold_out",
        "trace_id": trace_id,
    }


class DanmakuSimulator:
    """弹幕模拟生产者。"""

    def __init__(
        self,
        interval_seconds: int = 3,
        kafka_servers: str | None = None,
    ) -> None:
        settings = get_settings()
        self._interval = interval_seconds
        self._room_id = "room-sim-001"
        self._running = False

        servers = kafka_servers or settings.kafka_bootstrap_servers
        self._producer = KafkaProducer(
            bootstrap_servers=servers,
            value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
        )
        self._danmaku_topic = settings.kafka_topics["danmaku"]
        self._inventory_topic = settings.kafka_topics["inventory"]

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame) -> None:
        """收到中断信号后优雅关闭。"""
        print(f"[Simulator] 收到关闭信号，正在停止...")
        self._running = False

    def run(self, scenario: str = "normal", batch_size: int = 10) -> None:
        """按场景循环发送事件。

        每 interval 秒发送一批（batch_size 条弹幕），
        inventory_alert 场景在第一批发完后额外发送一条库存售罄事件。
        """
        if scenario not in ("normal", "price_spike", "inventory_alert"):
            print(f"[Simulator] 未知场景: {scenario}，使用 normal")
            scenario = "normal"

        print(f"[Simulator] 启动 (scenario={scenario}, interval={self._interval}s, topic={self._danmaku_topic})")
        print(f"[Simulator] 按 Ctrl+C 停止")
        print()

        self._running = True
        batch_index = 0

        try:
            while self._running:
                trace_id = f"sim-{uuid4().hex[:12]}"
                batch_index += 1

                # 发送一批弹幕
                danmaku_count = random.randint(5, batch_size)
                for _ in range(danmaku_count):
                    msg = _build_danmaku_message(self._room_id, trace_id, scenario)
                    self._producer.send(self._danmaku_topic, value=msg)

                # inventory_alert 场景：第一批发送后发一条售罄事件
                if scenario == "inventory_alert" and batch_index == 1:
                    inv_msg = _build_inventory_message(self._room_id, trace_id)
                    self._producer.send(self._inventory_topic, value=inv_msg)
                    print(f"  [售罄] {inv_msg['product_id']} 已发送到 {self._inventory_topic}")

                self._producer.flush()
                print(f"  [批次 {batch_index}] 发送 {danmaku_count} 条弹幕 (trace={trace_id[:8]}...)")

                time.sleep(self._interval)

        except KeyboardInterrupt:
            pass
        finally:
            self.graceful_shutdown()

    def graceful_shutdown(self) -> None:
        """清理 producer 连接。"""
        self._running = False
        try:
            self._producer.flush(timeout=3)
            self._producer.close(timeout=3)
        except Exception:
            pass
        print("[Simulator] 已停止")


def main() -> int:
    parser = argparse.ArgumentParser(description="Kafka 弹幕模拟生产者")
    parser.add_argument("--interval", type=int, default=3, help="发送间隔（秒）")
    parser.add_argument("--scenario", default="normal", choices=["normal", "price_spike", "inventory_alert"], help="播中场景")
    parser.add_argument("--batch-size", type=int, default=10, help="每批弹幕数量")
    args = parser.parse_args()

    sim = DanmakuSimulator(interval_seconds=args.interval)
    sim.run(scenario=args.scenario, batch_size=args.batch_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
