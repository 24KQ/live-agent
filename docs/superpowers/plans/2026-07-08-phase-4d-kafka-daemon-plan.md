# Phase 4D: Kafka 守护进程 + 弹幕聚合持久化实施计划
（详细版）

## Summary

Phase 4D 解决两个问题：

1. Kafka consumer 从一次性消费升级为守护进程——可长期运行、自动提交 offset、优雅关闭
2. 弹幕聚合结果持久化——5s 窗口聚合写入 PostgreSQL，副屏弹幕 API 改为读库

核心设计决策：不存原始弹幕，只存聚合结果。
10w+ 直播间每秒可能数百条弹幕，全量存 PostgreSQL 不现实也无必要。

## 涉及文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| docker/init_phase4_danmaku_aggregates.sql | 新增 | 建表脚本 |
| src/gateway/kafka_daemon.py | 新增 | 守护进程核心逻辑 |
| src/gateway/api_server.py | 修改 | 弹幕端点改读库 |
| scripts/run_kafka_daemon.py | 新增 | 启动守护进程 |
| scripts/run_kafka_daemon_demo.py | 新增 | 端到端演示 |
| tests/unit/test_kafka_daemon.py | 新增 | 单元测试 |
| tests/integration/test_kafka_daemon_flow.py | 新增 | 集成测试 |

## Key Changes

### 1. 数据库：live_agent_danmaku_aggregates 表

CREATE TABLE IF NOT EXISTS live_agent_danmaku_aggregates (
    id SERIAL PRIMARY KEY,
    room_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    count INTEGER NOT NULL CHECK (count > 0),
    sample_contents JSONB NOT NULL DEFAULT '[]'::jsonb,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_danmaku_aggr_room_time
    ON live_agent_danmaku_aggregates(room_id, window_start DESC);

### 2. 守护进程 src/gateway/kafka_daemon.py

DanmakuDaemon 类：
- run_forever() 主循环：
  1. poll() 拉取一批弹幕消息
  2. 解析为 DanmakuEvent（复用现有模型）
  3. 累积到当前 5s 窗口 buffer
  4. 窗口到期时调用 aggregate_and_persist()
  5. 写入 PostgreSQL（批量 INSERT）
  6. 提交 offset
  7. 清空 buffer，进入下一个窗口
- aggregate_and_persist()：复用现有 aggregate_danmaku_questions()
- graceful_shutdown()：监听 SIGINT/SIGTERM，完成当前窗口后退出
- 每处理完一个窗口调用 consumer.commit()

### 3. 副屏弹幕 API 升级

/api/danmaku/summary?room_id=xxx
- 改为从 live_agent_danmaku_aggregates 读取
- 按 window_start DESC 取最近 50 条
- 无数据时返回空列表 question_groups: []
- 移除所有模拟弹幕代码

### 4. CLI 演示

scripts/run_kafka_daemon.py：启动守护进程，Ctrl+C 停止
scripts/run_kafka_daemon_demo.py：
  1. 用 kafka-python 生产者发送 10 条测试弹幕
  2. 启动 DanmakuDaemon 消费
  3. 查询 PostgreSQL 验证有聚合记录
  4. 输出聚合结果

## 不做

- 不存原始弹幕（只存聚合结果）
- 不做 WebSocket 实时推送
- 不做售罄事件守护进程（保持一次性消费）
- 不做多线程（单线程 poll + 聚合 + 写库够用）

## Test Plan

### 单元测试 tests/unit/test_kafka_daemon.py
- 启动后能正常进入主循环
- 空消息不写库
- 5s 窗口到期后聚合结果写入数据库
- 优雅关闭不丢数据

### 集成测试 tests/integration/test_kafka_daemon_flow.py
- 生产端发送 10 条弹幕
- 守护进程消费并写入库
- 库中有聚合记录且 count 正确

## 验收命令

pytest tests/unit/test_kafka_daemon.py -v
pytest tests/integration/test_kafka_daemon_flow.py -v
pytest -v
python scripts/run_kafka_daemon_demo.py

## Assumptions

- Kafka 在 9092 端口运行
- 不接 LLM、不做 Web 前端、不接真实平台 API
- 新增代码 UTF-8 + 中文注释
- 提交信息：feat: add phase 4d kafka daemon with danmaku persistence
