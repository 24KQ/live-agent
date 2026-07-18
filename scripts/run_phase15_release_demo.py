"""Phase 15 Task 12 三场景业务闭环与 Release Acceptance 演示。

该入口把 Phase 14 已验证的 PREPARE/LIVE/REVIEW 内存闭环和 Phase 15 的本地
确定性 Release 内核组合成一份审计报告。它不连接真实模型、真人采集、GitHub
Actions、淘宝 API 或生产数据库；缺少这些外部证据时必须输出 INCONCLUSIVE。
"""

from __future__ import annotations

from enum import StrEnum
import json
from pathlib import Path
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # 直接执行 scripts/*.py 时补充仓库根目录，保持脚本导入和 pytest 一致。
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_phase14_human_support_demo import DemoResult, run_demo as run_business_demo
from scripts.run_release_gate import _parser, run_release_gate


class Phase15AcceptanceStatus(StrEnum):
    """Phase 15 阶段级结论；外部证据缺失不能被本地 dry-run 覆盖。"""

    PASS = "PASS"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAIL = "FAIL"


class Phase15DemoResult(BaseModel):
    """三场景事实、两次路由 Release 和外部阻断原因的不可变投影。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Phase15AcceptanceStatus
    phase_state: str = Field(default="PHASE_15_COMPLETE_INCONCLUSIVE", min_length=1)
    frozen_case_count: int = Field(default=48, strict=True, ge=48, le=48)
    business_loop: DemoResult
    release_runs: tuple[dict[str, Any], ...] = Field(..., min_length=2, max_length=2)
    external_evidence_status: str = Field(..., min_length=1)
    promotion_status: str = Field(..., min_length=1)
    final_route: str = Field(..., min_length=1)
    phase15_model_cost_cny: str = Field(..., pattern=r"^\d+\.\d{6}$")
    blocker_codes: tuple[str, ...] = ()


def _run_local_release(route_profile: str) -> dict[str, Any]:
    """运行不需要外部服务的本地 Release profile，禁止触发真实模型。"""

    args = _parser().parse_args(
        [
            "--mode",
            "pr",
            "--subject",
            "all",
            "--route-profile",
            route_profile,
        ]
    )
    return run_release_gate(args)


def run_demo(evaluation_root: Path) -> Phase15DemoResult:
    """回放三场景闭环并运行两次本地路由 Release。"""

    business = run_business_demo(Path(evaluation_root))
    explicit = _run_local_release("EXPLICIT_RELEASE")
    verified = _run_local_release("VERIFIED_DEFAULTS")

    blockers = (
        "REAL_MODEL_SMOKE_NOT_RUN",
        "HUMAN_STUDY_EVIDENCE_MISSING",
        "GITHUB_ACTIONS_EVIDENCE_MISSING",
    )
    local_release_ok = all(
        result.get("technical", {}).get("status") == "PASS"
        for result in (explicit, verified)
    )
    status = (
        Phase15AcceptanceStatus.FAIL
        if business.status.value == "FAIL" or not local_release_ok
        else Phase15AcceptanceStatus.INCONCLUSIVE
    )
    return Phase15DemoResult(
        status=status,
        business_loop=business,
        release_runs=(explicit, verified),
        external_evidence_status="BLOCKED",
        promotion_status=str(verified.get("promotion", {}).get("status", "BLOCKED")),
        final_route=str(
            verified.get("route_profile", {}).get(
                "decision_support", "DETERMINISTIC_ONLY"
            )
        ),
        # Task 12 只运行 ScriptedModel/确定性 Runner；Phase 14 的历史费用不计入
        # 本阶段，避免把旧阶段费用伪装成新的 Phase 15 smoke 证据。
        phase15_model_cost_cny="0.000000",
        blocker_codes=blockers,
    )


def render_acceptance_report(result: Phase15DemoResult) -> str:
    """以固定顺序渲染 Phase 15 和 Final Acceptance 共用的事实报告。"""

    business = result.business_loop.model_dump(mode="json")
    explicit, verified = result.release_runs
    lines = [
        "# Phase 15 Golden Release Gates Acceptance",
        "",
        "本报告只包含本地确定性演练和真实外部证据状态，不把 dry-run、ScriptedModel 或模拟人工数据冒充生产发布证据。",
        "",
        f"- Acceptance status: `{result.status.value}`",
        f"- Phase state: `{result.phase_state}`",
        f"- Frozen Golden cases: `{result.frozen_case_count}` (local PR run uses 36 non-holdout cases)",
        f"- External evidence: `{result.external_evidence_status}`",
        f"- Promotion status: `{result.promotion_status}`",
        f"- Decision Support route: `{result.final_route}`",
        f"- Phase 15 model cost: `{result.phase15_model_cost_cny} CNY`",
        "",
        "## Three-Scene Business Loop",
        "",
        f"- Live session: `{business['live_session_id']}`",
        f"- Views: `{', '.join(business['views'])}`",
        f"- Replay stable: `{str(business['replay_stable']).lower()}`",
        f"- Automatic protection: `{business['automatic_protection_status']}`",
        f"- Operator decision: `{business['operator_decision_kind']}`",
        f"- Execution command submitted: `{str(business['execution_command_submitted']).lower()}`",
        f"- Memory promotion: `{business['memory_promotion_status']}`",
        f"- Memory replay: `{business['memory_replay_status']}`",
        "",
        "## Two Local Release Profiles",
        "",
        f"- Explicit Release technical status: `{explicit['technical']['status']}`",
        f"- Explicit Release route: `{explicit['route_profile']['mode']}`",
        f"- Verified Defaults technical status: `{verified['technical']['status']}`",
        f"- Verified Defaults route: `{verified['route_profile']['mode']}`",
        f"- Verified Defaults Decision Support: `{verified['route_profile']['decision_support_policy']['route']}`",
        f"- Local final status: `{verified['final']['status']}`",
        "",
        "## External Blockers",
        "",
    ]
    lines.extend(f"- `{code}`" for code in result.blocker_codes)
    lines.extend(
        [
            "",
            "本地技术 dry-run 已完成，但真实模型、真人对照和托管 Release evidence 未提供，因此阶段结论保持 INCONCLUSIVE，默认路由保持确定性控制面。",
            "Phase 15 完成后不自动进入下一阶段；当前状态是 no automatic next phase。",
            "",
        ]
    )
    return "\n".join(lines)


def write_acceptance_reports(root: Path, result: Phase15DemoResult) -> tuple[Path, Path]:
    """写入 Phase 15 Acceptance 与 Agent Runtime Final Acceptance。"""

    output_root = Path(root)
    output_root.mkdir(parents=True, exist_ok=True)
    phase_report = output_root / "phase-15-golden-release-gates-acceptance.md"
    final_report = output_root / "agent-runtime-final-acceptance.md"
    report = render_acceptance_report(result)
    phase_report.write_text(report, encoding="utf-8", newline="\n")
    final_report.write_text(
        report.replace(
            "# Phase 15 Golden Release Gates Acceptance",
            "# Agent Runtime Final Acceptance",
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return phase_report, final_report


def main() -> int:
    """运行本地演示、写入两个报告并返回稳定退出码。"""

    result = run_demo(PROJECT_ROOT / "evaluation")
    write_acceptance_reports(PROJECT_ROOT / "docs" / "superpowers" / "reports", result)
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if result.status is not Phase15AcceptanceStatus.FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
