# Phase 5B: 弹幕语义聚合增强实施计划

## 日期

2026-07-10

## 涉及的代码文件

| 文件 | 操作 | 说明 |
|------|------|------|
| src/skills/danmaku_semantic_cluster.py | 新增 | 语义聚类器 |
| src/skills/danmaku_llm_fallback.py | 新增 | LLM 兜底分类器 |
| src/skills/danmaku_aggregator.py | 修改 | 新增 aggregate_with_semantic_fallback |
| scripts/run_phase5b_semantic_danmaku_demo.py | 新增 | CLI 演示 |
| tests/unit/test_danmaku_semantic_cluster.py | 新增 | 聚类器单元测试 |
| tests/unit/test_danmaku_llm_fallback.py | 新增 | LLM 兜底单元测试 |
| tests/unit/test_danmaku_aggregator_semantic.py | 新增 | 语义聚合增强测试 |

## TDD 顺序

1. test_danmaku_semantic_cluster.py（6 测试）→ DanmakuSemanticClusterer 实现
2. test_danmaku_llm_fallback.py（5 测试）→ DanmakuLLMFallback 实现
3. test_danmaku_aggregator_semantic.py（4 测试）→ aggregate_with_semantic_fallback 实现

## 验收命令

```powershell
pytest tests/unit/test_danmaku_semantic_cluster.py -v
pytest tests/unit/test_danmaku_llm_fallback.py -v
pytest tests/unit/test_danmaku_aggregator_semantic.py -v
pytest tests/unit/test_danmaku_aggregator.py -v
pytest -v
python scripts/run_phase5b_semantic_danmaku_demo.py
git status --short --ignored
git add -n .
```
