"""Phase 15 Task 8 本地 Release CLI 与辅助门禁的 TDD 契约。

这些测试只验证稳定退出码、报告事实和 fail-closed 边界，不访问真实模型、
GitHub 或数据库。外部服务证据由独立文件显式注入，避免测试通过网络副作用。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.check_coverage_gate import main as coverage_main
from scripts.fetch_github_actions_evidence import main as evidence_main
from scripts.run_release_gate import main as release_main


def test_release_cli_rejects_unknown_mode_with_stable_exit_code(capsys) -> None:
    """未知模式必须在执行前拒绝，不能降级成默认 Release。"""

    assert release_main(["--mode", "unknown"]) == 2
    assert "INVALID_MODE" in capsys.readouterr().out


def test_release_cli_pr_is_deterministic_and_does_not_call_external_model(tmp_path: Path, capsys) -> None:
    """PR 演练复用规则内核，技术通过但缺少外部证据时保持 Copilot 禁用。"""

    output_dir = tmp_path / "artifacts"
    assert release_main(["--mode", "pr", "--output-dir", str(output_dir)]) == 0
    report = json.loads((output_dir / "release-report.json").read_text(encoding="utf-8"))
    assert report["technical"]["status"] == "PASS"
    assert report["technical"]["expected_case_count"] == 36
    assert report["promotion"]["status"] == "BLOCKED"
    assert report["final"]["status"] == "RELEASED_DECISION_SUPPORT_DISABLED"
    assert report["external_calls"] is False
    assert "PASS" in capsys.readouterr().out


def test_release_cli_can_emit_explicit_new_runtime_profile(tmp_path: Path) -> None:
    """第一次 Release 的显式新路径必须进入报告，不能依赖默认 Legacy。"""

    output_dir = tmp_path / "explicit"
    assert release_main(
        ["--mode", "pr", "--route-profile", "EXPLICIT_RELEASE", "--output-dir", str(output_dir)]
    ) == 0
    report = json.loads((output_dir / "release-report.json").read_text(encoding="utf-8"))
    assert report["route_profile"]["mode"] == "EXPLICIT_RELEASE"
    assert report["route_profile"]["skill_policy"]["batch1"] == "SKILL_RUNTIME"
    assert report["route_profile"]["plan_policy"]["route"] == "PLAN_ENGINE"
    assert report["route_profile"]["decision_support_policy"]["route"] == "DETERMINISTIC_ONLY"


def test_release_cli_blocks_verified_defaults_without_technical_pass(tmp_path: Path) -> None:
    """技术门禁未 PASS 时不能把 Verified Defaults 写入报告。"""

    output_dir = tmp_path / "verified"
    assert release_main(
        ["--mode", "release", "--route-profile", "VERIFIED_DEFAULTS", "--output-dir", str(output_dir)]
    ) == 3
    report = json.loads((output_dir / "release-report.json").read_text(encoding="utf-8"))
    assert report["technical"]["status"] == "BLOCKED"
    assert report["route_profile"] == {
        "mode": "VERIFIED_DEFAULTS",
        "reason": "technical release must PASS before default promotion",
        "reason_code": "TECHNICAL_RELEASE_NOT_PASS",
        "status": "BLOCKED",
    }


def test_release_cli_rejects_manifest_subject_mismatch(tmp_path: Path, capsys) -> None:
    """调用方不能把一个 Subject 的 Manifest 冒充成另一个 Subject。"""

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "subject_id": "phase15-skill-runtime",
                "subject_version": "1.0.0",
                "subject_kind": "SKILL_RUNTIME",
                "allowed_skill_versions": {},
                "required_evidence_kinds": [],
                "allowed_plan_states": [],
                "allowed_event_states": [],
                "result_schema": {"type": "object"},
                "max_model_calls": 0,
                "max_skill_calls": 0,
                "max_cost_cny": "0",
                "no_fallback": True,
            }
        ),
        encoding="utf-8",
    )
    assert release_main(
        ["--mode", "pr", "--subject", "decision-support", "--manifest", str(manifest)]
    ) == 2
    assert "MANIFEST_SUBJECT_MISMATCH" in capsys.readouterr().out


def test_release_cli_reports_missing_database_as_blocked(tmp_path: Path, capsys) -> None:
    """要求数据库但无法连接时必须明确 BLOCKED，不能用内存 Store 冒充。"""

    assert release_main(
        [
            "--mode",
            "nightly",
            "--require-database",
            "--database-url",
            "postgresql://127.0.0.1:1/does-not-exist",
            "--output-dir",
            str(tmp_path),
        ]
    ) == 3
    assert "DATABASE_UNAVAILABLE" in capsys.readouterr().out


def test_release_cli_reports_artifact_write_failure_without_traceback(tmp_path: Path, capsys) -> None:
    """artifact 目录不可写时必须保持稳定 JSON 退出，而不是泄露 traceback。"""

    output_path = tmp_path / "not-a-directory"
    output_path.write_text("occupied", encoding="utf-8")
    assert release_main(["--mode", "pr", "--output-dir", str(output_path)]) == 3
    output = capsys.readouterr().out
    assert "ARTIFACT_WRITE_FAILED" in output
    assert "Traceback" not in output


def test_release_mode_requires_all_external_technical_gates(tmp_path: Path, capsys) -> None:
    """Release 模式缺数据库、覆盖率或 Actions 证据时技术结论必须 BLOCKED。"""

    assert release_main(["--mode", "release", "--output-dir", str(tmp_path)]) == 3
    payload = json.loads((tmp_path / "release-report.json").read_text(encoding="utf-8"))
    assert payload["technical"]["status"] == "BLOCKED"
    assert payload["technical"]["blocking_gate_count"] >= 3
    assert payload["final"]["status"] == "NOT_RELEASED"
    assert "COVERAGE_MISSING" in payload["technical"]["reason_codes"]
    capsys.readouterr()


def test_coverage_gate_rejects_insufficient_totals(tmp_path: Path, capsys) -> None:
    """覆盖率低于冻结阈值时使用稳定非零退出码。"""

    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps({"totals": {"percent_covered": 89.9, "percent_branches_covered": 84.9}}),
        encoding="utf-8",
    )
    assert coverage_main(
        ["--coverage-file", str(coverage), "--line", "90", "--branch", "85"]
    ) == 3
    assert "COVERAGE_INSUFFICIENT" in capsys.readouterr().out


def test_coverage_gate_accepts_complete_totals(capsys, tmp_path: Path) -> None:
    """覆盖率同时达到 line/branch 门槛时返回 PASS。"""

    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps({"totals": {"percent_covered": 91.0, "percent_branches_covered": 86.0}}),
        encoding="utf-8",
    )
    assert coverage_main(["--coverage-file", str(coverage)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_release_cli_rejects_non_finite_budget(capsys) -> None:
    """NaN 不能绕过 Phase 15 人民币预算上限比较。"""

    assert release_main(["--mode", "pr", "--budget-cny", "NaN"]) == 2
    assert "BUDGET_OUT_OF_RANGE" in capsys.readouterr().out


def test_github_actions_evidence_is_blocked_when_file_is_missing(tmp_path: Path, capsys) -> None:
    """没有托管 Actions 事实时不能伪造 Release evidence。"""

    assert evidence_main(
        [
            "--evidence-file",
            str(tmp_path / "missing.json"),
            "--require-evidence",
            "--run-id",
            "123",
        ]
    ) == 3
    assert "EXTERNAL_EVIDENCE_MISSING" in capsys.readouterr().out


def test_github_actions_evidence_replays_verified_identity(tmp_path: Path, capsys) -> None:
    """固化的成功 run 只有在 repo/run 身份匹配时才可被读取为 PASS。"""

    evidence = tmp_path / "actions.json"
    evidence.write_text(
        json.dumps(
            {
                "repo": "24KQ/live-agent",
                "run_id": 123,
                "workflow": "release.yml",
                "commit_sha": "a" * 40,
                "artifact_digest": "b" * 64,
                "status": "success",
            }
        ),
        encoding="utf-8",
    )
    assert evidence_main(
        [
            "--evidence-file",
            str(evidence),
            "--require-evidence",
            "--repo",
            "24KQ/live-agent",
            "--run-id",
            "123",
            "--workflow",
            "release.yml",
            "--commit-sha",
            "a" * 40,
            "--artifact-digest",
            "b" * 64,
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert "token" not in json.dumps(payload, ensure_ascii=False).lower()
