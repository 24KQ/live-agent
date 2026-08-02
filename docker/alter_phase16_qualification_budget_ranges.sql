-- Phase 16 资格账本预算 CHECK 放宽为范围兜底（幂等迁移）。
--
-- 背景：预算数值（project/campaign/stage/reservation）之前以精确值钉死在 DDL CHECK
-- 里（= 5.0 / = 4.0 / = 0.1 / <= 4.0 / <= 0.1），每次候选调参都要 drop trigger →
-- ALTER → 重建，把 DB 迁移仪式化。预算的唯一权威是 HMAC 绑定的 policy 模型
-- （phase16_qualification_policies 行 + policy_digest），DDL CHECK 只做合理性兜底：
--   policies:       project > 0；0 < campaign <= project；0 < stage <= campaign
--   campaigns:      reservation > 0 且 <= 10（Python 层强制 <= campaign budget 且
--                   全部 campaign committed 和 <= project budget）
--   dispatch_attempts: reservation > 0 且 <= 1（Python 层强制 <= policy stage cap）
-- 协议形状（holdout_batch_count = 2、holdout_e2e_cases_per_batch = 15）保持不变。
--
-- 幂等：重复执行会先 DROP 新旧两个名字的约束再重建，结果一致。

DO $$
BEGIN
    ALTER TABLE phase16_qualification_policies
        DROP CONSTRAINT IF EXISTS phase16_qualification_policies_project_budget_cny_check;
    ALTER TABLE phase16_qualification_policies
        DROP CONSTRAINT IF EXISTS phase16_qualification_policies_campaign_budget_cny_check;
    ALTER TABLE phase16_qualification_policies
        DROP CONSTRAINT IF EXISTS phase16_qualification_policies_stage_reservation_cny_check;
    ALTER TABLE phase16_qualification_policies
        DROP CONSTRAINT IF EXISTS phase16_qualification_policies_budget_ranges_check;
    ALTER TABLE phase16_qualification_campaigns
        DROP CONSTRAINT IF EXISTS phase16_qualification_campaigns_reservation_cny_check;
    ALTER TABLE phase16_qualification_campaigns
        DROP CONSTRAINT IF EXISTS phase16_qualification_campaigns_reservation_cny_range_check;
    ALTER TABLE phase16_qualification_dispatch_attempts
        DROP CONSTRAINT IF EXISTS phase16_qualification_dispatch_attempts_reservation_cny_check;
    ALTER TABLE phase16_qualification_dispatch_attempts
        DROP CONSTRAINT IF EXISTS phase16_qualification_dispatch_attempts_reservation_cny_range_check;
END $$;

ALTER TABLE phase16_qualification_policies
    ADD CONSTRAINT phase16_qualification_policies_budget_ranges_check
    CHECK (
        project_budget_cny > 0
        AND campaign_budget_cny > 0
        AND campaign_budget_cny <= project_budget_cny
        AND stage_reservation_cny > 0
        AND stage_reservation_cny <= campaign_budget_cny
    );

ALTER TABLE phase16_qualification_campaigns
    ADD CONSTRAINT phase16_qualification_campaigns_reservation_cny_range_check
    CHECK (reservation_cny > 0 AND reservation_cny <= 10.000000);

ALTER TABLE phase16_qualification_dispatch_attempts
    ADD CONSTRAINT phase16_qualification_dispatch_attempts_reservation_cny_range_check
    CHECK (reservation_cny > 0 AND reservation_cny <= 1.000000);
