-- Phase 16 资格账本 campaigns 唯一约束收敛为 campaign_id 身份（幂等迁移）。
--
-- 背景：campaign 身份已含声明组合（qualification_campaign_id = kind + digest[:16] +
-- sha256("batch|model|effort|hosts")[:16]）。遗留的
-- UNIQUE (policy_digest, campaign_kind, candidate_digest, batch_index) 不含组合，
-- 会把"同一 digest 下不同声明组合"误判为重复 —— 与身份设计直接冲突，真实 run
-- 已触发 UniqueViolation（vote520 优先组合被 synapse 优先组合占位拦截）。
--
-- 修正：删除该遗留约束。同 (digest, 组合) 仍被 campaign_id PRIMARY KEY +
-- ensure_campaign 声明字段查重双重拒绝；不同组合 → 不同 campaign_id → 合法并存
-- （各占一次 dev+validation 名额，防刷分语义不变）。
--
-- 幂等：重复执行 DROP IF EXISTS 后结束，结果一致。
DO $$
BEGIN
    ALTER TABLE phase16_qualification_campaigns
        DROP CONSTRAINT IF EXISTS phase16_qualification_campaig_policy_digest_campaign_kind_c_key;
END $$;
