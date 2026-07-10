# Phase 5E: Agent 接通本地真实服务设计

## 日期

2026-07-10

## 背景

Phase 5C 播中 Agent 使用 _DefaultExecutor 返回模拟结果。Phase 5E 将其替换为
_LocalServiceExecutor，真正调用 OnLiveFlowService / DanmakuFlowService。

## 设计目标

1. 播中 Agent 真正调起本地业务流程
2. 向后兼容 -- 不传 service 时退回 _DefaultExecutor
3. 不加新依赖，复用现有 service

## 架构

Agent Graph -> execute_tools -> _LocalServiceExecutor
  handle_sold_out_event -> OnLiveFlowService.handle_sold_out_event()
  recommend_backup -> recommend_backup_product()
  generate_on_live_prompt -> generate_sold_out_prompt()
  aggregate_danmaku_questions -> DanmakuFlowService.handle_danmaku_batch()
