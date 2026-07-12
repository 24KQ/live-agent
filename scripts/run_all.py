# -*- coding: utf-8 -*-
"""Phase 7C LiveAgent 统一启动入口。

子命令：
    python scripts/run_all.py migrate    # 执行数据库迁移
    python scripts/run_all.py seed       # 填充种子数据
    python scripts/run_all.py server     # 启动 API 服务
    python scripts/run_all.py demo       # 端到端全链路演示
    python scripts/run_all.py phase11a-demo  # Phase 11A 无外部依赖路由演示
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


def _run_demo_mock() -> int:
    """PostgreSQL不可用时的降级演示模式，输出模拟文本。"""
    bt = chr(96)
    _info("=" * 50)
    _info("Mock Demo Mode - PostgreSQL unavailable")
    _info("=" * 50)
    _info("")
    _info("Phase 7C Demo - End-to-End Pipeline")
    _info("")
    _info("Step 1: Pre-Live - Product Card Generation")
    _info("  Product: 夏季连衣裙新款")
    _info("  Price: 299.00")
    _info("  Suggestion: 强调透气面料和限时优惠")
    _info("")
    _info("Step 2: On-Live - Danmaku Aggregation")
    _info('  Top issues: ["价格太贵了 x12", "有没有其他颜色 x8", "尺码怎么选 x5"]')
    _info("  Agent Decision: generate_on_live_prompt")
    _info("")
    _info("Step 3: On-Live - Harness Agent + Interrupt")
    _info("  Event: inventory_alert for product p001")
    _info("  Risk Level: HIGH")
    _info("  Tool: handle_sold_out_event")
    _info("  Status: pending_human -> approved")
    _info("  Outcome: tool executed, suggestion generated")
    _info("")
    _info("Step 4: Post-Live - Review")
    _info("  Total Decisions: 12")
    _info("  Adoption Rate: 75.0 percent")
    _info("  Accuracy Rate: 83.3 percent")
    _info("  Issues Found: 2")
    _info("")
    _info("Step 5: Evaluation - Replay + Score")
    _info("  Replay Fidelity: full")
    _info("  Overall Score: 96.11")
    _info("  Coverage: 90.0 percent")
    _info("  Verdict: PASS")
    _info("  Violations: none")
    _info("")
    _info("=" * 50)
    _ok("Mock demo completed (no database required)")
    _info("To start the API server and Web UI:")
    _info("  python scripts/run_all.py server")
    _info("Then open http://localhost:8100")
    _info("=" * 50)
    return 0



def cmd_story(args: argparse.Namespace) -> int:
    """\u8fd0\u884c\u7aef\u5230\u7aef Agent \u6545\u4e8b\u6f14\u793a\u3002"""
    import sys as _sys
    _sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), ".."))
    from scripts.run_story_demo import main as story_main
    return story_main()


def cmd_daemon(args: argparse.Namespace) -> int:
    """启动 Kafka 弹幕守护进程（阻塞，需另开终端）。"""
    from src.gateway.kafka_daemon import DanmakuDaemon
    _info("Starting Kafka DanmakuDaemon...")
    daemon = DanmakuDaemon()
    daemon.run_forever()
    return 0


def cmd_simulator(args: argparse.Namespace) -> int:
    """启动 Kafka 弹幕模拟生产者（需先启动 daemon）。"""
    import sys
    from scripts.run_simulator import DanmakuSimulator
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sim = DanmakuSimulator(interval_seconds=args.interval)
    sim.run(scenario=args.scenario)
    return 0

def cmd_demo(args: argparse.Namespace) -> int:
    """判断 PostgreSQL 是否可用，选择真实链路或降级模式。"""
    try:
        from src.config.settings import get_settings
        import psycopg
        settings = get_settings()
        conn = psycopg.connect(**settings.postgres_connection_kwargs, connect_timeout=3)
        conn.close()
        _info("PostgreSQL available, running real demo pipeline")
        return _run_demo_with_db(args)
    except Exception:
        _info("PostgreSQL not available, running mock demo mode")
        return _run_demo_mock()


def cmd_phase11a_demo(args: argparse.Namespace) -> int:
    """运行独立的 Phase 11A 内存路由演示，不探测或连接 PostgreSQL。"""
    del args
    return _run_python("run_phase11a_skill_runtime_demo.py")



def _run_demo_with_db(args: argparse.Namespace) -> int:
    """有 PostgreSQL 时的真实演示链路。"""
    _info("[1/6] Database Migration")
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
    sub.add_parser("phase11a-demo", help="Phase 11A Skill Runtime 无外部依赖路由演示")
    sub.add_parser("story", help="\u7aef\u5230\u7aef Agent \u6545\u4e8b\u6f14\u793a\uff08\u65e0\u5916\u90e8\u4f9d\u8d56\uff09")
    sub.add_parser("daemon", help="启动 Kafka 弹幕守护进程（阻塞，需另开终端）")
    p_sim = sub.add_parser("simulator", help="启动 Kafka 弹幕模拟生产者（需先启动 daemon）")
    p_sim.add_argument("--interval", type=int, default=3, help="发送间隔（秒）")
    p_sim.add_argument("--scenario", default="normal", choices=["normal", "price_spike", "inventory_alert"], help="播中场景")

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
        "phase11a-demo": cmd_phase11a_demo,
        "story": cmd_story,
        "daemon": cmd_daemon,
        "simulator": cmd_simulator,
        "up": cmd_up,
    }
    return command_map[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
