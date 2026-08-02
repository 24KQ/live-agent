# LiveAgent

> 淘宝主播 AI 助手 —— 基于 LangGraph 的直播 Agent 系统

**项目定位（v1.0 定稿）**：受治理、有限循环、工具调用、人工审批协同的**直播运营 Agent 系统**——
面向直播运营的人机协同决策支持 Agent。播前为 Workflow（确定性排品手卡），播中为
bounded Agent loop（弹幕/告警 → EvidenceAnalyst 分析 → DecisionPlanner 规划 → 人审 →
执行建议），播后为结构化复盘与归因。高风险经营动作一律人工审批，审计、预算、幂等、
证据绑定与 fail-closed 治理贯穿全程；生产部署不在本版本范围内，未进行生产就绪声明。

## 架构总览

```mermaid
graph TD
    subgraph Infrastructure Layer
        PG[PostgreSQL 15 + pgvector]
        KF[Kafka 7.6]
        RD[Redis 7]
        MO[MinIO]
    end
    subgraph LangGraph Agent Layer
        PL[PreLive Harness - Workflow]
        OL[OnLive Harness Agent - StateGraph]
        PSR[PostLive Review + LLM Summary]
        EV[Agent Evaluation - Replay + Score]
    end
    subgraph Business Capability Layer
        PC[ProductCard + LiveReducer]
        DA[DanmakuAggregator + Kafka Daemon]
        MS[MemoryStore + TrustScore]
        TA[ToolCallAudit + DecisionTrace]
    end
    subgraph Web Presentation Layer
        API[FastAPI + REST + WebSocket]
        FE[Dashboard + Evaluation + Human Approval]
    end
    Infrastructure --> LangGraph Agent Layer
    LangGraph Agent Layer --> Business Capability Layer
    Business Capability Layer --> Web Presentation Layer
```

## 前置要求

- Docker 25+：运行 PostgreSQL 15 + Kafka 7.6 + Zookeeper
- Python 3.12+：运行 Agent 引擎和 API 服务
- （可选）DeepSeek API Key：开启 LLM 决策和播后总结，无 Key 时自动降级到规则引擎

## 快速开始

```bash
# 1. 克隆项目
git clone <repo-url>
cd live-agent

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动基础设施（PostgreSQL + Kafka）
docker compose up -d

# 4. 初始化数据库 + 启动 API
python scripts/run_all.py up

# 5. 打开 Web 副屏
open http://localhost:8100

# 6. （可选）启动端到端演示链路
#    终端 A：python scripts/run_all.py daemon
#    终端 B：python scripts/run_all.py simulator --scenario inventory_alert
```

> 无 Docker 环境时也可运行 mock 演示：
> ```bash
> python scripts/run_all.py demo
> ```

## 演示场景

| 场景 | 命令 | 效果 |
|------|------|------|
| 播前手卡生成 | 打开 Web 页面 | 查看排品方案和主播手卡 |
| 播中 Agent 决策 | `simulator --scenario price_spike` | 弹幕聚合 → Agent 建议 |
| 播中人工审批 | `simulator --scenario inventory_alert` | 售罄告警 → pending → 批准/拒绝 → 执行/跳过 |
| 播后复盘 | 打开 Web 页面 | 采纳率、准确率、归因分析 |
| Agent 评估 | 打开 /evaluation 页面 | 回放时间线、维度评分、Verdict |

Phase 13-16 的无外部依赖阶段演示：

```bash
python scripts/run_all.py phase13-demo
python scripts/run_all.py phase14-demo
python scripts/run_all.py phase15-demo
python scripts/run_all.py phase16-demo
```

其中 Phase 15 会回放三场景业务闭环并运行两次本地确定性 Release profile；在真实模型、真人对照和托管 CI 证据缺失时，技术 dry-run 可为 `PASS`，但 Promotion/Acceptance 保持 `BLOCKED`/`INCONCLUSIVE`，不会调用真实模型。

Phase 16 固定回放 `live-session-p001-sold-out-v2`：确定性售罄保护先执行，随后仅在高冲突证据满足门槛时顺序运行 EvidenceAnalystAgent 和 DecisionPlannerAgent。运营可批准、受控修改或拒绝方案；Demo 只持久化选中的人工决定和编译命令，绝不自动提交经营恢复。真实 smoke 缺少 endpoint/usage 证据时保持 `BLOCKED`，默认路由保持 `DETERMINISTIC_ONLY`。

## 验证证据链（Phase 16 V9，2026-08-02 账本权威值）

- **受控双 Agent E2E**：EvidenceAnalyst → DecisionPlanner 顺序编排（受控、非自主辩论），
  12 runs 真实模型执行（8 PASS / 4 FAILED，失败终态按防刷分设计保留、不可重跑）
- **账本**：PostgreSQL append-only qualification 账本，271 receipts / 6.604131 CNY /
  1,894,873 tokens / 332 transport attempts；全部 receipt `receipt_complete=true`
- **回执完整性**：新迁移写入的 receipt 必须包含实际响应端点与尝试次数
  （`attempt_count`/`responded_endpoint_host`）；46 条迁移前 legacy receipt 保留原始
  语义并标记 `PROVIDER_IDENTITY_UNVERIFIED`
- **CI 确定性 gate**：`agent-runtime-pr.yml` 无密钥（`PHASE15_REAL_MODEL=0`），
  Postgres + Kafka 环境 36 cases 确定性验证
- **契约治理**：v2（历史执行契约，冻结不可改）/ v3（纯回溯评价契约，已闭合）/
  Phase 17（计划中新建的独立 holdout 执行契约，尚未接入运行时）；digest 认证 + 闭包漂移 fail-closed
- **核验入口**：`python scripts/verify_phase16_qualification_ledger_export.py`
  （只读 SELECT，14 项聚合断言全 PASS）

## 诚实边界（如实声明，不包装）

- 默认路由为 `DETERMINISTIC_ONLY`：LLM 生产自动路由未放开，系统按规则引擎安全降级
- V9 = 开发/验证阶段资格认证（`DEVELOPMENT_VALIDATION_QUALIFIED`），**不等于**原始
  Phase 16 V5 官方 DeepSeek 契约 PASS，**不等于**生产就绪
- Phase 17 holdout 30 例为**计划中的、尚未执行**的预声明工程验收线（阈值 90%），
  不是统计显著性声明；只验证冻结的 Phase 16 Analyst→Planner 受控链路，不代表整个系统已生产就绪
- 记忆系统为受治理的检索与候选存储管线（证据约束/脱敏/幂等），不是模型自主学习
- 真实平台 API、真实经营副作用、业务 KPI 与生产运维**不在本版本范围内，未进行生产就绪声明**

## 核心功能

| 阶段 | 能力 | 技术实现 |
|------|------|---------|
| 播前 | 排品方案 + 主播手卡 | LangGraph Workflow + RulesPlanner |
| 播中 | 弹幕聚合 + Agent 决策 | Harness Agent Loop + LLM/规则降级 |
| 播中 | 人审 interrupt/resume | LangGraph interrupt + Web 审批界面 |
| 播中 | Kafka 弹幕通路 | DanmakuDaemon + 5s 窗口聚合 + 模拟生产者 |
| 播后 | 结构化复盘 | PostLiveReview + Attribution |
| 播后 | LLM 自然语言总结 | DeepSeek（不可用时降级到结构化模板） |
| 评估 | 回放 + 规则评分 | AgentReplayService + AgentRuleEvaluator（7 维度） |
| 评估 | LLM Judge（可选） | AgentLLMJudge（仅影响 10% 语义质量权重） |
| 生产 | 工具安全门禁 | Skill Catalog + SkillPolicyView + SecurityHook + LifecycleHook |
| 生产 | 操作员鉴权 | Header token + 角色权限（operator/reviewer/admin） |
| 生产 | 审批幂等/过期/锁定 | Idempotency Key + 10 分钟 TTL + Lock |
| 生产 | LLM 调用健壮性 | 指数退避重试 + 异常细分 + Token 追踪 |
| 生产 | 工具参数校验 | jsonschema 校验（可选依赖，不可用时跳过） |

## API 一览

| 端点 | 方法 | 说明 | 鉴权 |
|------|------|------|------|
| /api/card/{id} | GET | 获取主播手卡 | 否 |
| /api/danmaku/summary | GET | 弹幕聚合摘要 | 否 |
| /api/alert/{room_id} | GET | 库存告警 | 否 |
| /api/review/{room_id} | GET | 播后复盘数据 | 否 |
| /api/agent/harness/start | POST | 启动 Harness Agent 会话 | 否 |
| /api/agent/harness/status | GET | 查询 Agent 状态 | 否 |
| /api/agent/harness/approval | POST | 提交人审结果 | operator |
| /api/agent/evaluations | POST | 创建评估任务 | 否 |
| /api/agent/evaluations/{id} | GET | 查询评估结果 | 否 |
| /api/agent/evaluations/{id}/reviews | POST | 提交人工复核 | reviewer |
| /api/agent/replays/{trace_id} | GET | 查询回放时间线 | 否 |
| /ws | WS | 实时推送 | 否 |

## 项目结构

```text
live-agent/
  src/core/      LangGraph Agent 编排层（Graph、Hook、Audit、Replay）
  src/gateway/   FastAPI 服务、Session Store、WebSocket、鉴权
  src/skills/    业务能力层（手卡、弹幕、复盘、LLM）
  src/config/    配置；能力事实源位于 src/skill_runtime/catalog.py
  src/state/     领域模型与状态定义
  src/audit/     审计记录存储
  src/memory/    记忆与信任评分
  front/         Web 副屏页面（Dashboard + Evaluation UI）
  scripts/       CLI 工具、演示脚本、数据种子
  docker/        PostgreSQL 初始化 DDL（9 个 init SQL）
  tests/         单元测试（unit 1703 passed / integration 250 passed, 7 deselected；
  2026-08-02 Phase 16 收口本地基线，非永久常数，随阶段②新增测试变化）
```

## 技术栈

LangGraph / FastAPI / PostgreSQL 15 / Kafka 7.6 / DeepSeek / pgvector / Redis / MinIO

## 测试

```bash
# 全量单元测试
pytest tests/unit/ -v

# 编码扫描
python scripts/check_doc_encoding.py

# 端到端 mock 演示
python scripts/run_all.py demo
```

## 开发说明

- 新增 Skill 必须在 `Skill Catalog` 注册，并由 `SkillPolicyView` 投影生命周期、风险等级、参数 Schema 和门禁策略
- 所有新增/修改代码使用 UTF-8 编码
- 播前流程是 Workflow，不是 Agent；播中流程才是 LangGraph Agent
- 高风险工具不自动执行，必须经过人审 interrupt
- Agent 评估优先用规则评分，LLM Judge 只补充建议语义质量维度

## License

MIT
