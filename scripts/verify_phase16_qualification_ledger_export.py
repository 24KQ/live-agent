"""Phase 16 qualification 账本只读导出核验（第三方复验入口）。

本脚本对 PostgreSQL append-only 账本 `phase16_qualification_*` 执行**只读**
（纯 SELECT）聚合统计，并对照权威值断言；全部通过时退出码 0，否则非零。

用途（配合 `docs/superpowers/reports/phase-16-v9-contract-approval-record.md` §5）：
- 任何人可在自己的环境重跑本脚本，把输出与批准记录中的权威值逐项比对；
- 脚本自身 sha256（运行末尾打印）固定在批准记录中，防"换脚本取数"。

设计约束：
- 绝不打印任何凭据 / API key / 连接串；只输出聚合统计与断言结果。
- `.env` 只 load-into-process（不落盘、不回显），DB 密码不进入输出。
- 只读：所有查询均为 SELECT，不执行任何 DDL / DML / 迁移。

用法：
    python -u scripts/verify_phase16_qualification_ledger_export.py
"""

from __future__ import annotations

from hashlib import sha256
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

sys.path.insert(0, str(_PROJECT_ROOT))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from src.config.settings import get_settings  # noqa: E402


#: 权威值（2026-08-02 账本核验，见 approval record §5）。
_AUTHORITATIVE = {
    "campaigns_total": 13,           # 9 DEVELOPMENT + 4 VALIDATION
    "campaigns_development": 9,
    "campaigns_validation": 4,
    "runs_total": 12,                # 8 PASS + 4 FAILED
    "runs_pass": 8,
    "runs_failed": 4,
    "receipt_count": 271,
    "cost_cny_full_precision": "6.604131",
    "tokens_total": 1_894_873,
    "transport_attempt_count": 332,  # Σ receipt.attempt_count（含 24×3 注入 run）
    "legacy_rows_host_null": 46,     # attempt 列迁移前（candidate-1 + cdd63444）
    "incomplete_receipts": 0,
    "policy_digest_variants": 8,
}


def _aggregate(connection) -> dict:
    """纯 SELECT 聚合；返回与 _AUTHORITATIVE 同键的统计。"""
    queries = {
        "campaigns_total": 'SELECT count(*) FROM phase16_qualification_campaigns',
        "campaigns_development": (
            "SELECT count(*) FROM phase16_qualification_campaigns"
            " WHERE campaign_kind='DEVELOPMENT'"
        ),
        "campaigns_validation": (
            "SELECT count(*) FROM phase16_qualification_campaigns"
            " WHERE campaign_kind='VALIDATION'"
        ),
        "runs_total": 'SELECT count(*) FROM phase16_qualification_runs',
        "runs_pass": (
            "SELECT count(*) FROM phase16_qualification_results WHERE status='PASS'"
        ),
        "runs_failed": (
            "SELECT count(*) FROM phase16_qualification_results WHERE status='FAILED'"
        ),
        "receipt_count": (
            "SELECT count(*) FROM phase16_qualification_provider_receipts"
        ),
        "cost_cny_full_precision": (
            "SELECT to_char(sum(actual_cost_cny), 'FM999990.999999')"
            " FROM phase16_qualification_provider_receipts"
        ),
        "tokens_total": (
            "SELECT sum(COALESCE(total_tokens, 0))"
            " FROM phase16_qualification_provider_receipts"
        ),
        "transport_attempt_count": (
            "SELECT sum(COALESCE(attempt_count, 1))"
            " FROM phase16_qualification_provider_receipts"
        ),
        "legacy_rows_host_null": (
            "SELECT count(*) FROM phase16_qualification_provider_receipts"
            " WHERE responded_endpoint_host IS NULL"
        ),
        "incomplete_receipts": (
            "SELECT count(*) FROM phase16_qualification_provider_receipts"
            " WHERE receipt_complete = FALSE"
        ),
        "policy_digest_variants": (
            "SELECT count(DISTINCT policy_digest)"
            " FROM phase16_qualification_campaigns"
        ),
    }
    result = {}
    with connection.cursor() as cursor:
        for key, statement in queries.items():
            cursor.execute(sql.SQL(statement))
            value = cursor.fetchone()[0]
            result[key] = str(value) if key == "cost_cny_full_precision" else int(value)
    return result


def main() -> int:
    kwargs = dict(get_settings().postgres_connection_kwargs)
    with psycopg.connect(**kwargs) as connection:
        actual = _aggregate(connection)

    failures = []
    for key, expected in _AUTHORITATIVE.items():
        status = "PASS" if actual[key] == expected else "FAIL"
        if status == "FAIL":
            failures.append(key)
        print(f"{status}  {key:<32} actual={actual[key]!s:>12}  expected={expected!s:>12}")

    self_digest = sha256(Path(__file__).read_bytes()).hexdigest()
    print(f"INFO script_sha256={self_digest}")

    if failures:
        print(f"RESULT FAILED: {len(failures)} mismatch(es): {failures}")
        return 1
    print("RESULT PASS: all authoritative values verified on this database")
    return 0


if __name__ == "__main__":
    sys.exit(main())
