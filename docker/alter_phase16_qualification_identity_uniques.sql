-- Phase 16 资格账本身份唯一约束放宽（幂等迁移）。
--
-- 背景：policies/corpora 的 UNIQUE (id, version) 与 candidates 的 UNIQUE (policy_digest,
-- candidate_id) 把"同一 id+version 只能有一个 policy"当成身份。但重冻结语义是：任何
-- 资格源码/候选调参变化 → 新 digest → 新行追加（append-only），旧证据保留。之前的
-- UNIQUE 会让同 (id, version) 的第二次冻结直接 UniqueViolation，逼着每次调参都清库。
--
-- 修正：digest（每张表的 PK）才是身份；policy/corpus/candidate 之间用 digest 外键绑定，
-- 版本字符串降级为描述性字段。同 (id, version) 的新 digest 追加为新行，旧行只读保留。
--
-- 幂等：重复执行先 DROP IF EXISTS 再结束，结果一致。

DO $$
BEGIN
    ALTER TABLE phase16_qualification_policies
        DROP CONSTRAINT IF EXISTS phase16_qualification_policies_policy_id_policy_version_key;
    ALTER TABLE phase16_qualification_corpora
        DROP CONSTRAINT IF EXISTS phase16_qualification_corpora_corpus_id_corpus_version_key;
    ALTER TABLE phase16_qualification_candidates
        DROP CONSTRAINT IF EXISTS phase16_qualification_candidates_policy_digest_candidate_id_key;
END $$;
