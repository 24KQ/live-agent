# Phase 6A: 前端功能补全与数据可看化实施计划

## 日期

2026-07-10

## 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| front/index.html | 重写 | 五面板布局、Agent 建议、LLM 复盘 |
| src/gateway/api_server.py | 修改 | 新增 2 个 API 端点 |
| scripts/seed_frontend_data.py | 新增 | 种子脚本 |
| scripts/run_frontend.ps1 | 新增 | 一键启动 |
| tests/unit/test_api_server_extended.py | 新增 | 4 个测试 |

## TDD 结果

4 红 -> 4 绿

## 验收命令

```powershell
pytest tests/unit/test_api_server_extended.py -v
pytest -v
python scripts/seed_frontend_data.py
python -m uvicorn src.gateway.api_server:app --port 8100
```
