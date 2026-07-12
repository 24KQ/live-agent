-- LiveAgent Phase 1 审计表初始化脚本。
-- 本脚本只创建工具调用审计表，不创建商品业务表；Phase 1 商品状态仍以内存模型为主。

-- 集成测试可能并行启动多个进程同时初始化表；PostgreSQL 的
-- CREATE TABLE IF NOT EXISTS 仍可能在内部类型创建阶段发生并发冲突。
-- 使用事务级 advisory lock，把 DDL 串行化到事务提交为止，避免锁释放早于提交。
SELECT pg_advisory_xact_lock(hashtext('live_agent_phase1_audit_schema'));

CREATE TABLE IF NOT EXISTS tool_call_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    action_type TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    gate_decision TEXT NOT NULL,
    operator_decision TEXT,
    idempotency_key TEXT,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 兼容已经存在的 Phase 1 表；旧记录保持 NULL，不回填或删除历史审计。
ALTER TABLE tool_call_audit
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

-- 相同工具与显式幂等键只允许一条审计事实。NULL 表示非幂等普通事件，
-- 不受唯一约束影响。Store 使用同一冲突目标原子返回原 audit_id。
CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_call_audit_tool_idempotency
    ON tool_call_audit (tool_name, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- trace_id 是后续排查一次建议/确认/执行链路的核心入口，建立索引便于快速回放。
CREATE INDEX IF NOT EXISTS idx_tool_call_audit_trace_id ON tool_call_audit (trace_id);

-- room_id 方便按直播间查看播前、播中、播后全部工具调用记录。
CREATE INDEX IF NOT EXISTS idx_tool_call_audit_room_id ON tool_call_audit (room_id);
