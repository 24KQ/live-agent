# Phase 4C: Web 副屏数据源真实化实施计划

## Summary

Phase 4B 搭好了副屏骨架（FastAPI + 前端），但 4 个数据端点全部使用模拟数据。
Phase 4C 的目标是把副屏 API 从硬编码 return 切换到真实 PostgreSQL 数据源，
让副屏真正展示主播实时业务数据。

### 当前模拟点 vs 真实数据源

| API 端点 | 当前问题 | 真实数据源 |
|----------|----------|------------|
| /api/card/{product_id} | 硬编码 CatalogProduct | PostgreSQL live_agent_products 表 |
| /api/danmaku/summary | 2 条本地构造的 DanmakuEvent | 保持模拟，标注 TODO |
| /api/alert/{room_id} | 固定返回 1 条告警 | PostgreSQL live_agent_products 查库存 |
| /api/review/{room_id} | 内存模拟 2 条 traces | PostgreSQL live_agent_decision_trace 表 |

## Key Changes

### 1. 手卡端点数据库化
- /api/card/{product_id} 改为调用 ProductCatalogRepository.list_room_products
  从 live_agent_products 表读取真实商品
- 查询不到时返回 404

### 2. 告警端点数据库化
- /api/alert/{room_id} 查询 room 关联的所有商品
- 筛选 inventory < 30 或 inventory = 0（售罄）的商品
- 按 inventory 升序，库存最低排最前面
- 无告警时返回空列表

### 3. 复盘端点数据库化
- /api/review/{room_id} 查询 live_agent_decision_trace 中该 room_id 的所有记录
- 用 PostLiveAttribution.calculate 计算归因指标
- 按 created_at 降序排列

### 4. 弹幕端点保持模拟
- 弹幕数据未持久化到 PostgreSQL
- 标注 TODO: Phase 4D/5 接 Kafka 长期消费后升级

## Test Plan

- 手卡端点 404 场景
- 告警端点空库存返回空列表
- 全量 pytest 不变

## 验收命令

pytest tests/unit/test_api_server.py -v
pytest -v
