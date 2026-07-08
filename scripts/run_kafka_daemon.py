# -*- coding: utf-8 -*-
"""Phase 4D Kafka 弹幕守护进程启动脚本。Ctrl+C 优雅关闭。"""
from src.gateway.kafka_daemon import DanmakuDaemon
if __name__ == "__main__":
    daemon = DanmakuDaemon()
    daemon.run_forever()
