"""Phase 16 V4 禁思考 JSON 协议探针的纯离线契约测试。

所有端口实现均为确定性 Fake；本文件不读取 .env、不连接 PostgreSQL，绝不消耗真实模型
费用。PostgreSQL 的 append-only 约束由同名 integration 测试覆盖。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any

from src.decision_support.json_probe_v4 import (
    PHASE16_V4_JSON_PROBE_CASE_ID,
    Phase16V4JsonProbeAttempt,
    Phase16V4JsonProbeFailureFact,
    Phase16V4JsonProbeProtocol,
    Phase16V4JsonProbeRunner,
    Phase16V4JsonProbeStatus,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelMessage,
    ModelRequest,
    ModelSuccess,
    ModelUsage,
)
from src.decision_support.v4_json_probe_adapter import (
    AsyncHttpResponse,
    DeepSeekFinishReasonClass,
    DeepSeekOutputContentShape,
    DeepSeekOutputParseDiagnostics,
    DeepSeekOutputParseStage,
    DeepSeekThinkingMode,
    DeepSeekV4JsonProbeAdapter,
)


class _ProbeTransport:
    """记录 V4 Adapter 送往供应商的 payload，整个测试不产生网络请求。"""

    def __init__(self, response: AsyncHttpResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> AsyncHttpResponse:
        """保存请求元数据并返回固定响应，以验证装饰器不引入重试。"""

        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        return self._response


class _RecordingLedger:
    """最小账本 Fake：记录 Runner 的事实顺序，不模拟 PostgreSQL 具体实现。"""

    def __init__(self, *, receipt_complete: bool = True) -> None:
        self.receipt_complete = receipt_complete
        self.protocol = None
        self.failure = None
        self.receipt = None
        self.closed = None

    def ensure_run(self, protocol) -> None:
        """记录冻结协议，便于断言模型/预算入口未被调用方替换。"""

        self.protocol = protocol

    def begin_dispatch(self, *, internal_request_id: str) -> Phase16V4JsonProbeAttempt:
        """返回固定 UUID，表达真实账本先于网络持久化唯一 intent 的顺序。"""

        return Phase16V4JsonProbeAttempt(
            attempt_id="15f8312d-09e9-4b0c-b2e4-c6a5f09703a4",
            internal_request_id=internal_request_id,
        )

    def append_failure(self, fact) -> None:
        """保存失败投影，测试不接触模型正文。"""

        self.failure = fact

    def append_receipt(self, *, attempt_id: str, success: ModelSuccess) -> bool:
        """保存成功回执参数并返回预设完整性，模拟审计账本的严格门禁。"""

        self.receipt = (attempt_id, success)
        return self.receipt_complete

    def close(self, *, status: Phase16V4JsonProbeStatus, reason_code: str) -> None:
        """记录唯一终态，验证 Runner 不在结论后尝试第二次调用。"""

        self.closed = (status, reason_code)


class _SuccessPort:
    """返回完整、无业务含义 JSON 成功的确定性模型端口。"""

    def __init__(self, *, output: object = {"status": "ok"}) -> None:
        self.output = output
        self.requests = []
        self.thinking_mode = DeepSeekThinkingMode.DISABLED

    def pop_output_parse_diagnostics(self, _request_id: str):
        """成功端口不存在 JSON 解析失败诊断，保持与真实 Adapter 的一次性读取接口一致。"""

        return None

    async def complete(self, request):
        """收集请求后构造完整 Provider 回执，不进行网络请求。"""

        self.requests.append(request)
        return ModelSuccess(
            request_id=request.request_id,
            model_id="deepseek-v4-pro",
            output=self.output,
            usage=ModelUsage(input_tokens=12, output_tokens=4, total_tokens=16),
            provider_response_id="chatcmpl-v4-json-probe",
            finish_reason="stop",
            response_digest="a" * 64,
            latency_ms=Decimal("12.3456"),
        )


class _ParseFailurePort:
    """模拟 Adapter 已发送但不能解析正文的结果，输入中不包含真实模型文本。"""

    def __init__(self) -> None:
        self.thinking_mode = DeepSeekThinkingMode.DISABLED
        self._diagnostic = DeepSeekOutputParseDiagnostics(
            stage=DeepSeekOutputParseStage.OUTPUT_JSON_SYNTAX_INVALID,
            content_shape=DeepSeekOutputContentShape.MARKDOWN_CODE_FENCE,
            finish_reason=DeepSeekFinishReasonClass.STOP,
            reasoning_content_present=False,
        )

    def pop_output_parse_diagnostics(self, _request_id: str):
        """模拟真实 Adapter 以取走语义返回诊断，防止同一失败被多次消费。"""

        diagnostic, self._diagnostic = self._diagnostic, None
        return diagnostic

    async def complete(self, request):
        """返回带脱敏分类的端口失败，检验它会被完整写入 V4 账本投影。"""

        return ModelFailure(
            request_id=request.request_id,
            category=ModelFailureCategory.INVALID_OUTPUT_JSON,
            request_sent=True,
            response_digest="b" * 64,
            http_status=200,
            retry_after_seconds=None,
            latency_ms=Decimal("30.0004"),
        )


def test_v4_protocol_is_minimal_and_explicitly_disables_thinking() -> None:
    """协议探针必须冻结 V4 Pro、禁思考、30 秒 deadline 和 64 token 上限。"""

    protocol = Phase16V4JsonProbeProtocol.create()

    assert protocol.model_id == "deepseek-v4-pro"
    assert protocol.thinking_mode is DeepSeekThinkingMode.DISABLED
    assert protocol.max_output_tokens == 64
    assert protocol.deadline_seconds == 30
    assert len(protocol.protocol_digest) == 64


def test_v4_adapter_injects_disabled_thinking_and_forgets_model_text() -> None:
    """V4 专属 Adapter 必须发送禁思考字段，并只留下枚举级 JSON 失败诊断。"""

    response = AsyncHttpResponse(
        status_code=200,
        headers={},
        body=json.dumps(
            {
                "id": "chatcmpl-v4-probe",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "message": {
                            "content": "```json\\n{\"status\":\"ok\"}\\n```",
                            "reasoning_content": "private reasoning must not persist",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            }
        ).encode("utf-8"),
    )
    transport = _ProbeTransport(response)
    adapter = DeepSeekV4JsonProbeAdapter(api_key="test-secret", transport=transport)
    request = ModelRequest(
        request_id="v4-adapter-request-001",
        endpoint_host="api.deepseek.com",
        model_id="deepseek-v4-pro",
        temperature=Decimal("0"),
        prompt_hash="a" * 64,
        result_schema_hash="b" * 64,
        messages=(
            ModelMessage(role="system", content="Return JSON."),
            ModelMessage(role="user", content="Return status."),
        ),
        max_output_tokens=64,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=5),
    )

    outcome = asyncio.run(adapter.complete(request))

    assert isinstance(outcome, ModelFailure)
    assert outcome.category is ModelFailureCategory.INVALID_OUTPUT_JSON
    assert len(transport.calls) == 1
    assert transport.calls[0]["payload"]["thinking"] == {"type": "disabled"}
    diagnostics = adapter.pop_output_parse_diagnostics(request.request_id)
    assert diagnostics is not None
    assert diagnostics.stage is DeepSeekOutputParseStage.OUTPUT_JSON_SYNTAX_INVALID
    assert diagnostics.content_shape is DeepSeekOutputContentShape.MARKDOWN_CODE_FENCE
    assert diagnostics.finish_reason is DeepSeekFinishReasonClass.STOP
    assert diagnostics.reasoning_content_present is True
    assert "private reasoning" not in outcome.model_dump_json()
    assert "```json" not in outcome.model_dump_json()


def test_v4_runner_passes_only_for_complete_receipt_and_exact_minimal_json() -> None:
    """禁思考探针通过不代表经营决策正确，只证明最小 JSON 协议完整可消费。"""

    ledger = _RecordingLedger()
    port = _SuccessPort()
    report = asyncio.run(
        Phase16V4JsonProbeRunner(
            ledger=ledger,
            model_port=port,
            clock=lambda: datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
        ).execute()
    )

    assert report.status is Phase16V4JsonProbeStatus.PASS
    assert report.reason_code == "JSON_PROTOCOL_PASS"
    assert ledger.closed == (Phase16V4JsonProbeStatus.PASS, "JSON_PROTOCOL_PASS")
    assert len(port.requests) == 1
    assert port.thinking_mode is DeepSeekThinkingMode.DISABLED
    assert port.requests[0].max_output_tokens == 64
    assert PHASE16_V4_JSON_PROBE_CASE_ID not in port.requests[0].messages[1].content


def test_v4_runner_preserves_parse_diagnostic_without_model_body() -> None:
    """已发送 JSON 解析失败应关闭一次性 run，并只输出可审计的枚举级诊断。"""

    ledger = _RecordingLedger()
    report = asyncio.run(
        Phase16V4JsonProbeRunner(
            ledger=ledger,
            model_port=_ParseFailurePort(),
            clock=lambda: datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
        ).execute()
    )

    assert report.status is Phase16V4JsonProbeStatus.FAILED
    assert report.reason_code == "MODEL_FAILURE_INVALID_OUTPUT_JSON"
    assert report.parse_stage is DeepSeekOutputParseStage.OUTPUT_JSON_SYNTAX_INVALID
    assert report.content_shape is DeepSeekOutputContentShape.MARKDOWN_CODE_FENCE
    assert report.finish_reason is DeepSeekFinishReasonClass.STOP
    assert report.reasoning_content_present is False
    assert ledger.failure is not None
    assert ledger.closed == (
        Phase16V4JsonProbeStatus.FAILED,
        "MODEL_FAILURE_INVALID_OUTPUT_JSON",
    )
    # 失败事实类型没有正文、Prompt 或异常自由文本字段，防止诊断实现扩大数据留存面。
    assert set(ledger.failure.model_dump()) == {
        "attempt_id",
        "category",
        "request_sent",
        "response_digest",
        "http_status",
        "retry_after_seconds",
        "latency_ms",
        "parse_stage",
        "content_shape",
        "finish_reason",
        "reasoning_content_present",
        "fact_digest",
    }


def test_v4_failure_fact_rounds_latency_before_its_digest() -> None:
    """数据库只保留毫秒三位小数，摘要必须使用同一精度以保证历史事实可复验。"""

    fact = Phase16V4JsonProbeFailureFact.from_model_failure(
        attempt_id="15f8312d-09e9-4b0c-b2e4-c6a5f09703a4",
        outcome=ModelFailure(
            request_id="c7f1afe8-639a-4328-959c-580d1fc7d13c",
            category=ModelFailureCategory.DEADLINE_EXCEEDED,
            request_sent=True,
            response_digest=None,
            http_status=None,
            retry_after_seconds=None,
            latency_ms=Decimal("100.0005"),
        ),
        diagnostics=None,
    )

    assert fact.latency_ms == Decimal("100.001")
    assert fact.parse_stage is None
