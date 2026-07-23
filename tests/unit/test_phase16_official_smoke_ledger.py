"""Phase 16 正式 smoke 账本的离线部署与敏感字段契约。"""

from __future__ import annotations

from scripts.run_db_migrations import MIGRATIONS


def test_official_smoke_ledger_is_registered_after_legacy_phase16_smoke_migration() -> None:
    """正式账本必须由统一迁移入口执行，且不能替换旧 Task 10 账本迁移。"""

    files = [step.sql_file for step in MIGRATIONS]

    assert "init_phase16_smoke.sql" in files
    assert "init_phase16_official_smoke_ledger.sql" in files
    assert files.index("init_phase16_smoke.sql") < files.index(
        "init_phase16_official_smoke_ledger.sql"
    )
