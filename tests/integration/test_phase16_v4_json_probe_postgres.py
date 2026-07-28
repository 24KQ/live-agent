"""Phase 16 V4 JSON 协议探针的 PostgreSQL append-only 契约测试。

每条测试仅使用 Fake AgentModelPort 和临时 schema，因此可验证真实 SQL 行锁、触发器和
不可修改性，但不会读取 .env 或向 DeepSeek 发送任何请求。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from src.config.settings import get_settings
from src.decision_support.json_probe_v4 import (
    Phase16V4JsonProbeError,
    Phase16V4JsonProbeFailureFact,
    Phase16V4JsonProbeProtocol,
    Phase16V4JsonProbeRunner,
    Phase16V4JsonProbeStatus,
    PostgresPhase16V4JsonProbeLedger,
    initialize_phase16_v4_json_probe_ledger_schema,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelSuccess,
    ModelUsage,
)
from src.decision_support.v4_json_probe_adapter import DeepSeekThinkingMode


@pytest.fixture()
def postgres_v4_probe_ledger():
    """为每个测试新建独立 schema，确保 append-only 行为来自 PostgreSQL 而非内存 Fake。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_v4_json_probe_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={
            **base_kwargs,
            "options": f"-c search_path={schema_name}",
        }
    )
    initialize_phase16_v4_json_probe_ledger_schema(settings)
    try:
        yield PostgresPhase16V4JsonProbeLedger(
            settings,
            hmac_key=bytes.fromhex("7c" * 32),
        )
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
            connection.commit()


def test_v4_ledger_makes_sent_parse_failure_terminal(postgres_v4_probe_ledger) -> None:
    """已发送的 JSON 解析失败必须永久关闭唯一 slot，第二次 dispatch 不能悄悄重试。"""

    ledger = postgres_v4_probe_ledger
    ledger.ensure_run(Phase16V4JsonProbeProtocol.create())
    attempt = ledger.begin_dispatch(
        internal_request_id="df47c075-1e7c-4b46-9e55-646e82604b59"
    )
    ledger.append_failure(
        Phase16V4JsonProbeFailureFact.from_model_failure(
            attempt_id=attempt.attempt_id,
            outcome=ModelFailure(
                request_id=attempt.internal_request_id,
                category=ModelFailureCategory.INVALID_OUTPUT_JSON,
                request_sent=True,
                response_digest="d" * 64,
                http_status=200,
                retry_after_seconds=None,
                latency_ms=Decimal("12.0004"),
            ),
            diagnostics=None,
        )
    )
    ledger.close(
        status=Phase16V4JsonProbeStatus.FAILED,
        reason_code="MODEL_FAILURE_INVALID_OUTPUT_JSON",
    )

    with pytest.raises(Phase16V4JsonProbeError, match="terminal"):
        ledger.begin_dispatch(
            internal_request_id="1ad75f45-3e83-4a08-b6cb-5a5110e3194f"
        )


class _CompleteSuccessPort:
    """提供完整回执的内存端口，用于验证真实 PostgreSQL PASS 触发器。"""

    thinking_mode = DeepSeekThinkingMode.DISABLED

    @staticmethod
    def pop_output_parse_diagnostics(_request_id: str):
        """完整 JSON 成功不产生解析诊断，但仍提供与真实 Adapter 相同的读取边界。"""

        return None

    async def complete(self, request):
        """返回固定无业务 JSON，不建立网络连接。"""

        return ModelSuccess(
            request_id=request.request_id,
            model_id="deepseek-v4-pro",
            output={"status": "ok"},
            usage=ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14),
            provider_response_id="chatcmpl-v4-pg-test",
            finish_reason="stop",
            response_digest="e" * 64,
            latency_ms=Decimal("10.0005"),
        )


def test_v4_runner_passes_only_through_complete_postgres_receipt(postgres_v4_probe_ledger) -> None:
    """PASS 必须经受签名的完整回执和 SQL 终态触发器，不能由 Runner 自己宣告。"""

    report = asyncio.run(
        Phase16V4JsonProbeRunner(
            ledger=postgres_v4_probe_ledger,
            model_port=_CompleteSuccessPort(),
            clock=lambda: datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
        ).execute()
    )

    assert report.status is Phase16V4JsonProbeStatus.PASS
    assert report.reason_code == "JSON_PROTOCOL_PASS"
