"""Phase 15 Task 8 统一 Release CLI。

这个入口只编排已经冻结的 Dataset、Subject Runner、Release Store 和双轨报告，
不重新实现技术门禁，也不在 PR/Nightly 路径调用模型。每个 Subject 使用独立的
ReleaseRun，``--subject all`` 再按固定顺序汇总五类受限 Subject 的 48 个 case。
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # 直接执行 ``python scripts/run_release_gate.py`` 时，Python 默认只把
    # scripts 目录放入 sys.path；这里显式加入项目根，保持 CLI 与 pytest 导入一致。
    sys.path.insert(0, str(ROOT))

import psycopg

from src.release_gates.dataset import GoldenCase, GoldenSplit, load_phase15_dataset
from src.release_gates.decisions import (
    DecisionSupportPromotionDecision,
    ReleaseCaseResult,
    ReleaseMode,
    ReleaseRun,
    TechnicalReleaseDecision,
    TechnicalReleaseStatus,
)
from src.release_gates.models import SubjectKind, SubjectManifest, SubjectObservation
from src.release_gates.report import build_release_report, render_release_report_json
from src.release_gates.runner import (
    DecisionSupportRunner,
    EventRuntimeRunner,
    LifecycleRunner,
    PlanEngineRunner,
    SkillRuntimeRunner,
)
from src.release_gates.store import InMemoryReleaseStore
from src.specialist_runtime.models import EvidenceKind, EvidenceRef, canonical_json_sha256
from scripts.check_coverage_gate import evaluate_coverage
from scripts.fetch_github_actions_evidence import EvidenceValidationError, load_and_validate_evidence


SUBJECT_KINDS: dict[str, SubjectKind] = {
    "skill-runtime": SubjectKind.SKILL_RUNTIME,
    "plan-engine": SubjectKind.PLAN_ENGINE,
    "event-runtime": SubjectKind.EVENT_RUNTIME,
    "decision-support": SubjectKind.DECISION_SUPPORT,
    "lifecycle": SubjectKind.LIFECYCLE,
}
SUBJECT_ORDER = tuple(SUBJECT_KINDS)
SUBJECT_DOMAINS: dict[str, frozenset[str]] = {
    "skill-runtime": frozenset({"RUNTIME_SKILL"}),
    "plan-engine": frozenset({"RUNTIME_PLAN"}),
    "event-runtime": frozenset({"RUNTIME_EVENT"}),
    "decision-support": frozenset({"LIVE"}),
    "lifecycle": frozenset({"PREPARE", "REVIEW"}),
}
RUNNERS = {
    SubjectKind.SKILL_RUNTIME: SkillRuntimeRunner,
    SubjectKind.PLAN_ENGINE: PlanEngineRunner,
    SubjectKind.EVENT_RUNTIME: EventRuntimeRunner,
    SubjectKind.DECISION_SUPPORT: DecisionSupportRunner,
    SubjectKind.LIFECYCLE: LifecycleRunner,
}


class ReleaseCliError(ValueError):
    """输入或外部前置条件不满足时使用的稳定 CLI 错误。"""

    def __init__(self, code: str, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _emit(payload: dict[str, Any]) -> None:
    """输出排序后的单行 JSON，方便 CI 和人工恢复同时读取。"""

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    """创建不使用 ``choices`` 的解析器，让非法 mode 能返回稳定码而非 SystemExit。"""

    parser = argparse.ArgumentParser(description="Phase 15 deterministic release gate")
    parser.add_argument("--mode", required=True)
    parser.add_argument("--subject", default="all")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--evaluation-root", type=Path, default=ROOT / "evaluation")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--budget-cny", default="0.60")
    parser.add_argument("--require-database", action="store_true")
    parser.add_argument("--database-url")
    parser.add_argument("--require-external-evidence", action="store_true")
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--coverage-file", type=Path)
    parser.add_argument("--line", type=float, default=90.0)
    parser.add_argument("--branch", type=float, default=85.0)
    parser.add_argument("--repo")
    parser.add_argument("--run-id")
    parser.add_argument("--workflow")
    parser.add_argument("--commit-sha")
    parser.add_argument("--artifact-digest")
    return parser


def _subject_key(value: str) -> str:
    """校验 Subject 别名，禁止隐式把未知 Subject 映射成安全默认值。"""

    if value == "all" or value in SUBJECT_KINDS:
        return value
    raise ReleaseCliError("INVALID_SUBJECT", f"unknown subject: {value}", exit_code=2)


def _mode(value: str) -> ReleaseMode:
    """把命令行小写模式绑定到状态模型的冻结枚举。"""

    try:
        return ReleaseMode(value.upper())
    except ValueError as exc:
        raise ReleaseCliError("INVALID_MODE", f"unknown release mode: {value}", exit_code=2) from exc


def _load_manifest(subject: str, path: Path | None) -> SubjectManifest:
    """加载自定义 Subject Manifest，并强制验证命令行 Subject 身份。"""

    expected = SubjectManifest(
            subject_id=f"phase15-{subject}",
            subject_version="1.0.0",
            subject_kind=SUBJECT_KINDS[subject],
            required_evidence_kinds=(EvidenceKind.AUDIT,),
            result_schema={"type": "object"},
            max_model_calls=0,
            max_skill_calls=0,
            max_cost_cny=Decimal("0"),
            no_fallback=True,
        )
    if path is None:
        return expected
    try:
        manifest = SubjectManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseCliError("MANIFEST_INVALID", "subject manifest could not be loaded", exit_code=2) from exc
    if manifest.subject_kind is not SUBJECT_KINDS[subject]:
        raise ReleaseCliError(
            "MANIFEST_SUBJECT_MISMATCH",
            "manifest subject kind does not match requested subject",
            exit_code=2,
        )
    if (
        manifest.subject_id != expected.subject_id
        or manifest.subject_version != expected.subject_version
        or manifest.manifest_digest != expected.manifest_digest
    ):
        raise ReleaseCliError(
            "MANIFEST_NOT_FROZEN",
            "manifest identity or digest is not the frozen Phase 15 Subject manifest",
            exit_code=2,
        )
    return manifest


def _evidence(case: GoldenCase) -> EvidenceRef:
    """把 case 身份转成最小审计引用，不把 case 正文写入 Subject 结果。"""

    raw_refs = case.input.get("evidence_refs")
    if isinstance(raw_refs, list) and raw_refs:
        first = raw_refs[0]
        if isinstance(first, dict):
            try:
                return EvidenceRef.model_validate(first)
            except ValueError:
                pass
    digest = canonical_json_sha256({"case_id": case.case_id, "domain": case.domain})
    return EvidenceRef(
        kind=EvidenceKind.AUDIT,
        evidence_id=f"phase15-evidence-{case.case_id}",
        source_version="phase15-runtime-v1",
        digest=digest,
    )


def _deterministic_subject(case: GoldenCase) -> SubjectObservation:
    """提供本地发布演练的确定性事实，永远不触发模型或经营写操作。"""

    return SubjectObservation(
        output={"case_id": case.case_id, "decision": "operator_review_required"},
        evidence_refs=(_evidence(case),),
        model_calls=0,
        cost_cny=Decimal("0"),
        write_attempted=False,
        fallback_used=False,
    )


def _check_database(url: str | None) -> None:
    """只在显式要求时探测 PostgreSQL，连接失败明确转换成 BLOCKED。"""

    if not url:
        raise ReleaseCliError("DATABASE_UNAVAILABLE", "database URL is required", exit_code=3)
    try:
        with psycopg.connect(url, connect_timeout=1) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
    except psycopg.Error as exc:
        raise ReleaseCliError("DATABASE_UNAVAILABLE", "database is unavailable", exit_code=3) from exc


def _gate_fact(callback: Any, *, default_code: str) -> tuple[dict[str, object], str | None]:
    """执行一个强制外部门禁并把异常收敛为报告事实，不丢失其它门禁结果。"""

    try:
        return {"status": "PASS", "evidence": callback()}, None
    except ReleaseCliError as exc:
        return {"status": "BLOCKED", "reason_code": exc.code}, exc.code
    except EvidenceValidationError as exc:
        return {"status": "BLOCKED", "reason_code": exc.code or default_code}, exc.code or default_code


def _run_subject(
    *,
    subject: str,
    mode: ReleaseMode,
    dataset: Any,
    manifest: SubjectManifest,
) -> dict[str, Any]:
    """在内存 Release Store 中执行一个 Subject 的完整 append/finalize 流程。"""

    cases = tuple(
        case
        for case in dataset.cases
        if case.domain in SUBJECT_DOMAINS[subject]
        and (mode is ReleaseMode.RELEASE or case.split is not GoldenSplit.HOLDOUT)
    )
    run_id = f"phase15-{mode.value.lower()}-{subject}-v1"
    run = ReleaseRun(
        release_run_id=run_id,
        mode=mode,
        manifest_digest=manifest.manifest_digest,
        expected_case_ids=tuple(case.case_id for case in cases),
    )
    store = InMemoryReleaseStore()
    store.create_run(run)
    runner = RUNNERS[manifest.subject_kind](manifest)
    for case in cases:
        evaluated = runner.run_case(case, _deterministic_subject)
        store.append_case_result(
            ReleaseCaseResult(
                release_run_id=run_id,
                manifest_digest=manifest.manifest_digest,
                case_id=evaluated.case_id,
                subject_id=evaluated.subject_id,
                subject_version=evaluated.subject_version,
                status=evaluated.status,
                severe_violation=evaluated.severe_violation,
                rule_codes=evaluated.rule_codes,
                summary=evaluated.summary,
                output=evaluated.output,
                evidence_refs=evaluated.evidence_refs,
            )
        )
    technical = store.finalize_technical(run_id)
    promotion = DecisionSupportPromotionDecision.blocked("MODEL_AND_HUMAN_EVIDENCE_MISSING")
    store.save_promotion(run_id, promotion)
    report = build_release_report(technical=technical, promotion=promotion)
    return {
        "subject": subject,
        "manifest_digest": manifest.manifest_digest,
        "report": json.loads(render_release_report_json(report)),
    }


def _aggregate(
    *,
    mode: ReleaseMode,
    subject: str,
    dataset: Any,
    reports: list[dict[str, Any]],
    gate_facts: dict[str, object],
    gate_blockers: tuple[str, ...],
) -> dict[str, Any]:
    """聚合五个 Subject 的技术计数，同时保留每个子 Run 的完整报告。"""

    technicals = [item["report"]["technical"] for item in reports]
    status = TechnicalReleaseStatus.BLOCKED if gate_blockers else TechnicalReleaseStatus.PASS
    if any(item["status"] == TechnicalReleaseStatus.BLOCKED.value for item in technicals):
        status = TechnicalReleaseStatus.BLOCKED
    elif any(item["status"] == TechnicalReleaseStatus.FAIL.value for item in technicals):
        status = TechnicalReleaseStatus.FAIL
    expected = sum(item["expected_case_count"] for item in technicals)
    completed = sum(item["completed_case_count"] for item in technicals)
    passed = sum(item["passed_case_count"] for item in technicals)
    failed = sum(item["failed_case_count"] for item in technicals)
    blocked = sum(item["blocked_case_count"] for item in technicals)
    severe = sum(item["severe_violation_count"] for item in technicals)
    aggregate_id = f"phase15-{mode.value.lower()}-{subject}-v1"
    technical = TechnicalReleaseDecision(
        release_run_id=aggregate_id,
        status=status,
        expected_case_count=expected,
        completed_case_count=completed,
        passed_case_count=passed,
        failed_case_count=failed,
        blocked_case_count=blocked,
        severe_violation_count=severe,
        blocking_gate_count=len(gate_blockers),
        case_results_digest=canonical_json_sha256(technicals),
        reason_codes=tuple(
            sorted(
                {reason for item in technicals for reason in item["reason_codes"]}
                | set(gate_blockers)
            )
        ),
    )
    promotion = DecisionSupportPromotionDecision.blocked("MODEL_AND_HUMAN_EVIDENCE_MISSING")
    report = build_release_report(technical=technical, promotion=promotion)
    return {
        "mode": mode.value,
        "subject": subject,
        "manifest_digest": canonical_json_sha256([item["manifest_digest"] for item in reports]),
        "dataset_digest": dataset.manifest.dataset_digest,
        "external_calls": False,
        "technical": report.technical.model_dump(mode="json"),
        "promotion": report.promotion.model_dump(mode="json"),
        "final": report.final.model_dump(mode="json"),
        "report_digest": report.report_digest,
        "gate_facts": gate_facts,
        "subjects": reports,
    }


def run_release_gate(args: argparse.Namespace) -> dict[str, Any]:
    """执行一次本地 Release 评估并返回可写入 artifact 的 JSON。"""

    mode = _mode(args.mode)
    subject = _subject_key(args.subject)
    try:
        budget = Decimal(args.budget_cny)
    except (InvalidOperation, TypeError) as exc:
        raise ReleaseCliError("INVALID_BUDGET", "budget must be a decimal number", exit_code=2) from exc
    if not budget.is_finite() or budget < 0 or budget > Decimal("0.60"):
        raise ReleaseCliError("BUDGET_OUT_OF_RANGE", "Phase 15 local budget is capped at 0.60 CNY", exit_code=2)
    gate_facts: dict[str, object] = {
        "database": {"status": "NOT_REQUIRED"},
        "coverage": {"status": "NOT_REQUIRED"},
        "external_actions": {"status": "NOT_REQUIRED"},
    }
    gate_blockers: list[str] = []
    if args.require_database or mode is ReleaseMode.RELEASE:
        fact, blocker = _gate_fact(lambda: (_check_database(args.database_url) or {}), default_code="DATABASE_UNAVAILABLE")
        gate_facts["database"] = fact
        if blocker:
            gate_blockers.append(blocker)
    if args.coverage_file is not None or mode is ReleaseMode.RELEASE:
        coverage = evaluate_coverage(args.coverage_file, line_required=args.line, branch_required=args.branch)
        gate_facts["coverage"] = coverage
        if coverage["status"] != "PASS":
            gate_blockers.append(str(coverage["reason_code"]))
    if args.require_external_evidence or mode is ReleaseMode.RELEASE:
        fact, blocker = _gate_fact(
            lambda: load_and_validate_evidence(
                args.evidence_file,
                require=True,
                repo=args.repo,
                run_id=args.run_id,
                workflow=args.workflow,
                commit_sha=args.commit_sha,
                artifact_digest=args.artifact_digest,
            ),
            default_code="EXTERNAL_EVIDENCE_MISSING",
        )
        gate_facts["external_actions"] = fact
        if blocker:
            gate_blockers.append(blocker)
    dataset = load_phase15_dataset(args.evaluation_root)
    frozen_dataset = load_phase15_dataset(ROOT / "evaluation")
    if dataset.manifest.manifest_digest != frozen_dataset.manifest.manifest_digest:
        raise ReleaseCliError("DATASET_NOT_FROZEN", "evaluation dataset is not the frozen Phase 15 manifest", exit_code=2)
    subjects = SUBJECT_ORDER if subject == "all" else (subject,)
    if args.manifest is not None and len(subjects) != 1:
        raise ReleaseCliError("MANIFEST_SUBJECT_MISMATCH", "custom manifest requires one subject", exit_code=2)
    reports = [
        _run_subject(
            subject=item,
            mode=mode,
            dataset=dataset,
            manifest=_load_manifest(item, args.manifest if item == subject else None),
        )
        for item in subjects
    ]
    result = _aggregate(
        mode=mode,
        subject=subject,
        dataset=dataset,
        reports=reports,
        gate_facts=gate_facts,
        gate_blockers=tuple(sorted(set(gate_blockers))),
    )
    result["budget_cny"] = str(budget)
    result["artifact_digest"] = canonical_json_sha256(result)
    return result


def main(argv: list[str] | None = None) -> int:
    """执行 CLI；所有可预期失败都以结构化 JSON 和稳定退出码返回。"""

    args = _parser().parse_args(argv)
    try:
        payload = run_release_gate(args)
    except ReleaseCliError as exc:
        _emit({"status": "BLOCKED", "reason_code": exc.code, "message": str(exc), "external_calls": False})
        return exc.exit_code
    except (OSError, ValueError, KeyError) as exc:
        _emit({"status": "BLOCKED", "reason_code": "RELEASE_INPUT_INVALID", "message": str(exc), "external_calls": False})
        return 3
    if args.output_dir is not None:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            (args.output_dir / "release-report.json").write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            (args.output_dir / "release-report.md").write_text(
                "# Phase 15 Release Gate\n\n"
                f"- Mode: `{payload['mode']}`\n"
                f"- Subject: `{payload['subject']}`\n"
                f"- Technical status: `{payload['technical']['status']}`\n"
                f"- Promotion status: `{payload['promotion']['status']}`\n"
                f"- Final status: `{payload['final']['status']}`\n"
                f"- Report digest: `{payload['report_digest']}`\n",
                encoding="utf-8",
            )
        except OSError as exc:
            _emit({"status": "BLOCKED", "reason_code": "ARTIFACT_WRITE_FAILED", "message": str(exc), "external_calls": False})
            return 3
    _emit(payload)
    if payload.get("technical", {}).get("status") == TechnicalReleaseStatus.BLOCKED.value:
        return 3
    if payload.get("technical", {}).get("status") == TechnicalReleaseStatus.FAIL.value:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
