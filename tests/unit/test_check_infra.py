"""中间件检查脚本的单元测试。

这里不连接真实 Kafka，而是替换 KafkaAdminClient，专门验证脚本传给客户端
的参数契约。这样可以避免把第三方库不支持的配置项带到运行时才发现。
"""

from scripts.check_infra import check_kafka
from src.config.settings import Settings


def build_test_settings(**overrides) -> Settings:
    """构造不读取 `.env` 的测试配置，避免真实中间件账号影响脚本单测。"""

    return Settings(_env_file=None, **overrides)


def test_kafka_check_uses_supported_admin_client_options(monkeypatch) -> None:
    """Kafka 健康检查不应传入当前 kafka-python 不识别的配置项。"""

    captured_kwargs = {}

    class FakeKafkaAdminClient:
        """记录构造参数的轻量替身，避免测试依赖真实 Kafka broker。"""

        def __init__(self, **kwargs) -> None:
            captured_kwargs.update(kwargs)

        def list_topics(self) -> list[str]:
            return ["anchor.danmaku"]

        def close(self) -> None:
            return None

    monkeypatch.setattr("kafka.admin.KafkaAdminClient", FakeKafkaAdminClient)

    result = check_kafka(build_test_settings(KAFKA_BOOTSTRAP_SERVERS="localhost:9092"))

    assert result.ok is True
    assert captured_kwargs["bootstrap_servers"] == ["localhost:9092"]
    assert "request_timeout_ms" in captured_kwargs
    assert "api_version_auto_timeout_ms" not in captured_kwargs
