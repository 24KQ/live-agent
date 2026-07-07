-- LiveAgent Phase 2A 播前业务样例表初始化脚本。
-- 本脚本只创建脱敏样例主播、直播场次、商品和直播间货盘关联表。
-- 真实账号、真实用户、真实订单、真实淘宝 API 凭据均不得写入这些表。

-- 集成测试和演示脚本可能重复初始化 schema。这里使用事务级 advisory lock：
-- 锁会在事务提交后自动释放，避免“DDL 尚未提交但锁已释放”的并发建表竞态。
SELECT pg_advisory_xact_lock(hashtext('live_agent_phase2_pre_live_schema'));

-- pgcrypto 用于后续如需生成 UUID；当前表使用稳定文本 ID，便于测试可重复。
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS live_agent_anchors (
    anchor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    style_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS live_agent_live_rooms (
    room_id TEXT PRIMARY KEY,
    anchor_id TEXT NOT NULL REFERENCES live_agent_anchors(anchor_id),
    title TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS live_agent_products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price NUMERIC(12, 2) NOT NULL CHECK (price >= 0),
    inventory INTEGER NOT NULL CHECK (inventory >= 0),
    conversion_rate NUMERIC(8, 4) NOT NULL CHECK (conversion_rate >= 0),
    commission_rate NUMERIC(8, 4) NOT NULL CHECK (commission_rate >= 0),
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    selling_points JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS live_agent_room_products (
    room_id TEXT NOT NULL REFERENCES live_agent_live_rooms(room_id),
    product_id TEXT NOT NULL REFERENCES live_agent_products(product_id),
    display_order INTEGER NOT NULL CHECK (display_order > 0),
    PRIMARY KEY (room_id, product_id)
);

-- room_id 是播前查询货盘的主入口。
CREATE INDEX IF NOT EXISTS idx_live_agent_room_products_room_id
    ON live_agent_room_products(room_id, display_order);

-- active + inventory 支撑后续快速筛选可讲解商品。
CREATE INDEX IF NOT EXISTS idx_live_agent_products_active_inventory
    ON live_agent_products(is_active, inventory);
