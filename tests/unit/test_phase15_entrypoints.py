"""Phase 15 Task 1 的迁移、统一入口和敏感扫描契约测试。

这些测试只检查发布入口的静态事实，不执行 PostgreSQL、Kafka、真实模型或任何
生产写入。它们先把 Task 1 计划中容易遗漏的依赖注册、Demo 命令和 tracked-file
扫描行为固定下来，再由最小 GREEN 实现补齐入口。
"""

from __future__ import annotations

import py_compile
from pathlib import Path

from scripts.check_sensitive_payloads import main as sensitive_scan_main
from scripts.run_all import build_parser
from scripts.run_db_migrations import MIGRATIONS


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_phase15_migration_chain_registers_all_release_prerequisites() -> None:
    """迁移清单必须覆盖 Phase 13 Memory、Phase 14 事实和 Phase 15 Release 表。"""

    expected = (
        ("phase13", "init_phase13_specialist_evaluations.sql"),
        ("phase13_memory", "init_phase13_memory_candidates.sql"),
        ("phase14_decision_support", "init_phase14_decision_support.sql"),
        ("phase14_memory_feedback", "init_phase14_memory_feedback.sql"),
        ("phase15", "init_phase15_release_gates.sql"),
    )
    by_phase = {step.phase: step for step in MIGRATIONS}

    for phase, sql_file in expected:
        assert phase in by_phase
        assert by_phase[phase].sql_file == sql_file
        assert by_phase[phase].required is True
        assert (PROJECT_ROOT / "docker" / sql_file).is_file()

    phases = [step.phase for step in MIGRATIONS]
    positions = [phases.index(phase) for phase, _ in expected]
    assert positions == sorted(positions)


def test_unified_entrypoint_exposes_three_phase_demos() -> None:
    """Phase 13、14、15 Demo 必须从同一入口发现并路由。"""

    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if getattr(action, "dest", None) == "command"
    )

    for command in ("phase13-demo", "phase14-demo", "phase15-demo"):
        assert command in subparsers.choices

    for script_name in (
        "run_phase13_specialist_demo.py",
        "run_phase14_human_support_demo.py",
        "run_phase15_release_demo.py",
    ):
        assert (PROJECT_ROOT / "scripts" / script_name).is_file()


def test_sensitive_scanner_compiles_and_scans_git_tracked_files() -> None:
    """tracked 扫描必须可编译，并且只对 Git 已跟踪文件执行严格扫描。"""

    scanner = PROJECT_ROOT / "scripts" / "check_sensitive_payloads.py"
    py_compile.compile(str(scanner), doraise=True)
    # 直接调用 CLI 入口，避免 Windows 测试进程内再次创建 Python 子进程。
    assert sensitive_scan_main(["--tracked"]) == 0


def test_phase15_entrypoint_documentation_and_coverage_dependency_are_declared() -> None:
    """README 与开发依赖必须让发布门禁入口可发现、可复跑。"""

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for command in ("phase13-demo", "phase14-demo", "phase15-demo"):
        assert f"python scripts/run_all.py {command}" in readme
    assert "pytest-cov" in pyproject
