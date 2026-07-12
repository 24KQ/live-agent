# Phase 11A 受控 Skill Runtime Acceptance

- 状态：技术验收完成，待用户审核
- 验收日期：2026-07-12
- Design：[Phase 11A 受控 Skill Runtime Design](../specs/phase-11a-skill-runtime-design.md)
- Plan：[Phase 11A Skill Runtime Implementation Plan](../plans/2026-07-12-phase-11a-skill-runtime-plan.md)
- 验收代码基线：`35e3907`

## 1. 验收结论

Phase 11A 的契约门禁、行为门禁、两批独立路由、审批与幂等门禁、相关回归和默认全量测试均已取得可复核证据。同一四场景 Demo 的两个运行入口均完整执行，生产代码未实现 `SHADOW_COMPARE`、热加载、PlanEngine 或 Specialist Agent。

本结论仅表示技术验收完成。Acceptance 尚待用户审核，Phase 11B 保持未开始；用户审核前不得把本报告解释为用户已批准，也不得提前展开 Phase 11B 实施。

## 2. 实际交付

1. 以 13 个冻结 `SkillManifest` 作为工具元数据唯一事实源，由 `SkillCatalog` 生成 ToolRegistry 只读兼容投影。
2. 建立冻结调用模型、可信 `SkillExecutionContext`、`ApprovalContext`、结构化结果与稳定错误码。
3. 四个播前核心能力统一进入 `SkillExecutor`，并使用商品、计划和单手卡显式快照。
4. 通过 `RoutedPreLiveBusinessService` 保持现有同步 Graph、checkpoint 与 interrupt 协议不变。
5. 将 AgentToolExecutor 的四个核心工具收敛到兼容规范化与单一 Runtime dispatch；其他工具保持原入口。
6. 增加测试专用等价比较器、四场景 Demo 和 `run_all.py phase11a-demo` 统一入口。

`96a5adb` 是提前错误实施，其新增兼容层已由 `94e2766` 完整删除，不计入有效交付。正式 Task 7 从 `4f77403` 开始，实现与硬化提交为 `4f77403`、`7e132f3`、`b60a85d`；Task 8 的证据与 Demo 提交为 `7154c89`、`fd54005`。

验收前的审计幂等复审补充了 `202b0f2`、`cc3de8b`、`4d40b9f`：PostgreSQL 审计 Store 现在对同工具、同幂等键的重放比较完整审计事实，显式固定 `READ COMMITTED` 以保证并发冲突后的新语句快照；等价比较器与业务流测试替身同步采用完整事实和严格 JSON 类型语义。`2ff7ca2` 与 `5c19ea6` 仅移除 `pyproject.toml` 的尾部空行，使全阶段 diff 检查没有遗留空白错误。

最终全阶段审查发现并由 `09fd138`、`35e3907` 修复两项 P1：人工中断审批改为 Graph 审计完成后的内部受控工厂，普通对象不能伪造可信来源；13 个 Manifest 的根 Schema 全部拒绝未声明字段。该安全收紧记录为 D-053，并成为 D-035 的受控投影例外。

## 3. 契约门禁

| 门禁 | 证据 | 结果 |
| --- | --- | --- |
| 13 个 Manifest ID、版本与 Schema 可启动校验 | `test_skill_catalog.py`、`test_skill_runtime_models.py` | 通过 |
| 9 个未迁移工具严格投影，4 个核心 Schema 只允许白名单差异 | Catalog 快照与兼容断言 | 通过 |
| 版本、生命周期、参数、幂等、审批和 Handler 校验 fail-closed | `test_skill_executor.py` | 通过 |
| 四个核心 Skill 使用显式快照 | Handler、Facade 与 Graph Runtime 测试 | 通过 |
| ToolRegistry 和 AgentToolExecutor 兼容，无四工具双 dispatch | `test_tool_registry.py`、`test_agent_tool_executor_skill_compat.py` | 通过 |

## 4. 行为门禁

读取与生成批次使用两套深隔离 Fake Repository 和独立 AuditStore。等价测试证明商品、计划、前三张手卡、状态及规范化审计语义一致，并额外验证嵌套商品快照不会跨栈污染。

`setup_live_session` 未参与双执行。专项测试覆盖无审批、拒绝、可信批准、相同幂等键重放和单路执行；Runtime 失败不会隐式调用 legacy，也不存在生产影子路由。

## 5. 两批路由

- 第一批 `query_products`、`generate_live_plan`、`generate_product_card` 由 `SKILL_ROUTE_PRELIVE_GENERATION` 独立控制。
- 第二批 `setup_live_session` 由 `SKILL_ROUTE_PRELIVE_SETUP` 独立控制。
- 两个配置默认均为 `LEGACY`，非法值 fail-fast，已装配的 `RoutePolicy` 不受进程内环境变量变化影响。
- Demo 验证全 legacy、仅第一批 Runtime、两批 Runtime、第二批回滚四种组合；第二批回滚不影响第一批。

## 6. 审批与幂等

- `HUMAN_INTERRUPT` 必须携带 `operator_id` 与 `approval_audit_id`，拒绝恢复不调用 setup Handler。
- `TRUSTED_COMPAT` 只能由内部兼容工厂构造，外部 arguments 不能伪造来源；其证据会进入可信上下文。
- 兼容规范化校验调用 room/trace 与显式快照一致，避免旧参数越过信任边界。
- setup 幂等键只从可信 Context 传入；相同键重放复用原 `audit_id`，不产生重复成功副作用。
- 同一工具幂等键若被用于不同 room、trace、计划或审计载荷，Store 返回受控冲突且保持首次审计行不变；JSON `true` 与数字 `1` 也会被视为不同事实。
- 并发等价重放在 Store 的显式 `READ COMMITTED` 连接中处理，唯一键冲突后的 SELECT 能读取已经提交的胜者审计事实。

## 7. 测试与 Demo 证据

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| Runtime 专项 7 文件 | `110 passed in 2.46s` | `0` |
| 相关回归 8 文件 | `45 passed in 1.15s` | `0` |
| 审计幂等专项 | `28 passed in 5.48s` | `0` |
| `pytest -q` | `543 passed, 3 deselected, 9 warnings in 97.98s` | `0` |
| `python scripts/run_phase11a_skill_runtime_demo.py` | 4 个场景均为 4 商品、4 计划项、3 手卡、`prepared`、8 条审计 | `0` |
| `python scripts/run_all.py phase11a-demo` | 统一入口复现相同 4 个场景 | `0` |
| `python scripts/check_doc_encoding.py` | `4 errors/59 warnings`，未通过 | `1` |
| `git diff --check` 与 `git diff --check 8f386cd^..HEAD` | 无空白错误；仅工作树 Git 行尾转换提示 | `0` |

### 7.1 复现命令

Runtime 专项：

```bash
pytest tests/unit/test_skill_runtime_models.py tests/unit/test_skill_catalog.py tests/unit/test_skill_executor.py tests/unit/test_pre_live_skill_handlers.py tests/unit/test_skill_runtime_routing.py tests/unit/test_agent_tool_executor_skill_compat.py tests/unit/test_skill_runtime_equivalence.py -q
```

相关回归：

```bash
pytest tests/unit/test_tool_registry.py tests/unit/test_tool_mask_policy.py tests/unit/test_security_hooks.py tests/unit/test_agent_tool_executor.py tests/unit/test_pre_live_graph.py tests/unit/test_pre_live_graph_checkpoint.py tests/unit/test_pre_live_graph_interrupt.py tests/unit/test_on_live_harness_agent_graph.py -q
```

全量测试的 9 条 warning 为现有 FastAPI/Starlette TestClient 与 Kafka Serializer/Deserializer 弃用告警。3 个 deselected 为默认配置排除项，没有将其记为 passed。

全仓编码扫描不能声明通过：4 个 error 均来自 `scripts/check_doc_encoding.py` 自身用于检测的 U+FFFD 示例；59 个 warning 为仓库既有 BOM 或工作树混合换行命中。Task 9 未修改扫描脚本，也未顺手治理历史文件。

严格 UTF-8 专项检查分两层执行：对 `8f386cd^..5c19ea6` 范围内已提交 Python 代码、测试和 Demo 检查 Git canonical blob；对 6 个 Task 9 文档和 3 个冻结事实源检查工作树字节。检查项包括严格 decode、encode 往返、BOM、U+FFFD、混合换行和尾随空白，最终目标命中为 `0`。工作树全仓扫描中的既有换行告警不等同于 canonical blob 损坏，二者已分开记录。

## 8. Design 偏差与实现澄清

Task 7 正式在 `SkillExecutionContext` 增加 `compatibility_enriched` 字段，使旧参数补全成为可序列化、可断言的兼容证据，而不是只存在于摘要文案。该字段只标记兼容入口，不改变核心 Skill 显式快照架构，未来 PlanEngine 仍禁止依赖补全。

实现进一步硬化了 Design 已定义的信任边界：`TRUSTED_COMPAT` 改为内部工厂令牌构造；兼容快照的 room/trace 必须与调用上下文一致；Executor 在调用 Handler 前复制并钉住映射；等价 Fake Repository 对嵌套模型做深隔离。这些是对既定架构的可验证澄清，没有改变两批路由、单一 Executor、无生产双执行或无隐式 fallback 的设计。

## 9. 历史问题与兼容债务

- ToolRegistry 只读查询 API 保留至 Phase 12 验收后重审。
- `TRUSTED_COMPAT`、AgentToolExecutor 参数补全和同步执行桥接仍是兼容债务，不应成为新调用方 API。
- 默认仍为 legacy 路由；本阶段证明可切换与可回滚，不代表已经接入真实平台流量。
- 未迁移的 9 个 Handler、统一超时、完整结构化失败映射和真实平台 Adapter 属于 Phase 11B 候选范围。
- 全仓编码扫描仍有 `4 errors/58 warnings` 的历史/工具自身命中，后续治理必须单独立项，不能归为 Phase 11A 通过项。

## 10. Phase 11B 进入条件

1. 用户审核并明确接受本 Acceptance；当前条件尚未满足。
2. Phase 11A 的 Manifest、Executor、显式输入、两批路由及兼容债务边界保持稳定。
3. 重新读取本报告、Design、Plan、路线图和决策日志，基于实际验收证据按 Just-in-Time 原则编写独立 Phase 11B Design。
4. 在 Design 审核前不实现平台 Adapter、统一超时、剩余 Handler 迁移或新的动态配置能力。

## 11. 用户审核

- [ ] 用户已审核技术验收证据。
- [ ] 用户已确认是否接受 Phase 11A Acceptance。
- [ ] 用户已授权进入 Phase 11B Design 讨论。

当前审核结论：待用户审核。
