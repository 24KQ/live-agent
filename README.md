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
