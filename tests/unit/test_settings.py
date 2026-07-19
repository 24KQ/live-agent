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


def test_default_settings_load_without_real_env_file(monkeypatch) -> None:
    """没有本地 .env 时，也应使用公开模板一致的安全默认值。"""

    # PR Gate 会为迁移注入 live_agent；本用例验证的是“无环境覆盖”的公开默认值。
    monkeypatch.delenv("POSTGRES_DB", raising=False)
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


def test_postgres_checkpoint_conninfo_is_built_without_logging_password() -> None:
    """PostgresSaver 需要完整连接串，但日志和报错只能展示脱敏 DSN。"""

    settings = build_test_settings(
        POSTGRES_HOST="db.local",
        POSTGRES_PORT="15432",
        POSTGRES_DB="live_agent",
        POSTGRES_USER="live_user",
        POSTGRES_PASSWORD="secret with space",
    )

    assert settings.postgres_checkpoint_conninfo == (
        "host=db.local port=15432 dbname=live_agent "
        "user=live_user password='secret with space'"
    )
    assert settings.postgres_safe_dsn == "postgresql://live_user:***@db.local:15432/live_agent"
    assert "secret" not in settings.postgres_safe_dsn


def test_langgraph_strict_msgpack_defaults_to_enabled() -> None:
    """checkpoint 默认启用严格 msgpack，降低反序列化风险。"""

    settings = build_test_settings()

    assert settings.langgraph_strict_msgpack is True

def test_skill_route_defaults_to_legacy() -> None:
    """Skill Runtime 路由默认值必须为 LEGACY，保证 Phase 11A 切换前行为不变。"""

    settings = build_test_settings()

    assert settings.skill_route_prelive_generation == "LEGACY"
    assert settings.skill_route_prelive_setup == "LEGACY"
    assert settings.skill_route_phase11b_batch1 == "LEGACY"
    assert settings.skill_route_phase11b_batch2 == "LEGACY"
    assert settings.skill_route_phase11b_batch3 == "LEGACY"


def test_skill_route_can_be_set_via_environment_variable() -> None:
    """路由字段可以通过环境变量独立配置 generation 和 setup。"""

    settings = build_test_settings(
        SKILL_ROUTE_PRELIVE_GENERATION="SKILL_RUNTIME",
        SKILL_ROUTE_PRELIVE_SETUP="LEGACY",
        SKILL_ROUTE_PHASE11B_BATCH1="LEGACY",
        SKILL_ROUTE_PHASE11B_BATCH2="SKILL_RUNTIME",
        SKILL_ROUTE_PHASE11B_BATCH3="SKILL_RUNTIME",
    )

    assert settings.skill_route_prelive_generation == "SKILL_RUNTIME"
    assert settings.skill_route_prelive_setup == "LEGACY"
    assert settings.skill_route_phase11b_batch1 == "LEGACY"
    assert settings.skill_route_phase11b_batch2 == "SKILL_RUNTIME"
    assert settings.skill_route_phase11b_batch3 == "SKILL_RUNTIME"
