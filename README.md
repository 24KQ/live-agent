# LiveAgent

LiveAgent（淘宝主播 Agent 开源复刻版）是一个面向电商直播的 AI 主播搭档，以 Harness Engineering 为核心，让大模型在可控、可审计、可恢复的工程边界内辅助主播完成播前策划、播中控场和播后复盘。当前仓库处于正式立项与架构设计阶段，重点文档集中在 `docs/project_guidance/`。

## 文档入口

- [PRD](docs/project_guidance/taobao_anchor_agent_prd.md)：说明为什么做、做什么、做到什么程度算完成。
- [Design Spec](docs/project_guidance/taobao_anchor_agent_design_spec.md)：说明怎么做、按什么架构做、如何验证。
- [Compliance Checklist](docs/project_guidance/compliance_checklist.md)：新增工具、数据表或外部接口时的合规与安全检查表。
- [Delivery Plan](docs/project_guidance/delivery_plan.md)：Phase 0-5 的研发排期与验收标准。
- [Implementation Guide](docs/project_guidance/implementation_guide.md)：后续开发者或 AI 编码 Agent 的实现顺序。

## 当前边界

- LiveAgent 是本地研发与开源复刻原型，不接入真实淘宝生产 API。
- MVP 使用模拟商品、模拟弹幕、模拟库存和模拟主播反馈。
- 公开仓库不保存真实账号、密码、Token 或 `.env` 文件。

## 本地配置

复制 `.env.example` 为 `.env`，然后填入你本机 Docker 中间件的真实值。

```powershell
Copy-Item .env.example .env
```

`.env` 已被 `.gitignore` 忽略，请不要提交。

## Phase 0 快速开始

安装 Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

复制环境变量模板，并按你本机 Docker 中间件填写真实账号和密码：

```powershell
Copy-Item .env.example .env
```

运行配置层测试：

```powershell
pytest tests/unit/test_settings.py -v
pytest tests/integration/test_infra_config.py -v
```

运行全量测试：

```powershell
pytest -v
```

检查本地中间件连通性：

```powershell
python scripts/check_infra.py
```

如果 PostgreSQL、Redis 或 Kafka 未启动，检查脚本会显示失败服务并返回退出码 `1`；全部通过时返回退出码 `0`。

## Phase 1 播前地基层演示

Phase 1 演示播前最小可控闭环：查询模拟货盘、请求改价、进入 hard-gate、确认后更新状态并写入 PostgreSQL 审计。

```powershell
python scripts/run_phase1_pre_live_demo.py
```

## Phase 2A 播前业务能力演示

Phase 2A 演示基于 PostgreSQL 样例数据的播前业务闭环：初始化脱敏商品数据、查询货盘、生成排品草案、生成商品手卡、确认模拟建播并写入审计。

初始化或刷新样例数据：

```powershell
python scripts/seed_phase2_demo_data.py
```

运行播前业务闭环演示：

```powershell
python scripts/run_phase2_pre_live_demo.py
```

## Phase 2B 基础播中事件演示

Phase 2B 演示本地模拟售罄事件：进入播中状态、下架售罄商品、推荐备选商品、生成主播提示并写入审计。

```powershell
python scripts/run_phase2b_on_live_demo.py
```

## Phase 2C 基础弹幕聚合演示

Phase 2C 演示本地模拟弹幕批次：进入播中状态、按 5 秒窗口聚合同类问题、生成主播参考回复并写入审计。回复只作为主播参考，不会自动发送给观众。

```powershell
python scripts/run_phase2c_danmaku_demo.py
```

## Phase 2D LangGraph 播前骨架演示

Phase 2D 演示用 LangGraph 编排已有播前业务闭环：查询货盘、生成排品、生成手卡、输出合规摘要、通过建播 hard-gate 并写入审计。本阶段不接 LLM、不启用持久 checkpoint。

```powershell
python scripts/run_phase2d_pre_live_graph_demo.py
```

阶段任务、验收命令和测试反馈记录在 [Phase Execution Log](docs/project_guidance/phase_execution_log.md)。
