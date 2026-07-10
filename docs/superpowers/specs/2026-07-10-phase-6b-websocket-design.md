# Phase 6B: WebSocket 实时推送副屏设计

## 日期

2026-07-10

## 背景

Phase 6A 前端使用 HTTP 轮询拉取数据。改为 WebSocket 后在数据变化时主动推送。

## 架构

前端 WebSocket -> ws://localhost:8100/ws
后端:
  后台任务 1 (5s): Agent 建议 -> broadcast agent_suggestion
  后台任务 2 (10s): 弹幕摘要 -> broadcast danmaku_update
  后台任务 3 (10s): 告警 -> broadcast alert_update
  后台任务 4 (30s): 复盘 -> broadcast review_update

无连接时不运行轮询，不消耗数据库。
