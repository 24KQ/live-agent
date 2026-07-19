"""Phase 7B 迁移脚本的语法回归测试。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PHASE7B_SQL = PROJECT_ROOT / "docker" / "init_phase7b_production_hardening.sql"


def test_phase7b_partial_index_uses_sql_literal_after_do_block() -> None:
    """确保 DO 块结束后的局部索引使用普通 SQL 字符串字面量。

    ``DO $$ ... $$`` 是 dollar-quoted 正文，块内和结束后的 ``CREATE INDEX`` 都
    直接由 PostgreSQL 解析。继续双写会被解析为空字符串拼接，导致全量迁移失败。
    """

    sql = PHASE7B_SQL.read_text(encoding="utf-8")
    partial_index_sql = sql.split("-- 创建索引用于查询 expired/locked 会话", maxsplit=1)[1]

    assert "WHERE status = 'pending_human';" in partial_index_sql
    assert "WHERE status = ''pending_human'';" not in partial_index_sql


def test_phase7b_static_sql_after_do_block_does_not_double_escape_literals() -> None:
    """防止静态告警表 DDL 误沿用 PL/pgSQL 块内的双写规则。"""

    sql = PHASE7B_SQL.read_text(encoding="utf-8")
    static_sql = sql.split("-- 2. 新增 live_agent_operational_alerts 表", maxsplit=1)[1]

    assert "''" not in static_sql


def test_phase7b_dollar_quoted_blocks_do_not_double_escape_sql_literals() -> None:
    """确保独立 SQL 文件不会把 dollar-quoted 正文误作 Python 字符串转义。"""

    sql = PHASE7B_SQL.read_text(encoding="utf-8")

    assert "''" not in sql
