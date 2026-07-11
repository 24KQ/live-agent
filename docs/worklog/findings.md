# LiveAgent 工作发现记录

## 2026-07-11 文档编码治理发现

- 乱码问题需要区分两类：终端显示乱码，以及文件内容已经被写坏。
- 之前的风险主要来自 PowerShell heredoc / 管道写入大段中文，终端编码和文件编码不一致时容易把乱码写回文件。
- 当前项目重点留迹文档位于 `docs/project_guidance/`，过程记录位于 `docs/worklog/`。
- `docs/worklog/` 之前被 `.gitignore` 忽略，不利于后续项目迭代留迹回放。
- 本阶段将 `docs/worklog/` 改为可追踪目录，但不记录真实密钥、token、`.env` 内容或本机私密路径。

## 当前治理结论

- 优先使用 `apply_patch` 修改中文文档。
- 不再使用 PowerShell heredoc / 管道写入大段中文。
- 不把终端中已经乱码的内容复制回 Markdown 文件。
- 已新增 `scripts/check_doc_encoding.py` 作为只读扫描工具。
- 后续阶段收尾时应同时运行编码扫描和 `git diff --check`。

## 后续观察点

- 如果扫描脚本出现高置信 mojibake 命中，应先人工确认上下文，再决定从 git 历史恢复还是重写。
- 如果 VS Code 显示正常但终端显示异常，优先调整终端编码，不要修改文件内容。
- 如果某个历史文档已经无法恢复，应按当前项目事实重写摘要，不做盲目转码。
# 2026-07-11 Phase 7A 发现

- 生产级 Agent 项目不能只证明“能跑”，还要能回放、评分和复核，否则很难解释 Agent 决策是否可靠。
- Replay 不能只依赖 LangGraph checkpoint；checkpoint 适合恢复状态，业务评估还需要 Harness session、ToolCallAudit 和 DecisionTrace 作为证据。
- 规则评分必须先于 LLM Judge。安全、人审和工具合规不能交给 LLM 改判。
- 外部模型测试需要显式标记，默认测试使用 fake HTTP，避免网络、额度和模型波动污染工程验收。
- 运维页面也要按生产标准处理持久化数据，不能因为是内部页面就用 `innerHTML` 直接拼接 replay 字段。
- 评估任务的汇总和维度明细必须事务一致，否则会产生“任务完成但证据缺失”的排障陷阱。

---
