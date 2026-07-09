# Phase 5B: 弹幕语义聚合增强设计

## 日期

2026-07-10

## 背景

当前弹幕聚合完全依赖关键词规则。同一个意思的不同说法会被分到不同分类：

- "高呼价" — 包含"价"，命中 PRICE
- "多少钱" — 包含"多少钱"，命中 PRICE
- "这个几米" — 未命中任何关键词，归入 GENERAL

这导致聚合结果碎片化，GENERAL 分类堆积大量有实际含义但关键词覆盖不到的弹幕。

## 目标

在关键词聚合基础上增加两层兜底，不替代现有聚合：

1. **Embedding 语义聚类** — 将语义相似的未分类弹幕自动归簇
2. **LLM 低频兜底** — 规则+embedding 都无法归类的零散弹幕，批量交给 LLM

## 架构

```
弹幕事件
  → 关键词分类（现有）
  → GENERAL 弹幕 → embedding 语义聚类（新增）
    → 簇内多数归入同一类别 → 采纳该类别
    → LLM 兜底归类（低频批量）
  → 合并结果输出
```

## 组件

### 1. DanmakuSemanticClusterer

- 输入：一批弹幕文本 + 相似度阈值
- 流程：生成 embedding → 两两余弦相似度 → 相似度 >= threshold 归簇
- 输出：ClusterResult 列表

### 2. DanmakuLLMFallback

- 输入：未分类弹幕列表 + 现有分类列表
- 仅在列表 >= 5 条时触发 LLM
- 输出：[(message, category)]
- LLM 不可用 → 全部标记 general

### 3. aggregate_with_semantic_fallback

- 包装现有 aggregate_danmaku_questions
- 对 GENERAL 弹幕运行语义聚类 + LLM 兜底
- 不改变现有函数签名
