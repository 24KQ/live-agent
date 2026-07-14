"""LiveAgent 本地配置定义。

本模块是 Phase 0 的配置中心，负责把 `.env` 或系统环境变量转换为
类型明确的 Python 对象。生产代码不要直接读取环境变量，而应通过
`Settings` 或 `get_settings()` 访问配置，这样测试、脚本和后续服务
可以共享同一套配置契约。
"""

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from psycopg.conninfo import make_conninfo


class Settings(BaseSettings):
    """LiveAgent 运行配置。

    字段默认值与公开的 `.env.example` 保持一致，因此公开仓库在没有
    真实 `.env` 的情况下也可以运行单元测试。真正的账号、密码和端口
    应写入开发者本机 `.env`，该文件已被 `.gitignore` 忽略，避免凭据
    被推送到 GitHub。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用名称只用于日志、命令行输出和后续服务标识，不参与业务判断。
    app_name: str = Field(default="LiveAgent", validation_alias="APP_NAME")

    # PostgreSQL 是 Phase 0 的主数据库，后续会承载状态、审计、记忆和 checkpoint。
    postgres_host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, validation_alias="POSTGRES_PORT")
    postgres_db: str = Field(default="postgres", validation_alias="POSTGRES_DB")
    postgres_user: str = Field(default="postgres", validation_alias="POSTGRES_USER")
    postgres_password: str = Field(default="change_me", validation_alias="POSTGRES_PASSWORD")

    # Redis 用于短期缓存、幂等键和分布式锁；Phase 0 只验证连接可达性。
    redis_host: str = Field(default="localhost", validation_alias="REDIS_HOST")
    redis_port: int = Field(default=6379, validation_alias="REDIS_PORT")

    # Kafka 承载直播过程中的事件流，多个 broker 使用逗号分隔。
    kafka_bootstrap_servers: str = Field(
        default="localhost:9092",
        validation_alias="KAFKA_BOOTSTRAP_SERVERS",
    )
    kafka_topic_danmaku: str = Field(default="anchor.danmaku", validation_alias="KAFKA_TOPIC_DANMAKU")
    kafka_topic_inventory: str = Field(default="anchor.inventory", validation_alias="KAFKA_TOPIC_INVENTORY")
    kafka_topic_traffic: str = Field(default="anchor.traffic", validation_alias="KAFKA_TOPIC_TRAFFIC")
    kafka_topic_command: str = Field(default="anchor.command", validation_alias="KAFKA_TOPIC_COMMAND")

    # ── Phase 12B 库存事件可信入站 ──────────────────────────────────────
    # Profile 在 DurableInventoryKafkaConsumer 构造时复制为冻结值对象。topic 仍复用
    # kafka_topic_inventory，避免同一进程出现“订阅 topic”和“受信 topic”两份配置。
    inventory_ingress_profile_id: str = Field(
        default="inventory-kafka-v1",
        min_length=1,
        validation_alias="INVENTORY_INGRESS_PROFILE_ID",
    )
    inventory_ingress_trusted_sources: str = Field(
        default="inventory-service",
        validation_alias="INVENTORY_INGRESS_TRUSTED_SOURCES",
    )
    inventory_ingress_enabled: bool = Field(
        default=False,
        validation_alias="INVENTORY_INGRESS_ENABLED",
    )
    kafka_inventory_event_group_id: str = Field(
        default="live-agent-inventory-plan-engine-v1",
        min_length=1,
        validation_alias="KAFKA_INVENTORY_EVENT_GROUP_ID",
    )
    kafka_inventory_auto_offset_reset: Literal["earliest", "latest"] = Field(
        default="latest",
        validation_alias="KAFKA_INVENTORY_AUTO_OFFSET_RESET",
    )

    # MinIO 是可选对象存储，用于后续保存长报告、大文件或上下文卸载材料。
    minio_endpoint: str = Field(default="http://localhost:8900", validation_alias="MINIO_ENDPOINT")
    minio_access_key: str = Field(default="change_me", validation_alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(default="change_me", validation_alias="MINIO_SECRET_KEY")
    minio_bucket: str = Field(default="live-agent", validation_alias="MINIO_BUCKET")

    # LangGraph checkpoint 使用官方 PostgresSaver。严格 msgpack 默认开启，避免
    # checkpoint 反序列化时接受过宽的类型范围；如本机调试确需关闭，应只改 `.env`。
    langgraph_strict_msgpack: bool = Field(default=True, validation_alias="LANGGRAPH_STRICT_MSGPACK")

    # pgAdmin / MySQL 当前只作为本地实验辅助配置，Phase 0 不主动连接。
    pgadmin_email: str = Field(default="change_me@example.com", validation_alias="PGADMIN_EMAIL")
    pgadmin_password: str = Field(default="change_me", validation_alias="PGADMIN_PASSWORD")
    mysql_user: str = Field(default="root", validation_alias="MYSQL_USER")
    mysql_password: str = Field(default="change_me", validation_alias="MYSQL_PASSWORD")

    # Embedding API 配置（Phase 3C 语义记忆检索）。
    # 默认使用智谱（bigmodel）embedding-3 模型，1024 维；
    # 如切换到 OpenAI 或本地模型，只需改这几个值。
    embedding_api_base_url: str = Field(
        default="https://open.bigmodel.cn/api/paas/v4",
        validation_alias="EMBEDDING_API_BASE_URL",
    )
    embedding_api_key: str = Field(
        default="change_me",
        validation_alias="EMBEDDING_API_KEY",
    )
    embedding_model: str = Field(
        default="embedding-3",
        validation_alias="EMBEDDING_MODEL",
    )
    embedding_embeddings_path: str = Field(
        default="/embeddings",
        validation_alias="EMBEDDING_EMBEDDINGS_PATH",
    )
    embedding_dimensions: int = Field(
        default=1024,
        validation_alias="EMBEDDING_DIMENSIONS",
    )

    # LLM API ???Phase 3E DeepSeek ???????
    llm_api_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias="LLM_API_BASE_URL",
    )
    llm_api_key: str = Field(
        default="change_me",
        validation_alias="LLM_API_KEY",
    )
    llm_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="LLM_MODEL",
    )
    llm_max_tokens: int = Field(
        default=500,
        validation_alias="LLM_MAX_TOKENS",
    )
    llm_temperature: float = Field(
        default=0.3,
        validation_alias="LLM_TEMPERATURE",
    )
    llm_timeout_seconds: int = Field(
        default=15,
        validation_alias="LLM_TIMEOUT_SECONDS",
    )

    # Phase 7B 生产硬化：操作员鉴权与 schema 初始化控制
    operator_auth_enabled: bool = Field(
        default=False,
        validation_alias="OPERATOR_AUTH_ENABLED",
    )
    operator_tokens: str = Field(
        default="",
        validation_alias="OPERATOR_TOKENS",
    )
    auto_initialize_schema: bool = Field(
        default=True,
        validation_alias="AUTO_INITIALIZE_SCHEMA",
    )
    # ── Phase 11A Skill Runtime 路由配置 ──────────────────────────────────
    # 播前 generation 批次（query_products, generate_live_plan, generate_product_card）
    # 的路由选择。LEGACY 走原 PreLiveBusinessFlowService，SKILL_RUNTIME 走统一 Executor。
    skill_route_prelive_generation: Literal["LEGACY", "SKILL_RUNTIME"] = Field(
        default="LEGACY", validation_alias="SKILL_ROUTE_PRELIVE_GENERATION"
    )
    # 播前 setup 批次（setup_live_session）的路由选择，独立于 generation 批次。
    skill_route_prelive_setup: Literal["LEGACY", "SKILL_RUNTIME"] = Field(
        default="LEGACY", validation_alias="SKILL_ROUTE_PRELIVE_SETUP"
    )
    # ── Phase 11B 三批启动冻结路由配置 ──────────────────────────────────
    # 三批字段是后续统一执行契约的正式入口；旧 Phase 11A generation/setup 字段
    # 只作为兼容别名保留到 Phase 12 验收。RoutePolicy.from_settings 会在“新批次
    # 字段未显式配置”时读取旧别名，从而支持旧 .env 平滑过渡。
    skill_route_phase11b_batch1: Literal["LEGACY", "SKILL_RUNTIME"] = Field(
        default="LEGACY", validation_alias="SKILL_ROUTE_PHASE11B_BATCH1"
    )
    skill_route_phase11b_batch2: Literal["LEGACY", "SKILL_RUNTIME"] = Field(
        default="LEGACY", validation_alias="SKILL_ROUTE_PHASE11B_BATCH2"
    )
    skill_route_phase11b_batch3: Literal["LEGACY", "SKILL_RUNTIME"] = Field(
        default="LEGACY", validation_alias="SKILL_ROUTE_PHASE11B_BATCH3"
    )

    # ── Phase 12A DAG PlanEngine 路由配置 ───────────────────────────────
    # 该字段只控制播前 Graph 的手卡批次，不复用 Skill Runtime 的批次路由。默认
    # LEGACY 可保证升级后不会自动改变生产执行路径；应用装配层会把值复制到冻结策略。
    plan_engine_card_execution_route: Literal["LEGACY", "PLAN_ENGINE"] = Field(
        default="LEGACY",
        validation_alias="PLAN_ENGINE_CARD_EXECUTION_ROUTE",
    )

    @property
    def postgres_connection_kwargs(self) -> dict[str, Any]:
        """生成 psycopg.connect 可直接使用的 PostgreSQL 连接参数。

        使用字典参数而不是手写 DSN，可以避免密码中包含特殊字符时产生
        转义问题，也方便健康检查脚本对超时时间等参数做局部追加。
        """

        return {
            "host": self.postgres_host,
            "port": self.postgres_port,
            "dbname": self.postgres_db,
            "user": self.postgres_user,
            "password": self.postgres_password,
        }

    @property
    def postgres_checkpoint_conninfo(self) -> str:
        """生成 PostgresSaver 可直接使用的 PostgreSQL conninfo。

        PostgresSaver 的 `from_conn_string()` 接受 libpq conninfo 字符串。这里使用
        psycopg 官方的 `make_conninfo()` 负责转义密码、空格和特殊字符，避免手写
        字符串导致 checkpoint 连接失败或凭据解析错误。该返回值包含真实密码，
        只能传给数据库客户端，禁止打印到日志、README 或阶段记录。
        """

        return make_conninfo(
            "",
            host=self.postgres_host,
            port=self.postgres_port,
            dbname=self.postgres_db,
            user=self.postgres_user,
            password=self.postgres_password,
        )

    @property
    def postgres_safe_dsn(self) -> str:
        """生成可打印到日志的 PostgreSQL DSN。

        返回值会把密码替换为 `***`。健康检查失败时可以展示这个字符串，
        让开发者知道正在连接哪里，同时不会把真实密码泄露到终端截图、
        GitHub Issue 或聊天记录中。
        """

        return (
            f"postgresql://{self.postgres_user}:***@"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_connection_target(self) -> tuple[str, int]:
        """生成 Redis 连接目标。

        返回 `(host, port)` 的简单元组，方便脚本直接解包，同时保持测试
        对连接契约的断言足够清晰。
        """

        return (self.redis_host, self.redis_port)

    @property
    def kafka_bootstrap_server_list(self) -> list[str]:
        """把 Kafka broker 字符串拆成客户端需要的列表。

        开发者可能在 `.env` 中写入 `host1:9092, host2:9092`。这里会去掉
        多余空白并过滤空项，避免 Kafka 客户端收到无效 broker 地址。
        """

        return [
            server.strip()
            for server in self.kafka_bootstrap_servers.split(",")
            if server.strip()
        ]

    @property
    def kafka_topics(self) -> dict[str, str]:
        """返回 Phase 0 必须声明的 Kafka topic 映射。

        使用语义化 key 是为了让后续业务代码引用 `danmaku`、`inventory`
        这类领域名称，而不是到处复制真实 topic 字符串。
        """

        return {
            "danmaku": self.kafka_topic_danmaku,
            "inventory": self.kafka_topic_inventory,
            "traffic": self.kafka_topic_traffic,
            "command": self.kafka_topic_command,
        }

    @property
    def inventory_ingress_trusted_source_set(self) -> frozenset[str]:
        """解析启动冻结的可信业务 source 集，并去除重复值与空白。"""
        return frozenset(
            source.strip()
            for source in self.inventory_ingress_trusted_sources.split(",")
            if source.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置实例。

    `lru_cache` 可以避免脚本或服务在一次进程生命周期内重复解析 `.env`。
    测试中如需隔离配置，应直接实例化 `Settings(...)`，不要依赖缓存对象。
    """

    return Settings()
