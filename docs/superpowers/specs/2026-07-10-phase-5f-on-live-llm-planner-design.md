# Phase 5F：播中 LLM Planner 设计文档

## 概述

Phase 5F 把播中 Agent 的决策从"确定性规则"升级为"LLM 驱动决策"。
当 LLM 可用时，弹幕和库存事件的决策由 DeepSeek（deepseek-v4-flash）生成；LLM 不可用或失败时自动降级到 Phase 5C 的确定性规则。

## 架构

```text
播中上下文（弹幕聚合 + 库存告警 + 信任分 + 记忆偏好）
  -> OnLiveLLMPlanner.plan()
     ├── LLM 可用 → DeepSeek chat completions → JSON 决策
     └── LLM 不可用/失败 → _rule_fallback()（确定性规则降级）
  -> planner_route（direct_plan / finish）
  -> execute_tools / END
```

## 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| LLM 调用方式 | urllib（标准库） | 不加新依赖，复用 Phase 3E 模式 |
| 模型 | deepseek-v4-flash | 已在用，响应快、成本低 |
| 失败策略 | 降级到规则 | 不阻塞播中流程 |
| prompt 构造 | 函数式 build_on_live_prompt() | 便于测试和调试 |
| 集成方式 | OnLiveLLMPlanner 替换 _planner_node 中的规则逻辑 | 最小侵入 |

## 向后兼容

- `build_on_live_agent_graph(planner=None)` 走旧规则（_DefaultPlanner）
- `build_on_live_agent_graph(planner=OnLiveLLMPlanner())` 走 LLM 决策
- 旧的 `_DefaultPlanner` 保持不变
