"""从 smoke manifest 同步账本 SQL 触发器常量并重算 schema contract digest。

重认证周期（白名单/闭包变更导致 manifest digest 重冻结）后的收尾工具：
自动把 manifest 的 manifest_digest / profile_digests / case_ids+case_digests 写回
init SQL 的触发器函数体，并在全新隔离 schema 重算 schema contract digest 更新
expected_contract_digest。避免手工编辑 SQL 常量与手算 digest。

用法：
    cd .worktrees/phase16-v5-controlled-e2e
    python scripts/sync_phase16_smoke_ledger_digests.py            # 实际同步
    python scripts/sync_phase16_smoke_ledger_digests.py --dry-run  # 只打印变更预览

说明：
    - V1/V2 两份 init SQL 与 manifest 一一对应（manifest 与 SQL 的 run_id 匹配）。
    - 契约 digest 在干净 schema 上重算（迁移本身按同流程验证），结果写回
      expected_contract_digest 常量；断言函数自身不计入被核验的投影。
    - 同步后需重跑 scripts/run_db_migrations.py 让真实库补齐结构并验证全链。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# (manifest 相对路径, init SQL 相对路径, 触发器前缀, digest 函数名)
_SPECS = (
    (
        "evaluation/manifests/phase16-official-smoke-evidence-v1.json",
        "docker/init_phase16_official_smoke_ledger.sql",
        "phase16_official_smoke_",
        "phase16_official_smoke_schema_contract_digest",
    ),
    (
        "evaluation/manifests/phase16-official-smoke-evidence-v2.json",
        "docker/init_phase16_official_smoke_v2_ledger.sql",
        "phase16_official_smoke_v2_",
        "phase16_official_smoke_v2_schema_contract_digest",
    ),
)

_FROZEN_RUN_TEMPLATE = (
    "CREATE OR REPLACE FUNCTION {prefix}validate_frozen_run() RETURNS trigger AS $$\n"
    "BEGIN\n"
    "    IF NEW.run_id <> '{run_id}'\n"
    "       OR NEW.manifest_digest <> '{manifest_digest}'\n"
    "       OR NEW.analyst_profile_digest <> '{analyst_digest}'\n"
    "       OR NEW.planner_profile_digest <> '{planner_digest}' THEN\n"
    "        RAISE EXCEPTION 'phase16 official smoke frozen manifest identity conflicts with formal evidence';\n"
    "    END IF;\n"
    "    RETURN NEW;\n"
    "END;\n"
    "$$ LANGUAGE plpgsql;"
)

_FROZEN_SLOT_TEMPLATE = (
    "CREATE OR REPLACE FUNCTION {prefix}validate_frozen_case_slot() RETURNS trigger AS $$\n"
    "BEGIN\n"
    "    IF NEW.run_id <> '{run_id}'\n"
    "       OR NOT EXISTS (\n"
    "           SELECT 1\n"
    "             FROM (VALUES\n"
    "{rows}\n"
    "             ) AS frozen_slot(slot_position, case_id, case_digest)\n"
    "            WHERE frozen_slot.slot_position = NEW.slot_position\n"
    "              AND frozen_slot.case_id = NEW.case_id\n"
    "              AND frozen_slot.case_digest = NEW.case_digest\n"
    "       ) THEN\n"
    "        RAISE EXCEPTION 'phase16 official smoke frozen case slot conflicts with formal evidence';\n"
    "    END IF;\n"
    "    RETURN NEW;\n"
    "END;\n"
    "$$ LANGUAGE plpgsql;"
)


def _build_slot_rows(case_ids: list[str], case_digests: dict[str, str]) -> str:
    # V1/V2 manifest 的 case_digests 均为 dict(case_id -> digest)；按 case_id 取值，
    # 保持 SQL 行顺序与 case_ids 一致。
    rows = []
    for index, case_id in enumerate(case_ids, start=1):
        case_digest = case_digests[case_id]
        comma = "," if index < len(case_ids) else ""
        rows.append(f"                 ({index}, '{case_id}', '{case_digest}'){comma}")
    return "\n".join(rows)


def _replace_function(text: str, prefix: str, name: str, replacement: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE FUNCTION {re.escape(prefix)}{name}\(\) RETURNS trigger AS \$\$"
        rf".*?\$\$ LANGUAGE plpgsql;",
        re.DOTALL,
    )
    new_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise RuntimeError(f"trigger function {prefix}{name} not found in SQL")
    return new_text


def _sync_sql_file(sql_path: Path, manifest_path: Path, prefix: str, *, dry_run: bool) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    case_ids = manifest["case_ids"]
    case_digests = manifest["case_digests"]
    if len(case_ids) != len(case_digests):
        raise RuntimeError(f"{manifest_path.name}: case_ids/case_digests length mismatch")
    profile_digests = manifest["profile_digests"]

    original = sql_path.read_text(encoding="utf-8")
    text = original

    run_id = manifest["run_id"]
    run_replacement = _FROZEN_RUN_TEMPLATE.format(
        prefix=prefix,
        run_id=run_id,
        manifest_digest=manifest["manifest_digest"],
        analyst_digest=profile_digests["analyst"],
        planner_digest=profile_digests["planner"],
    )
    text = _replace_function(text, prefix, "validate_frozen_run", run_replacement)

    slot_replacement = _FROZEN_SLOT_TEMPLATE.format(
        prefix=prefix,
        run_id=run_id,
        rows=_build_slot_rows(case_ids, case_digests),
    )
    text = _replace_function(text, prefix, "validate_frozen_case_slot", slot_replacement)

    changes = []
    for label, old_line in (
        ("manifest/run identity", "OR NEW.manifest_digest <> '"),
        ("analyst profile digest", "OR NEW.analyst_profile_digest <> '"),
        ("planner profile digest", "OR NEW.planner_profile_digest <> '"),
        ("case slot rows", "FROM (VALUES"),
    ):
        if old_line in original and old_line in text:
            changes.append(label)

    if dry_run:
        return changes

    sql_path.write_text(text, encoding="utf-8", newline="\n")
    return changes


def _recompute_contract_digest(sql_path: Path, digest_function: str) -> str:
    """在全新隔离 schema 执行 init SQL（去掉末尾 assert 行）并返回契约 digest。"""
    import psycopg
    from psycopg import sql as psql
    from src.config.settings import get_settings

    text = sql_path.read_text(encoding="utf-8")
    body, _, _ = text.rpartition(f"SELECT {digest_function.replace('_schema_contract_digest', '_assert_schema_contract')}();")
    if not body:
        # 断言行名不匹配则按通用模式剥离末尾 SELECT
        body, _, _ = text.rpartition("SELECT ")
    base = dict(get_settings().postgres_connection_kwargs)
    schema = f"digest_sync_{sql_path.stem.replace('-', '_')}"
    with psycopg.connect(**base) as conn:
        conn.execute(psql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(psql.Identifier(schema)))
        conn.execute(psql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psql.Identifier(schema)))
        conn.execute(psql.SQL("CREATE SCHEMA {}").format(psql.Identifier(schema)))
        conn.commit()
    settings = {**base, "options": f"-c search_path={schema}"}
    try:
        with psycopg.connect(**settings) as conn:
            conn.execute(body)
            conn.commit()
            digest = conn.execute(
                psql.SQL("SELECT {}()").format(psql.Identifier(digest_function))
            ).fetchone()[0]
    finally:
        with psycopg.connect(**base) as conn:
            conn.execute(psql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psql.Identifier(schema)))
            conn.commit()
    return digest


def _update_expected_digest(sql_path: Path, expected_digest: str, *, dry_run: bool) -> None:
    text = sql_path.read_text(encoding="utf-8")
    # schema contract digest 由 SQL 内 md5() 聚合得出，期望值是 32 位十六进制。
    pattern = re.compile(r"(expected_contract_digest TEXT := ')[0-9a-f]{32}(')")
    if not pattern.search(text):
        raise RuntimeError(f"{sql_path.name}: expected_contract_digest constant not found")
    if dry_run:
        return
    new_text, count = pattern.subn(rf"\g<1>{expected_digest}\g<2>", text, count=1)
    if count != 1:
        raise RuntimeError(f"{sql_path.name}: expected_contract_digest replace failed")
    sql_path.write_text(new_text, encoding="utf-8", newline="\n")


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    for manifest_rel, sql_rel, prefix, digest_function in _SPECS:
        manifest_path = _PROJECT_ROOT / manifest_rel
        sql_path = _PROJECT_ROOT / sql_rel
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(f"[SYNC] {sql_rel} <-- {manifest_rel} (run_id={manifest['run_id']})")
        changes = _sync_sql_file(sql_path, manifest_path, prefix, dry_run=dry_run)
        print(f"       trigger constants synced ({', '.join(changes)})")
        digest = _recompute_contract_digest(sql_path, digest_function)
        print(f"       schema contract digest (clean schema): {digest}")
        _update_expected_digest(sql_path, digest, dry_run=dry_run)
        print(f"       expected_contract_digest {'would be' if dry_run else 'updated'} -> {digest}")
    print("[DONE] 同步完成。重跑 scripts/run_db_migrations.py 验证全链。")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(_PROJECT_ROOT))
    raise SystemExit(main())
