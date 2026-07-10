# Phase 5F：播中 LLM Planner 实施计划

## 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/skills/on_live_llm_planner.py` | 新增 ✅ | OnLiveLLMPlanner 核心逻辑 |
| `src/core/on_live_agent_graph.py` | 修改 ✅ | _planner_node 集成 LLM 分支 |
| `scripts/run_phase5f_llm_planner_demo.py` | 新增 ✅ | CLI 演示 |
| `tests/unit/test_on_live_llm_planner.py` | 新增 ✅ | 11 个单元测试 |
| `docs/superpowers/specs/2026-07-10-phase-5f-on-live-llm-planner-design.md` | 新增 ✅ | 设计文档 |
| `docs/superpowers/plans/2026-07-10-phase-5f-on-live-llm-planner-plan.md` | 新增 ✅ | 实施计划 |

## 实施步骤

1. 创建 `test_on_live_llm_planner.py` ✅ RED 确认 → GREEN 11/11 passed
2. 创建 `on_live_llm_planner.py` ✅ 实现完成
3. 修改 `on_live_agent_graph.py` ✅ _planner_node 支持 LLM 分支
4. 创建 CLI 演示脚本 ✅ run_phase5f_llm_planner_demo.py
5. 创建设计/计划文档 ✅
6. 全量测试验证 ⏳

## 验收

```powershell
pytest tests/unit/test_on_live_llm_planner.py -v
pytest tests/unit/test_on_live_agent_graph.py -v
pytest -v
python scripts/run_phase5f_llm_planner_demo.py
git add -n .
```
