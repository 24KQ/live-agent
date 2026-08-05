"""把独立第三方 Claude 的单例安全 verdict 追加到 Phase 17 账本。

该入口只接受固定 reviewer ``claude-independent-review``，不接受代码或测试
结论作为安全审查替代。脚本不打印 HMAC key，也不读取模型正文；正文仍由
``_probe_artifacts`` 保存，账本只保存 artifact digest 和人工摘要。

示例（真实执行前后均需用户明确批准对应操作）::

    python -u scripts/record_phase17_safety_review.py \
        --run-id phase17-holdout-... \
        --case-id phase17-holdout-danmaku-001 \
        --artifact-digest <64-char-sha256> \
        --verdict PASS \
        --summary "独立审查：拦截风险、保留人工门，未发现未授权动作。"
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config.settings import get_settings  # noqa: E402
from src.decision_support.phase16_qualification import (  # noqa: E402
    QualificationExecutionContract,
    admit_phase17_holdout_execution,
    load_phase17_holdout_execution_contract,
)
from src.decision_support.phase17_holdout_ledger import (  # noqa: E402
    PostgresPhase17HoldoutLedger,
    phase17_holdout_schema_ready,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--artifact-digest", required=True)
    parser.add_argument("--verdict", choices=("PASS", "FAIL", "INCONCLUSIVE"), required=True)
    parser.add_argument("--summary", required=True)
    args = parser.parse_args()

    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    allowed, reasons = admit_phase17_holdout_execution(
        requested_identity=QualificationExecutionContract.PHASE17_HOLDOUT_EXECUTION_V1,
        contract=contract,
    )
    if not allowed:
        print(f"[SAFETY] BLOCKED: contract admission rejected ({','.join(reasons)})")
        return 1
    hmac_hex = os.environ.get("PHASE17_HOLDOUT_RECEIPT_HMAC_HEX", "").strip()
    try:
        hmac_key = bytes.fromhex(hmac_hex)
    except ValueError:
        print("[SAFETY] BLOCKED: HMAC key is not valid hex")
        return 1
    if len(hmac_key) < 32:
        print("[SAFETY] BLOCKED: HMAC key must contain at least 256 bits")
        return 1
    settings = get_settings()
    if not phase17_holdout_schema_ready(settings):
        print("[SAFETY] BLOCKED: phase17 schema is not migrated")
        return 1
    ledger = PostgresPhase17HoldoutLedger(settings, hmac_key=hmac_key)
    try:
        ledger.record_phase17_safety_review(
            run_id=args.run_id,
            case_id=args.case_id,
            artifact_digest=args.artifact_digest,
            verdict=args.verdict,
            summary=args.summary,
            reviewer="claude-independent-review",
        )
    except Exception as exc:  # noqa: BLE001 - 账本拒绝必须显式返回
        print(f"[SAFETY] BLOCKED: review append rejected ({exc})")
        return 1
    print(
        f"[SAFETY] recorded reviewer=claude-independent-review "
        f"run={args.run_id} case={args.case_id} verdict={args.verdict}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
