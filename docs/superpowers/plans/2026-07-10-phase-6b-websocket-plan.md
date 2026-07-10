# Phase 6B: WebSocket 实时推送副屏实施计划

## 日期

2026-07-10

## 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| src/gateway/websocket_manager.py | 新增 | WebSocket 管理器 |
| src/gateway/api_server.py | 修改 | 加 WS 端点和后台任务 |
| front/index.html | 修改 | 轮询改 WebSocket |
| scripts/run_phase6b_ws_demo.py | 新增 | 推送验证 |
| tests/unit/test_websocket_manager.py | 新增 | 7 个测试 |

## TDD 结果

7 红 -> 7 绿

## 验收命令

```powershell
pytest tests/unit/test_websocket_manager.py -v
pytest -v
python -m uvicorn src.gateway.api_server:app --port 8100
# 浏览器打开 http://localhost:8100
# 打开开发者工具 -> Network -> WS -> 验证消息推送
```
