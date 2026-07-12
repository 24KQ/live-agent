# LiveAgent Skill Runtime 包初始化
#
# 本包承载 Phase 11A 受控 Skill Runtime，包括模型定义、Manifest Catalog、
# 统一 SkillExecutor、播前 Handler、路由策略和兼容适配层。
#
# ToolRegistry 将逐步降级为由 Manifest 生成的只读兼容投影；
# AgentToolExecutor 保留同步外观，但四个核心工具通过兼容适配层
# 委托统一 SkillExecutor 执行。

__all__: list[str] = []
