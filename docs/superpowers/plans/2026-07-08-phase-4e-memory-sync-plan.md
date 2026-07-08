# Phase 4E: 记忆回写补全实施计划

## Summary

播后复盘得出的洞察回写到记忆层，形成完整闭环：

排品建议 -> 主播反馈 -> 播后复盘 -> 记忆回写 -> 信任更新 -> 下一次排品更准

现有资产可以复用：
- DecisionTraceMemoryFeedbackService（已存在，生成 L2 记忆）
- MemoryStore.write_memory()（已存在，写入数据库）
- TrustManager（已存在，更新 trust_score）
- DecisionTraceStore.list_traces()（已存在，读取决策记录）

缺的只是一个编排层：从 DB 读 traces -> 逐条生成反馈记忆 -> 批量写入 -> 触发信任更新。

## 涉及文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| src/skills/post_live_memory_sync.py | 新增 | 编排层：同步决策记录到记忆 |
| scripts/run_phase4e_memory_sync_demo.py | 新增 | 演示脚本 |
| tests/unit/test_post_live_memory_sync.py | 新增 | 单元测试 |
| tests/integration/test_post_live_memory_sync_flow.py | 新增 | 集成测试 |

## Key Changes

### 1. post_live_memory_sync.py

PostLiveMemorySyncService 类：
- sync_room_traces(anchor_id, room_id, trace_id)：从 DB 读取指定 trace 的决策记录
- 每条记录调用 DecisionTraceMemoryFeedbackService.build_feedback_memory()
- 使用 ProductCatalogRepository 获取货盘
- 写入 MemoryStore
- 调用 TrustManager 更新信任分
- 返回写入结果摘要（记忆数、信任变化）

### 2. 演示脚本

scripts/run_phase4e_memory_sync_demo.py：
- 初始化锚点和房间数据
- 读取 DB 中已有的 DecisionTrace 记录
- 执行同步
- 展示写入的记忆和信任变化

## Test Plan

- tests/unit/test_post_live_memory_sync.py：
  - 空 trace 不报错
  - 合法 trace 生成 L2 记忆
  - 信任分更新正确
- tests/integration/test_post_live_memory_sync_flow.py：
  - 发送决策记录 -> 同步 -> 验证记忆表和信任表

## 验收命令

pytest tests/unit/test_post_live_memory_sync.py -v
pytest tests/integration/test_post_live_memory_sync_flow.py -v
pytest -v
python scripts/run_phase4e_memory_sync_demo.py
git status --short --ignored
git add -n .
