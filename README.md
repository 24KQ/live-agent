# LiveAgent

> 淘宝主播 AI 助手 —— 基于 LangGraph 的直播 Agent 系统

## 快速开始

`ash
pip install -r requirements.txt
docker compose up -d
python scripts/run_all.py up
# 打开 http://localhost:8100
`

## 架构

`	ext
Web Frontend (FastAPI + Vanilla JS)
  -> LangGraph Agent Layer (PreLive / OnLiveHarness / PostLive)
    -> Business Skills (ProductCard / DanmakuAggregator / MemoryStore)
      -> Infrastructure (PostgreSQL / Kafka / Redis / MinIO)
`

## 核心能力

- 播前: Workflow + RulesPlanner 生成商品手卡
- 播中: LangGraph Harness Agent + Interrupt/Resume 人审
- 播中: Kafka 弹幕捕获 + 5s 窗口聚合
- 播后: 规则复盘 + LLM 总结含降级
- 评估: Agent 回放 + 7 维度规则评分 + LLM Judge
- 运维: 操作员鉴权 + 审批锁/TTL + 幂等 + Worker 恢复 + 告警

## 技术栈

LangGraph / FastAPI / PostgreSQL 15 / Kafka 7.6 / DeepSeek / pgvector

## 演示

`powershell
.\\run.ps1 docker  # 启动基础设施
.\\run.ps1 up      # 一键启动
.\\run.ps1 demo    # 端到端演示
`

## 测试

`ash
pytest tests/unit/ -v   # 366+ 单元测试
python scripts/check_doc_encoding.py
python scripts/check_sensitive_payloads.py
`

## 项目结构

`	ext
src/core/      LangGraph Agent 编排
src/gateway/   API 服务与持久化
src/skills/    业务能力层
src/config/    配置中心
front/         Web 副屏
scripts/       演示脚本
docker/        数据库初始化 SQL
tests/         单元与集成测试
`

详细文档见 docs/ 目录