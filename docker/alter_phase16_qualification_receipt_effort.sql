-- Phase 16 资格账本 provider_receipts 补 V9 矩阵配置的 reasoning_effort 列（幂等迁移）。
--
-- 背景：V9 起模型 / 思考强度 / 渠道列表改为运行时矩阵配置（白名单集合由
-- profiles.py 闭包认证，campaign 声明组合）。receipt 需要持久化该 receipt 实际使用
-- 的思考强度，与 model_id / responded_endpoint_host 一起钉死运行时矩阵组合，
-- 供审计核查（receipt_auth_tag HMAC payload 已包含该值）。
--
-- 幂等：ADD COLUMN IF NOT EXISTS；重复执行结果一致。旧行 reasoning_effort=NULL
-- （迁移前写入的 receipt 未记录该事实，属正常）。
--
-- 注意：HMAC payload 已把 reasoning_effort 纳入，因此新 receipt 的 auth tag 自动
-- 覆盖新列；本迁移不需要重签任何历史行。

DO $$
BEGIN
    ALTER TABLE phase16_qualification_provider_receipts
        ADD COLUMN IF NOT EXISTS reasoning_effort TEXT;
END $$;
