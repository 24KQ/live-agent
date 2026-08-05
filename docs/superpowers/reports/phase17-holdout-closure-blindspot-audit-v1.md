# Phase 17 Holdout Source Closure 盲区审计 v1

## 审计范围

- 审计对象：Phase 17 执行契约 `phase17-holdout-execution-v1` 的 21 路
  `PHASE17_HOLDOUT_SOURCE_CLOSURE_PATHS`。
- 当前候选契约 digest：`75ac54c821a0f30d4bf2fbeee59e1dba453b4a6ec299c5ca8a9a2c1625efcd81`。
- 当前 approved registry 仍为旧 digest；本候选未获用户批准前，不得真实联网。
- 审计方法：逐文件核对 prompt 形状示例、result schema、运行时校验器、测试
  fake 输出和跨模块输入投影；同时检查 dataset、capture、ledger、adapter、CLI
  和 SQL 边界。
- 审计约束：不修改 prompt、result schema、candidate profile、身份、案例、标签、
  阈值、预算、账本历史、`safety_reviews`、v2/v3 manifest 或 Phase 16 冻结闭包。

## 发现与修复

### 1. FINAL envelope 与 runner 校验器错位

冻结 Analyst/Planner profile 的 prompt 示例和 result schema 都要求：

```json
{"kind":"FINAL","final_output":{}}
```

原 Phase 17 runner 却按旧 v2 的顶层 `trigger_codes`、`analysis`、`risk_codes`、
`proposal` 字段判定，因此真实模型即使忠实遵循冻结 prompt，也会被记录为
`ANALYST_VALIDATION_FAILED` 或 `PLANNER_VALIDATION_FAILED`。本次修复在
`phase17_holdout_runner.py` 集中加入 `_final_output_mapping()`，先严格要求
`kind == "FINAL"`，再对 `final_output` 做阶段字段校验；旧 v2 形状继续
fail-closed。

Analyst 的 `constraint_codes` 和 `risk_codes` 在冻结 schema 中没有 `minItems`，
prompt 形状示例也明确使用空数组。因此校验器只要求这两个字段存在且为数组，
不擅自要求数组非空；`explanation` 必须为非空字符串，`evidence_ids` 必须是
非空字符串数组。Planner 的 `options` 按冻结 schema 要求非空数组。

### 2. Analyst 到 Planner 的输入投影错位

原 runner 把完整 Analyst envelope 嵌入 Planner 的 `analysis` 输入；Planner
prompt 读取的是 `analysis.risk_codes` 等内层结果字段。修复后 Planner 只收到
`final_output` 投影，避免再次产生“测试 fake 自洽、真实运行时字段层级错误”。

### 3. 修复验证覆盖

- integration fake 输出改为冻结 `FINAL/final_output` 结构，并增加 Planner 收到
  内层 analysis 的断言。
- 新增 runner 单元测试，直接从冻结 prompt 提取形状示例，核对 envelope、required
  字段集合和 validator；同时确认旧 v2 形状被拒绝、冻结允许的空 code 数组仍被接受。
- capture 单元测试使用同一冻结 envelope，避免 transport 测试继续伪造旧形状。

## 21 路闭包逐项清单

| 闭包文件 | 核对结果 | 证据与边界 |
| --- | --- | --- |
| `docker/init_phase17_holdout_ledger.sql` | 通过 | Phase 17 表族、append-only 触发器、attempt/case/safety review 所需列与 Python ledger 的写入边界一致；本次不改 SQL。 |
| `scripts/run_db_migrations.py` | 通过 | 统一 migration 入口只负责迁移安装；CLI 通过 schema readiness 检查，不绕过 migration；本次不改。 |
| `scripts/run_phase17_holdout.py` | 通过 | `--probe`/`--execute`/`--aggregate` 均先加载 approved contract、身份准入和 dataset/label 前置检查；batch 集合通过 manifest 公开 API 读取；本次未改变执行身份或阈值。 |
| `scripts/record_phase17_safety_review.py` | 通过 | review 只能以 `claude-independent-review` 入账，digest/verdict 进入 append-only ledger；不读取模型正文、不替代人工审查。 |
| `src/decision_support/controlled_e2e_adapter_v5.py` | 通过 | Phase 17 复用其受控重试/换端语义，但实际 adapter 是独立 `phase17_v5_adapter.py`；未发现 Phase 17 把 v2 历史入口误当新入口的字段错位。 |
| `src/decision_support/models.py` | 通过 | Phase 17 runner 对模型输出使用 `Mapping` 和 tuple 兼容冻结后的 `FrozenDict`/tuple；不会因冻结容器类型把合法 FINAL 输出误判为非对象。 |
| `src/decision_support/multi_agent.py` | 通过 | `_SMOKE_V2_CONFLICT_ANALYSIS_RESULT_SCHEMA` 与 Planner schema 提供冻结字段、required、minItems 和 additionalProperties 边界；本次只读取，不修改冻结 schema。 |
| `src/decision_support/phase16_qualification.py` | 通过 | Phase 17 contract loader 校验自摘要、21 路 source closure、approved registry、身份、预算和批次；v3 仍是回溯评价契约，无执行身份。 |
| `src/decision_support/phase16_qualification_candidate.py` | 通过 | Phase 17 profile 的 prompt 示例、result schema 和 profile digest 来自同一 builder；Analyst/Planner 均使用 `FINAL/final_output`，身份由 Phase 17 terra/high 常量绑定。 |
| `src/decision_support/phase16_qualification_execution_ledger.py` | 通过 | 这是历史 v2 execution ledger；Phase 17 runner 不调用其写入路径，避免新 holdout 混入 v2 账本。 |
| `src/decision_support/phase16_qualification_ledger.py` | 通过 | 历史 candidate/campaign 类型只用于构造兼容字段；Phase 17 预算、campaign、attempt、case 事实由独立 ledger 表族承载。 |
| `src/decision_support/phase16_qualification_runner.py` | 通过 | 这是 v2 历史 bounded runner，仍保留其历史 prompt/validator 语义；没有被 Phase 17 CLI 调用，本次不改。 |
| `src/decision_support/phase17_holdout_dataset.py` | 通过 | manifest digest、case-to-input digest、精确 batch 集合、dev 排除和输入/标签物理分离在 loader/validator/CLI 三处闭合；模型调用前先验证全部 case。 |
| `src/decision_support/phase17_holdout_capture.py` | 通过 | 每个真实 transport response body 独占写入 `_probe_artifacts/<run>/<case>/<stage>/attempt-N.body`，回读 SHA-256；capture 缺失阻断重试和后续准入。 |
| `src/decision_support/phase17_holdout_ledger.py` | 通过 | runner 写入逐 attempt response/artifact digest、tokens、cost、receipt 和 case 聚合；UNKNOWN_USAGE 使用 stage 预留，不重复使用 campaign 预留。 |
| `src/decision_support/phase17_holdout_runner.py` | 已修复 | 原有两处 v2 字段层级错位已修复：FINAL envelope 解包校验、Analyst 到 Planner 的内层投影；旧形状和未知 kind 仍拒绝。 |
| `src/specialist_runtime/deepseek_adapter.py` | 通过 | HTTP payload 的 model、json mode、response digest 和原始 body 事实边界一致；Phase 17 capture 装饰 transport 后再交回同一 response。 |
| `src/specialist_runtime/model_port.py` | 通过 | `ModelSuccess.output` 冻结 JSON、`response_digest` 和 usage 的类型边界与 runner 的 Mapping/tuple 处理一致；失败结果不伪造模型正文。 |
| `src/specialist_runtime/models.py` | 通过 | `_freeze_json`/`_plain_json` 的冻结与还原边界已由 runner 的 Planner projection 测试覆盖；JSON 序列化不会把 FrozenDict 当普通 dict 错读。 |
| `src/specialist_runtime/phase17_v5_adapter.py` | 通过 | Phase 17 adapter 收集真实 attempt 明细，要求环境 reasoning effort 与 contract 一致，并把响应摘要、artifact digest 和 capture 状态交给 runner。 |
| `src/specialist_runtime/profiles.py` | 通过 | endpoint/model/reasoning 白名单与 Phase 17 terra/high、`synapse-ai.uk` 身份准入一致；本次不扩大白名单。 |

## 盲区审计结论

本次发现的同类缺陷只有上述两处，根因都是“冻结 envelope 的字段层级与运行时
手写协议不一致”；它们已在同一 checkpoint 中修复并加入跨层测试。没有发现
dataset membership、capture digest、ledger attempt/case、adapter raw output、
CLI aggregate、migration/SQL 边界存在同类的内部自洽/真实错位。

仍需明确的设计边界：`_structure_valid()` 是 runner 的快速结构门，不是完整的
JSON Schema 或 Planner 领域语义验证器。完整 schema 的 additionalProperties、
枚举、字符串长度、option 内部字段和 evidence 归属目前没有由这个快速结构门
独立检查；本次不把它伪装成已经完成的完整 schema/语义验证，也没有在禁止修改
schema 的前提下新增约束。因此本 checkpoint 的通过测试只能证明 envelope/字段层级
对齐和既有受控链路通过，不能额外宣称 Phase 17 已完成一套独立的完整 JSON Schema
验证。若以后要让 Phase 17 runner 独立承担完整验证，必须另立变更、重新计算
source closure 和 contract digest。

## 放行结论

本 checkpoint 的新鲜离线 gate 原始输出保存在工作树未跟踪证据文件中：

| Gate | 命令结果 |
| --- | --- |
| 全量 unit | `1739 passed, 1 warning`，退出码 `0`，`_phase17_structure_fix_unit_output.txt` |
| 全量 integration | `295 passed, 7 deselected, 5 warnings`，退出码 `0`，`_phase17_structure_fix_integration_output.txt` |
| 文档编码 | `python -u scripts/check_doc_encoding.py --docs-only`，退出码 `0`，`_phase17_structure_fix_encoding_output.txt` |
| compileall | `python -m compileall -q src scripts tests`，退出码 `0`，`_phase17_structure_fix_compileall_output.txt` |
| diff 检查 | `git diff --check`，退出码 `0`，`_phase17_structure_fix_diff_check_output.txt` |
| 候选 `--probe` | `ADMISSION_OK`，退出码 `0`，明确显示候选 digest，`_phase17_structure_fix_probe_output.txt`；dry-run，无模型调用 |

上述测试均未调用真实模型。unit/integration 测试使用的是**测试进程内**的
候选 digest override；没有修改 `src/decision_support/phase17_approved_digest.py`，
也没有写入或还原 registry 文件。测试结束后的独立核对仍显示：候选 manifest
自摘要与 21 路源码 closure 一致，工作树 registry 保持旧批准值
`6cbb90299bdd961d228f47f67fce716ce9ed3f446020a923dffff6edb9004edb`。

代码修复完成后，只有在最终候选 digest 的全量 unit、Phase 17 integration、
encoding、compileall 和 `git diff --check` 全部如实通过，并经独立复算和用户
批准更新 registry 后，才可申请新的 batch1 run。历史 `0/10 BLOCKED`、`1.000000`
CNY 账本事实保持不变；本 checkpoint 不重跑历史 run、不修改 safety review、
不调用真实模型。
