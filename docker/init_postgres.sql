-- LiveAgent Phase 0 PostgreSQL 初始化脚本。
-- 本脚本只初始化扩展，不创建业务表，避免在项目脚手架阶段提前固化数据模型。

-- pgvector 用于后续向量检索和长期记忆召回，例如商品知识、直播复盘和主播偏好。
-- 如果本地 PostgreSQL 镜像没有安装 pgvector，执行本脚本时会明确失败，便于尽早修正镜像。
CREATE EXTENSION IF NOT EXISTS vector;

-- pgcrypto 用于后续生成 UUID、摘要或加密辅助能力，适合审计记录和幂等键等场景。
CREATE EXTENSION IF NOT EXISTS pgcrypto;
