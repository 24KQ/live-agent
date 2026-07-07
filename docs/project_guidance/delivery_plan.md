# LiveAgent 研发排期交付计划

> **版本**: v1.0  
> **更新日期**: 2026-07-07  
> **用途**: 指导后续把 LiveAgent（淘宝主播 Agent 开源复刻版）分阶段开发、验证和演示。  
> **配套文档**: [PRD](./taobao_anchor_agent_prd.md)、[Design Spec](./taobao_anchor_agent_design_spec.md)、[实现指南](./implementation_guide.md)

---

## 1. 交付策略

本项目按“先 Harness 地基，再业务智能”的顺序交付。不要一开始就做复杂 UI 或多 Agent 炫技；先保证生命周期、状态、工具门禁、审计和恢复这些底座可用。

交付节奏：

| 阶段 | 目标 | 建议周期 | 结果 |
| :--- | :--- | :---: | :--- |
| Phase 0 | 项目脚手架与本地中间件验证 | 1-2 天 | 能连接 PostgreSQL、Redis、Kafka |
| Phase 1 | 地基层 | 5-7 天 | 生命周期、Reducer、安全 Hook 可运行 |
| Phase 2 | 播前与基础播中 | 5-7 天 | 排品、手卡、查询、改价拦截 |
| Phase 3 | 记忆与信任 | 5-7 天 | L1/L2/L3、Decision Trace、trust_score |
| Phase 4 | PlanEngine 与抢占 | 7-10 天 | DAG、checkpoint、抢占恢复 |
| Phase 5 | 端到端演示 | 5-7 天 | CLI/Web 副屏、完整 demo、播后报告 |

---

## 2. Phase 0: 项目脚手架与中间件验证

### 目标

建立 Python 项目骨架，确认本地 Docker 中间件可用。

### 交付物

- `requirements.txt` 或 `pyproject.toml`
- `.env.example`
- `src/config/settings.py`
- `docker/init_postgres.sql`
- `scripts/check_infra.py`

### 验收标准

| 编号 | 验收项 |
| :--- | :--- |
| P0-01 | 能连接 `localhost:5432` PostgreSQL |
| P0-02 | 能执行 `CREATE EXTENSION IF NOT EXISTS vector;` |
| P0-03 | 能连接 `localhost:6379` Redis |
| P0-04 | 能连接 `localhost:9092` Kafka |
| P0-05 | `.env.example` 包含必要配置，但真实 `.env` 不提交 |

---

## 3. Phase 1: Harness 地基层

### 目标

实现系统最小可控闭环：生命周期、状态模型、Reducer、工具注册、安全 Hook、审计。

### 任务清单

| 任务 | 文件 | 验收 |
| :--- | :--- | :--- |
| 数据模型 | `src/state/models.py` | Pydantic 模型能校验 Lifecycle、Product、Action |
| 生命周期 | `src/core/lifecycle.py` | 合法状态可切换，非法跳转被拒绝 |
| 工具注册 | `src/config/tool_registry.py` | 每个工具有生命周期、风险等级、Schema |
| Reducer | `src/state/reducer.py` | `SET_PRICE`、`MARK_SOLD_OUT`、`SWITCH_PRODUCT` 可测试 |
| 安全 Hook | `src/core/security_hooks.py` | auto、soft-gate、hard-gate、block 四级可测试 |
| 审计 | `src/audit/tool_call_audit.py` | 工具调用可写入审计表 |

### 阶段演示

终端输入：

```text
查询货盘 -> 建议修改 p001 价格 -> hard-gate 等待确认 -> 确认后 Reducer 更新价格 -> 写审计日志
```

---

## 4. Phase 2: 播前与基础播中能力

### 目标

实现主播最容易理解的业务闭环：货盘查询、排品、手卡、模拟建播、弹幕回复、售罄处理。

### 任务清单

| 任务 | 文件 | 验收 |
| :--- | :--- | :--- |
| 样例数据 | `scripts/seed_demo_data.py` | 生成 10 个商品、1 个主播、1 场直播 |
| 播前工具 | `src/skills/pre_live_skills.py` | 排品和手卡输出符合 Schema |
| 播中工具 | `src/skills/on_live_skills.py` | 售罄下架、弹幕回复可运行 |
| 上下文管理 | `src/core/context_manager.py` | 每轮只注入 State 快照和必要记忆 |
| CLI 演示 | `scripts/run_demo_live.py` | 能手动推进 PRE -> ON -> POST |

### 阶段演示

```text
创建直播 -> 查询货盘 -> 生成排品 -> 生成 3 个商品手卡 -> 开播 -> 模拟弹幕 -> 生成参考回复
```

---

## 5. Phase 3: 记忆与信任

### 目标

让 Agent 跨场次记住主播偏好，并根据主播反馈调整建议强度和工具可见范围。

### 任务清单

| 任务 | 文件 | 验收 |
| :--- | :--- | :--- |
| 记忆存储 | `src/memory/memory_store.py` | L1/L2/L3 可写入、查询、向量检索 |
| 决策轨迹 | `src/audit/decision_trace.py` | 建议、采纳、拒绝、业务结果可记录 |
| 信任分 | `src/memory/trust_manager.py` | trust_score 按规则更新并钳制 |
| 偏好对账 | `src/memory/belief_revision.py` | L1/L3 矛盾达到阈值时触发确认 |
| 工具掩码 | `src/core/security_hooks.py` | trust_score 影响可用工具 |

### 阶段演示

```text
主播拒绝 3 次高风险建议 -> trust_score 降低 -> 下一场只展示数据，不再直接建议改价
```

---

## 6. Phase 4: PlanEngine 与抢占恢复

### 目标

实现本项目的高含金量部分：DAG 全局规划、checkpoint、增量 Replan、紧急事件抢占。

### 任务清单

| 任务 | 文件 | 验收 |
| :--- | :--- | :--- |
| DAG 模型 | `src/core/plan_engine.py` | 节点依赖、状态、重试次数可维护 |
| Checkpointer | `src/state/checkpointer.py` | DAG 状态可保存和恢复 |
| 事件聚合 | `src/gateway/event_aggregator.py` | 5 秒窗口聚合同类弹幕 |
| Kafka 消费 | `src/gateway/kafka_consumer.py` | 能消费弹幕、库存、流量 topic |
| 抢占控制 | `src/core/preemption.py` | Level 5 事件冻结当前任务 |
| 增量 Replan | `src/core/plan_engine.py` | 只重算受影响节点 |

### 阶段演示

```text
正在生成 10 个商品手卡 -> p003 售罄事件到达 -> 冻结手卡任务 -> 下架 p003 并推荐备选 -> 恢复剩余手卡生成
```

---

## 7. Phase 5: 端到端演示与交付

### 目标

形成可以向别人展示的完整项目：从播前到播中再到播后，有数据、有状态、有审计、有复盘。

### 任务清单

| 任务 | 文件 | 验收 |
| :--- | :--- | :--- |
| 副屏展示 | CLI 或 Web UI | 能展示建议、审批、状态 |
| 演示脚本 | `scripts/run_demo_live.py` | 一键跑完整 demo |
| 播后报告 | `src/skills/post_live_skills.py` | 输出本场复盘 markdown |
| 合规自检 | `docs/project_guidance/compliance_checklist.md` | 填写检查结论 |
| README | `README.md` | 包含启动、演示、测试说明 |

---

## 8. 最终验收清单

- [ ] 本地 PostgreSQL、Redis、Kafka 连接成功。
- [ ] 生命周期状态机测试通过。
- [ ] Reducer 测试通过。
- [ ] Security Hook 测试通过。
- [ ] Tool Registry 与 PRD 功能清单一致。
- [ ] 播前排品和手卡生成可演示。
- [ ] 播中售罄事件可抢占。
- [ ] 播后信任分和记忆可更新。
- [ ] 所有高危工具有审批和审计。
- [ ] 端到端 demo 可重复运行。
