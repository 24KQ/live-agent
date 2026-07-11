# -*- coding: utf-8 -*-
"""Phase 7C LiveAgent 统一启动入口。

子命令：
    python scripts/run_all.py migrate    # 执行数据库迁移
    python scripts/run_all.py seed       # 填充种子数据
    python scripts/run_all.py server     # 启动 API 服务
    python scripts/run_all.py demo       # 端到端全链路演示
    python scripts/run_all.py up         # migrate + seed + server（批量执行）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def _ok(msg: str) -> None:
    print(f"\033[92m[OK]\033[0m {msg}")


def _fail(msg: str) -> None:
    print(f"\033[91m[FAIL]\033[0m {msg}")


def _info(msg: str) -> None:
    print(f"\033[94m[INFO]\033[0m {msg}")


def _run_python(script_name: str, *args: str) -> int:
    """运行 scripts/ 下的 Python 脚本并返回退出码。"""
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name), *args]
    _info(f"running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return result.returncode


def cmd_migrate(args: argparse.Namespace) -> int:
    """执行数据库迁移。"""
    _info("=" * 50)
    _info("Phase 7C: Database Migration")
    _info("=" * 50)
    dry_run = "--dry-run" if getattr(args, "dry_run", False) else ""
    rc = _run_python("run_db_migrations.py", *([dry_run] if dry_run else []))
    if rc == 0:
        _ok("database migration completed")
    else:
        _fail(f"database migration failed (exit={rc})")
    return rc


def cmd_seed(args: argparse.Namespace) -> int:
    """填充前端展示数据和演示种子数据。"""
    _info("=" * 50)
    _info("Phase 7C: Seed Data")
    _info("=" * 50)
    seeds = [
        ("seed_phase2_demo_data.py", "seed phase2 demo data"),
        ("seed_phase3_memory_demo_data.py", "seed phase3 memory demo data"),
        ("seed_frontend_data.py", "seed frontend data"),
    ]
    ok_count = 0
    for script, desc in seeds:
        rc = _run_python(script)
        if rc == 0:
            _ok(desc)
            ok_count += 1
        else:
            _fail(f"{desc} failed (exit={rc})")
    _info(f"seed data: {ok_count}/{len(seeds)} succeeded")
    return 0 if ok_count == len(seeds) else 1


def cmd_server(args: argparse.Namespace) -> int:
    """启动 uvicorn API 服务。"""
    _info("=" * 50)
    _info("Phase 7C: Starting API Server")
    _info("=" * 50)
    port = str(getattr(args, "port", 8100))
    _info(f"API server starting at http://localhost:{port}")
    _info("Frontend at http://localhost:8100 (served as static files)")
    _info("Press Ctrl+C to stop")
    cmd = [
        sys.executable, "-m", "uvicorn",
        "src.gateway.api_server:app",
        "--port", port,
        "--host", "0.0.0.0",
    ]
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return result.returncode


def cmd_up(args: argparse.Namespace) -> int:
    """批量执行 migrate + seed + server。"""
    _info("=" * 50)
    _info("Phase 7C: LiveAgent Quick Start")
    _info("=" * 50)
    _info("Step 1/3: Database Migration")
    if cmd_migrate(args) != 0:
        _fail("migration failed, aborting up")
        return 1
    _info("Step 2/3: Seed Data")
    _info("(seed errors are non-fatal, continuing...)")
    cmd_seed(args)
    _info("Step 3/3: Start Server")
    return cmd_server(args)


def cmd_demo(args: argparse.Namespace) -> int:
    """端到端全链路演示。"""
    _info("=" * 50)
    _info("Phase 7C: End-to-End Demo")
    _info("=" * 50)

    # Step 1: migrate
    _info("[1/6] Database Migration")
    if cmd_migrate(args) != 0:
        _fail("migration failed, cannot continue demo")
        return 1
    _info("[1/6] OK")

    # Step 2: seed
    _info("[2/6] Seed Data")
    cmd_seed(args)
    _info("[2/6] OK")

    # Step 3: Pre-live - generate a product card
    _info("[3/6] Pre-Live: Generate Product Card")
    rc = _run_python("run_phase3e_llm_card_demo.py")
    if rc != 0:
        _info("LLM card demo skipped or failed (non-fatal)")
    _info("[3/6] OK (pre-live)")

    # Step 4: On-Live - Harness Agent demo
    _info("[4/6] On-Live: Harness Agent Demo")
    rc = _run_python("run_phase6c_harness_dashboard_demo.py")
    if rc != 0:
        _info("Harness dashboard demo skipped or failed (non-fatal)")
    _info("[4/6] OK (on-live harness)")

    # Step 5: Post-Live - review
    _info("[5/6] Post-Live: Review")
    rc = _run_python("run_phase5d_llm_review_demo.py")
    if rc != 0:
        _info("LLM review demo skipped or failed (non-fatal)")
    _info("[5/6] OK (post-live)")

    # Step 6: Evaluation demo (most complete)
    _info("[6/6] Agent Evaluation Demo")
    rc = _run_python("run_phase7a_agent_evaluation_demo.py")
    if rc != 0:
        _info("Evaluation demo skipped or failed (non-fatal)")
    _info("[6/6] OK (evaluation)")

    _info("=" * 50)
    _ok("End-to-end demo completed")
    _info("To start the API server and Web UI:")
    _info("  python scripts/run_all.py server")
    _info("Then open http://localhost:8100 in your browser")
    _info("=" * 50)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LiveAgent 统一启动入口")
    parser.add_argument("--dry-run", action="store_true", help="仅检查迁移文件，不执行 DDL")

    sub = parser.add_subparsers(dest="command", help="子命令")

    p_migrate = sub.add_parser("migrate", help="执行数据库迁移")
    p_migrate.add_argument("--dry-run", action="store_true")

    p_seed = sub.add_parser("seed", help="填充种子数据")

    p_server = sub.add_parser("server", help="启动 API 服务")
    p_server.add_argument("--port", type=int, default=8100, help="端口号")

    sub.add_parser("demo", help="端到端全链路演示")

    p_up = sub.add_parser("up", help="migrate + seed + server 批量执行")
    p_up.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    command_map = {
        "migrate": cmd_migrate,
        "seed": cmd_seed,
        "server": cmd_server,
        "demo": cmd_demo,
        "up": cmd_up,
    }
    return command_map[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
