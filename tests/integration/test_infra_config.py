"""中间件连接参数组装测试。

本文件属于“集成配置测试”：它验证 LiveAgent 会如何把配置转换成
PostgreSQL、Redis、Kafka 客户端需要的参数，但不会连接真实 Docker 服务。
这样既能覆盖连接契约，又不会让 CI 或公开仓库测试依赖开发者本机环境。
"""

from src.config.settings import Settings


def build_test_settings(**overrides) -> Settings:
    """构造不读取本机 `.env` 的测试配置。

    集成配置测试只验证连接参数组装，不应因为开发者本机 Docker 密码或端口
    不同而改变断言结果。
    """

    return Settings(_env_file=None, **overrides)


def test_postgres_connection_kwargs_are_built_from_settings() -> None:
    """PostgreSQL 连接参数应包含主机、端口、库名、账号和密码。"""

    settings = build_test_settings(
        POSTGRES_HOST="db.local",
        POSTGRES_PORT="15432",
        POSTGRES_DB="live_agent",
        POSTGRES_USER="live_user",
        POSTGRES_PASSWORD="secret",
    )

    assert settings.postgres_connection_kwargs == {
        "host": "db.local",
        "port": 15432,
        "dbname": "live_agent",
        "user": "live_user",
        "password": "secret",
    }


def test_postgres_dsn_masks_password_when_rendered_for_logs() -> None:
    """日志展示用 DSN 必须隐藏密码，避免真实凭据被复制到控制台或 Issue。"""

    settings = build_test_settings(
        POSTGRES_HOST="db.local",
        POSTGRES_PORT="15432",
        POSTGRES_DB="live_agent",
        POSTGRES_USER="live_user",
        POSTGRES_PASSWORD="secret",
    )

    assert settings.postgres_safe_dsn == "postgresql://live_user:***@db.local:15432/live_agent"
    assert "secret" not in settings.postgres_safe_dsn


def test_redis_connection_target_is_built_from_settings() -> None:
    """Redis 连接目标应保持简单，供健康检查脚本直接解包使用。"""

    settings = build_test_settings(REDIS_HOST="cache.local", REDIS_PORT="16379")

    assert settings.redis_connection_target == ("cache.local", 16379)


def test_kafka_bootstrap_servers_are_split_and_trimmed() -> None:
    """Kafka broker 列表应支持逗号分隔，并自动去掉多余空格。"""

    settings = build_test_settings(
        KAFKA_BOOTSTRAP_SERVERS="kafka-1.local:9092, kafka-2.local:9092",
    )

    assert settings.kafka_bootstrap_server_list == [
        "kafka-1.local:9092",
        "kafka-2.local:9092",
    ]
