-- Phase 16 资格账本 provider_receipts 补 V9 传输层重试事实列（幂等迁移）。
--
-- 背景：V9 起 controlled_e2e_adapter_v5.py 的 complete() 在同端点内对 TRANSPORT_ERROR
-- 与 HTTP 5xx 最多重试一次，每次返回值带 attempts 与 endpoint_host 事实。receipt
-- 需要持久化这两个事实，才能审计"实际调用了几次、响应来自哪个端点"。
--
-- 幂等：ADD COLUMN IF NOT EXISTS；重复执行结果一致。旧行默认 attempt_count=1、
-- responded_endpoint_host=NULL（迁移前写入的 receipt 保持原语义）。

DO $$
BEGIN
    ALTER TABLE phase16_qualification_provider_receipts
        ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1);
    ALTER TABLE phase16_qualification_provider_receipts
        ADD COLUMN IF NOT EXISTS responded_endpoint_host TEXT;
END $$;
