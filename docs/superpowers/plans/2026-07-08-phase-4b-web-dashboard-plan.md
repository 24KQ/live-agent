# Phase 4B Web 副屏界面 — 实施计划

## 任务清单

- [x] FastAPI app 创建 + 5 个端点
- [x] 前端 index.html（深色主题、四象限布局、轮询）
- [x] 单元测试（5 个端点）
- [x] 全量 pytest 验证
- [x] 设计文档
- [x] phase_execution_log.md 留迹

## 依赖

- fastapi>=0.100.0
- uvicorn>=0.30.0

## 测试

- tests/unit/test_api_server.py: 5 tests
- 全量测试: 198 passed, 0 failed

## 验收

- pytest -v 通过
- python -m uvicorn src.gateway.api_server:app --port 8100 可启动
- 浏览器 http://localhost:8100 可访问副屏
