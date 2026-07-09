# Phase 5D: LLM 播后复盘总结设计

## 日期

2026-07-10

## 背景

Phase 4A 已实现结构化播后复盘（PostLiveReview + PostLiveAttribution），但输出的报告是键值对格式。引入 LLM 后，能将归因数据转为自然语言总结。

## 设计目标

1. 在结构化复盘基础上生成自然语言播后总结
2. LLM 不可用时降级到结构化模板，不阻塞流程
3. 不修改现有 Phase 4A 代码

## 架构

归因数据 + 问题列表 -> build_review_prompt() -> LLMPostLiveSummary._call_llm() 调用 DeepSeek
  - 成功: 返回自然语言总结
  - 失败: build_structured_fallback() 降级

## 关键设计

- 总结分三部分: 本场概览、发现问题、后续建议
- 复用 Phase 3E 的 Settings（llm_api_key 等）
- 不加新依赖，使用 urllib
