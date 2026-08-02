-- Phase 16 资格账本 campaigns 补 V9 矩阵配置声明列（幂等迁移）。
--
-- 背景：V9 起模型 / 思考强度 / 渠道列表不再冻结进 policy digest，改由 campaign
-- 声明运行时组合（白名单集合由 profiles.py 闭包认证；顺序即渠道优先级）。
-- receipt 行记录实际组合，核查以声明值为基准。
--
-- 幂等：ADD COLUMN IF NOT EXISTS；重复执行结果一致。旧行取默认值
-- （declared_model_id='gpt-5.6-luna'、declared_endpoint_hosts='synapse-ai.uk'、
-- declared_reasoning_effort=NULL），与历史 campaign 的实际运行组合一致。
--
-- 注意：白名单校验在 ledger Python 层（QualificationCampaign 校验器）强制，本迁移
-- 只加宽松列，避免白名单调参触发 ALTER。

DO $$
BEGIN
    ALTER TABLE phase16_qualification_campaigns
        ADD COLUMN IF NOT EXISTS declared_model_id TEXT NOT NULL DEFAULT 'gpt-5.6-luna';
    ALTER TABLE phase16_qualification_campaigns
        ADD COLUMN IF NOT EXISTS declared_reasoning_effort TEXT;
    ALTER TABLE phase16_qualification_campaigns
        ADD COLUMN IF NOT EXISTS declared_endpoint_hosts TEXT NOT NULL DEFAULT 'synapse-ai.uk';
END $$;
