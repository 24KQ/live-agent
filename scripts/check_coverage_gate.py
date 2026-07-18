"""Phase 15 Task 8 覆盖率门禁入口。

只读取 pytest-cov JSON 的 totals，不执行测试、不修改 coverage artifact。缺失或
无法解析的报告按 BLOCKED 处理，防止 CI 把“没有证据”当成 0% 或通过。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def _emit(payload: dict[str, object]) -> None:
    """用稳定 JSON 输出门禁结论。"""

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    """构造覆盖率入口参数。"""

    parser = argparse.ArgumentParser(description="Phase 15 coverage gate")
    parser.add_argument("--coverage-file", type=Path, default=Path("coverage.json"))
    parser.add_argument("--line", type=float, default=90.0)
    parser.add_argument("--branch", type=float, default=85.0)
    return parser


def evaluate_coverage(path: Path | None, *, line_required: float, branch_required: float) -> dict[str, object]:
    """读取并返回覆盖率事实，供本地 CLI 和托管工作流共享同一判定。"""

    if line_required < 0 or branch_required < 0:
        return {"status": "BLOCKED", "reason_code": "INVALID_THRESHOLD"}
    if path is None or not path.is_file():
        return {"status": "BLOCKED", "reason_code": "COVERAGE_MISSING"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        totals = payload["totals"]
        line = float(totals["percent_covered"])
        branch = float(totals["percent_branches_covered"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {"status": "BLOCKED", "reason_code": "COVERAGE_INVALID"}
    passed = math.isfinite(line) and math.isfinite(branch) and line >= line_required and branch >= branch_required
    return {
        "status": "PASS" if passed else "BLOCKED",
        "reason_code": "" if passed else "COVERAGE_INSUFFICIENT",
        "line_percent": line,
        "branch_percent": branch,
        "line_required": line_required,
        "branch_required": branch_required,
    }


def main(argv: list[str] | None = None) -> int:
    """检查 line/branch 两个硬阈值并返回 0/3/2。"""

    args = _parser().parse_args(argv)
    result = evaluate_coverage(args.coverage_file, line_required=args.line, branch_required=args.branch)
    _emit(result)
    return 0 if result["status"] == "PASS" else 2 if result["reason_code"] == "INVALID_THRESHOLD" else 3


if __name__ == "__main__":
    raise SystemExit(main())
