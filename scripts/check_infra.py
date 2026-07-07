"""LiveAgent 本地中间件健康检查脚本。

运行方式：
    python scripts/check_infra.py

脚本会依次检查 PostgreSQL、pgvector、Redis 和 Kafka。所有必需中间件
都通过时返回退出码 0；任意必需项失败时返回退出码 1，方便后续接入
CI 或本地一键检查脚本。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# 直接执行 `python scripts/check_infra.py` 时，Python 默认只把 scripts 目录
# 放入 sys.path。这里手动加入仓库根目录，确保可以稳定导入 `src.config`。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import Settings, get_settings


@dataclass(frozen=True)
class CheckResult:
    """单个中间件检查结果。

    name 用于终端展示；ok 表示是否通过；message 解释成功细节或失败原因。
    保持结果对象简单，是为了后续可以很容易扩展成 JSON 输出或 CI 注释。
    """

    name: str
    ok: bool
    message: str


def _success(name: str, message: str) -> CheckResult:
    """生成成功结果，统一输出格式。"""

    return CheckResult(name=name, ok=True, message=message)


def _failure(name: str, error: Exception | str) -> CheckResult:
    """生成失败结果，统一隐藏异常类型之外的复杂堆栈。

    健康检查面向开发者排障，默认只展示关键错误信息；如果后续需要详细
    堆栈，可以再增加 `--verbose` 参数，而不是在默认输出里暴露敏感信息。
    """

    message = str(error)
    return CheckResult(name=name, ok=False, message=message)


def check_postgres(settings: Settings) -> CheckResult:
    """检查 PostgreSQL 是否可连接。

    这里使用 psycopg 的关键字参数连接，避免手写 DSN 泄露密码或遇到转义问题。
    connect_timeout 控制失败等待时间，防止 Docker 未启动时脚本长时间卡住。
    """

    try:
        import psycopg

        connect_kwargs = {
            **settings.postgres_connection_kwargs,
            "connect_timeout": 3,
        }
        with psycopg.connect(**connect_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1;")
                cursor.fetchone()
        return _success("PostgreSQL", f"connected to {settings.postgres_safe_dsn}")
    except Exception as exc:  # noqa: BLE001 - 健康检查需要捕获所有连接失败并继续汇总。
        return _failure("PostgreSQL", exc)


def check_pgvector(settings: Settings) -> CheckResult:
    """检查 pgvector 扩展是否已经安装到当前数据库。

    Phase 0 的初始化脚本会执行 `CREATE EXTENSION IF NOT EXISTS vector`。
    这里验证 `pg_extension`，确认扩展不仅在镜像中可用，而且已经安装进当前库。
    """

    try:
        import psycopg

        connect_kwargs = {
            **settings.postgres_connection_kwargs,
            "connect_timeout": 3,
        }
        with psycopg.connect(**connect_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector');")
                installed = cursor.fetchone()[0]

        if not installed:
            return _failure("pgvector", "extension 'vector' is not installed in current database")
        return _success("pgvector", "extension 'vector' is installed")
    except Exception as exc:  # noqa: BLE001 - 扩展检查失败也要汇总到最终报告。
        return _failure("pgvector", exc)


def check_redis(settings: Settings) -> CheckResult:
    """检查 Redis 是否可 ping。

    Redis 当前只验证基础连通性，不读取或写入任何业务 key，避免健康检查污染
    开发者本地缓存数据。
    """

    try:
        import redis

        host, port = settings.redis_connection_target
        client = redis.Redis(
            host=host,
            port=port,
            socket_connect_timeout=3,
            socket_timeout=3,
            decode_responses=True,
        )
        client.ping()
        return _success("Redis", f"ping ok at {host}:{port}")
    except Exception as exc:  # noqa: BLE001 - Redis 未启动、端口错误等都汇总为失败。
        return _failure("Redis", exc)


def check_kafka(settings: Settings) -> CheckResult:
    """检查 Kafka broker 元数据是否可读取。

    Kafka 不要求 Phase 0 立刻创建 topic，本检查只确认 bootstrap servers 可达，
    并能返回 broker 元数据。topic 是否存在由配置测试保证“已声明”。
    """

    try:
        from kafka.admin import KafkaAdminClient

        bootstrap_servers = settings.kafka_bootstrap_server_list
        if not bootstrap_servers:
            return _failure("Kafka", "KAFKA_BOOTSTRAP_SERVERS is empty")

        admin_client = KafkaAdminClient(
            bootstrap_servers=bootstrap_servers,
            request_timeout_ms=3000,
            # kafka-python 3.x 的 AdminClient 不支持 api_version_auto_timeout_ms；
            # bootstrap_timeout_ms 用于限制首次探测 broker 元数据的等待时间。
            bootstrap_timeout_ms=3000,
            client_id="live-agent-infra-check",
        )
        try:
            topics = admin_client.list_topics()
        finally:
            admin_client.close()

        return _success("Kafka", f"metadata ok, {len(topics)} topics visible")
    except Exception as exc:  # noqa: BLE001 - Kafka 启动慢或端口错误时需要清晰报告失败。
        return _failure("Kafka", exc)


def run_checks(settings: Settings) -> list[CheckResult]:
    """按固定顺序执行中间件检查。

    即使前一个服务失败，也继续检查后续服务，方便开发者一次看到所有未启动
    的中间件，而不是修一个服务就重新跑一次脚本。
    """

    checks: list[Callable[[Settings], CheckResult]] = [
        check_postgres,
        check_pgvector,
        check_redis,
        check_kafka,
    ]
    return [check(settings) for check in checks]


def print_report(results: list[CheckResult]) -> None:
    """打印人类可读的健康检查报告。"""

    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] {result.name}: {result.message}")


def main() -> int:
    """脚本入口。

    返回码约定：
    - 0：PostgreSQL、pgvector、Redis、Kafka 全部检查通过。
    - 1：任意必需中间件失败，调用方应阻止继续启动依赖中间件的功能。
    """

    settings = get_settings()
    results = run_checks(settings)
    print_report(results)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
