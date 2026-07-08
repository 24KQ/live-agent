# Phase 4B Web 副屏界面 — 设计文档

## 概述

为 LiveAgent 项目搭建 Web 副屏界面，主播在直播时可通过浏览器（iPad 横屏或桌面端）实时查看 AI 助手建议。

## 架构

FastAPI (port 8100) -> REST API -> 现有 Service 层 -> PostgreSQL
                      |
                      +-> 静态文件服务 -> front/index.html (副屏主界面)

## API 端点

| 端点 | 方法 | 用途 | 数据来源 |
|------|------|------|----------|
| /api/health | GET | 健康检查 | — |
| /api/card/{product_id} | GET | 当前商品讲解手卡 | LLMCardGenerator |
| /api/danmaku/summary?room_id= | GET | 弹幕聚合摘要 | DanmakuAggregator |
| /api/alert/{room_id} | GET | 库存/售罄告警 | 模拟数据（后续接 Reducer） |
| /api/review/{room_id} | GET | 播后复盘报告 | PostLiveAttribution + PostLiveReview |
| / | GET | 副屏 HTML | 静态文件 |

## 前端设计

- 深色主题（#0d1117 背景），适配直播间暗环境
- 固定 1280x800 布局（iPad 横屏比例）
- 四象限：手卡（左上）、弹幕（右上）、告警（左下）、复盘（右下）
- 轮询策略：弹幕+告警 10s、复盘 30s、手卡仅手动刷新
- 纯 HTML + Vanilla JS，无前端框架

## 技术选型

- FastAPI + uvicorn（异步、轻量）
- 前端零依赖
- 各端点复用现有 service，不做新业务逻辑

## 不在本阶段范围

- WebSocket 实时推送（后续升级）
- React/Vue 框架
- 真实淘宝/抖音 API 对接
- LLM 驱动的话术生成（Phase 3E 已完成）
