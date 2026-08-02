"""Phase 17 holdout 执行契约 CLI：契约身份路由与准入探针。

阶段②形态：只做 dry-run 准入检查（加载契约 → 身份路由 → 预算/闭包断言），
**绝不调用真实模型**；阶段③真实执行在每次 run 前经用户单独批准后扩展本脚本。

设计约束：
- 执行入口只接受 `PHASE17_HOLDOUT_EXECUTION_V1`；v2 历史契约走 v2 既有
  fail-closed load 路径；v3 回溯契约无执行身份，运行时拒绝。
- 不读取/不打印 .env；不写执行账本；不触碰 v2/v3 manifest。
- 文档 UTF-8/LF/无 BOM。

用法：
    python -u scripts/run_phase17_holdout.py --probe
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.decision_support.phase16_qualification import (  # noqa: E402
    PHASE17_HOLDOUT_EXECUTION_FORWARD_BUDGET_REMAINING_CNY,
    PHASE17_HOLDOUT_EXECUTION_PROJECT_BUDGET_CNY,
    PHASE17_HOLDOUT_EXECUTION_RETROSPECTIVE_ACTUAL_CNY,
    PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT,
    PHASE17_HOLDOUT_TOTAL_E2E_PASS_MIN,
    QualificationExecutionContract,
    admit_phase17_holdout_execution,
    load_phase17_holdout_execution_contract,
)


def _probe() -> int:
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    print(f"[phase17] contract_id={contract.contract_id} digest={contract.contract_digest}")
    print(f"[phase17] parent_v3={contract.parent_v3_evaluation_digest[:12]}...")
    print(
        f"[phase17] budget project={contract.project_budget_cny} "
        f"retrospective={contract.retrospective_budget_actual_cny} "
        f"forward_remaining={contract.forward_budget_remaining_cny}"
    )
    batches = ", ".join(f"batch{b['batch_index']}:{b['case_count']}" for b in contract.holdout_batches)
    print(
        f"[phase17] holdout={contract.holdout_case_count} cases "
        f"[{batches}] total_pass_min={contract.holdout_total_e2e_pass_min}"
    )
    allowed, reasons = admit_phase17_holdout_execution(
        requested_identity=QualificationExecutionContract.PHASE17_HOLDOUT_EXECUTION_V1,
        contract=contract,
    )
    if not allowed:
        print(f"[phase17] ADMISSION_REJECTED reasons={','.join(reasons)}")
        return 1
    print("[phase17] ADMISSION_OK (dry-run, no model call)")
    print("[phase17] identity routing: PHASE17_HOLDOUT_EXECUTION_V1 accepted; "
          "V2_HISTORICAL_EXECUTION via v2 fail-closed; v3 read-only rejected")
    return 0


def _reject_wrong_identity() -> int:
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    allowed, reasons = admit_phase17_holdout_execution(
        requested_identity=QualificationExecutionContract.V2_HISTORICAL_EXECUTION,
        contract=contract,
    )
    print(
        f"[phase17] v2 identity -> allowed={allowed} reasons={','.join(reasons)} "
        "(expected: rejected, 历史入口不接受 phase17 契约)"
    )
    return 0 if not allowed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="dry-run 契约加载与身份路由准入检查（不调用模型）",
    )
    parser.add_argument(
        "--reject-v2-identity",
        action="store_true",
        help="验证 v2 历史执行身份在 phase17 入口被拒绝",
    )
    args = parser.parse_args()
    if args.reject_v2_identity:
        return _reject_wrong_identity()
    return _probe()


if __name__ == "__main__":
    raise SystemExit(main())
