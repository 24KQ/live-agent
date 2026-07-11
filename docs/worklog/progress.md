# LiveAgent 工作进度记录

## 2026-07-11

- 启动文档编码治理，目标是先防止继续乱码，再处理已有风险文档。
- 新增只读编码扫描脚本 `scripts/check_doc_encoding.py`。
- 新增编码规范文档 `docs/project_guidance/document_encoding_policy.md`。
- 将 `docs/worklog/` 从忽略目录调整为可追踪工作日志目录。
- 明确后续文档写入规范：优先 `apply_patch`，避免 PowerShell heredoc / 管道写大段中文。
- 追加项目状态和阶段执行日志，确保后续迭代能看到本次治理背景。

## 下一步

- 每个阶段完成后，继续按“测试记录、反馈、遗留限制、后续迭代方向”四类信息补充留迹。
- 如果继续推进 Phase 6C/Phase 7，需要先确认文档编码扫描通过。
# 2026-07-11 Phase 7A 进度

- 完成 Phase 6C 功能提交和编码治理提交，避免 7A 改动混入历史收尾。
- 新增 Agent Replay、规则评分、评估 Store、Worker、LLM Judge、API 和 `/evaluation` 页面。
- 新增 PostgreSQL 评估表，使用任务租约和 `FOR UPDATE SKIP LOCKED` 支持多 Worker 抢占。
- 将真实 DeepSeek 集成测试标记为 `external`，默认测试不访问外部模型。
- 当前 7A 聚焦测试、全量 unit、全量 pytest、demo、编码扫描和 diff 检查均已通过。

---
