# Phase 5C: 播中 Agent 动态决策小循环实施计划

## 日期

2026-07-10

## 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| src/core/on_live_agent_graph.py | 新增 | 播中 Agent LangGraph |
| src/core/agent_tool_executor.py | 修改 | 扩展 ON_LIVE 工具 |
| scripts/run_phase5c_on_live_agent_demo.py | 新增 | CLI 演示 |
| tests/unit/test_on_live_agent_graph.py | 新增 | 7 个单元测试 |

## TDD 结果

7 红 → 7 绿，全部一次通过。

## 验收命令

```powershell
pytest tests/unit/test_on_live_agent_graph.py -v
pytest -v
python scripts/run_phase5c_on_live_agent_demo.py
git status --short --ignored
git add -n .
```
