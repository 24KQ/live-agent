"""LangGraph PostgreSQL checkpoint 辅助函数。

本模块只负责接入官方 `langgraph-checkpoint-postgres` 的 PostgresSaver，不自建
checkpoint 表，也不在业务代码中直接拼 SQL。这样后续接入 interrupt、
human-in-the-loop 和抢占恢复时，可以沿用 LangGraph 官方持久化语义。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
import os

from langgraph.checkpoint.postgres import PostgresSaver

from src.config.settings import Settings


def apply_langgraph_checkpoint_env(settings: Settings) -> None:
    """应用 LangGraph checkpoint 序列化安全配置。

    官方扩展会读取 `LANGGRAPH_STRICT_MSGPACK` 环境变量。这里统一根据 Settings
    写入进程环境，避免 CLI、测试和后续服务入口配置不一致。
    """

    os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true" if settings.langgraph_strict_msgpack else "false"


def create_postgres_checkpointer(settings: Settings) -> AbstractContextManager[PostgresSaver]:
    """创建官方 PostgresSaver 上下文管理器。

    返回上下文管理器而不是裸对象，是为了让调用方用 `with` 明确管理数据库连接
    生命周期。`postgres_checkpoint_conninfo` 内含真实密码，只能传给 PostgresSaver，
    不得打印。
    """

    apply_langgraph_checkpoint_env(settings)
    return PostgresSaver.from_conn_string(settings.postgres_checkpoint_conninfo)


def initialize_postgres_checkpointer(settings: Settings) -> None:
    """初始化官方 checkpoint 表结构。

    PostgresSaver 首次使用前必须执行 `setup()`。该操作幂等，集成测试和 CLI
    演示都显式调用，避免依赖开发者本机数据库中已经存在相关表。
    """

    with create_postgres_checkpointer(settings) as checkpointer:
        checkpointer.setup()
