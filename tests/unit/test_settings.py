"""LiveAgent 配置加载的单元测试。

这些测试先定义 Phase 0 对配置层的最小行为要求：
1. 默认值必须能在没有真实 .env 的情况下加载，保证公开仓库可直接运行测试。
2. 端口字段必须被解析为整数，避免连接中间件时把字符串误传给客户端。
3. Kafka 核心 topic 必须存在，保证后续弹幕、库存、流量和指令事件都有明确入口。
"""

from src.config.settings import Settings


def build_test_settings(**overrides) -> Settings:
    """构造不读取本机 `.env` 的测试配置。

    开发者通常会按 README 复制 `.env` 并填入真实端口和密码；单元测试需要
    验证公开默认契约，因此显式关闭 env_file，避免被本机私有配置影响。
    """

    return Settings(_env_file=None, **overrides)


def test_default_settings_load_without_real_env_file() -> None:
    """没有本地 .env 时，也应使用公开模板一致的安全默认值。"""

    settings = build_test_settings()

    assert settings.app_name == "LiveAgent"
    assert settings.postgres_host == "localhost"
    assert settings.postgres_db == "postgres"
    assert settings.minio_bucket == "live-agent"


def test_middleware_ports_are_parsed_as_int() -> None:
    """中间件端口必须是整数，方便 psycopg、redis 等客户端直接使用。"""

    settings = build_test_settings(
        POSTGRES_PORT="15432",
        REDIS_PORT="16379",
    )

    assert settings.postgres_port == 15432
    assert settings.redis_port == 16379


def test_required_kafka_topics_exist() -> None:
    """Phase 0 必须声明直播事件流所需的四个 Kafka topic。"""

    settings = build_test_settings()

    assert settings.kafka_topic_danmaku == "anchor.danmaku"
    assert settings.kafka_topic_inventory == "anchor.inventory"
    assert settings.kafka_topic_traffic == "anchor.traffic"
    assert settings.kafka_topic_command == "anchor.command"
    assert settings.kafka_topics == {
        "danmaku": "anchor.danmaku",
        "inventory": "anchor.inventory",
        "traffic": "anchor.traffic",
        "command": "anchor.command",
    }
