"""Phase 16 V4 JSON 协议探针的隔离 DeepSeek Adapter。

这个模块故意不修改 ``specialist_runtime.deepseek_adapter``。后者属于 V1/V2/V3 已冻结
证据 Manifest 的 source closure；即使只为探针增加一个可选参数，也会改变历史运行的源码
摘要。V4 通过包装底层 Transport 注入 DeepSeek 顶层 ``thinking`` 字段，并把解析失败收敛
为有限枚举，既复用共享单次调用语义，也不改写任何已发送的历史证据。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any, Callable

from src.specialist_runtime.deepseek_adapter import (
    AsyncHttpResponse,
    AsyncHttpTransport,
    DeepSeekAgentModelAdapter,
    HttpxAsyncHttpTransport,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelOutcome,
    ModelRequest,
)


class DeepSeekThinkingMode(StrEnum):
    """V4 探针唯一允许的 DeepSeek 思考模式枚举。"""

    DISABLED = "disabled"


class DeepSeekOutputParseStage(StrEnum):
    """模型 HTTP 响应进入共享 Adapter 后无法消费时的脱敏阶段。"""

    CONTENT_MISSING = "CONTENT_MISSING"
    CONTENT_NON_STRING = "CONTENT_NON_STRING"
    OUTPUT_JSON_SYNTAX_INVALID = "OUTPUT_JSON_SYNTAX_INVALID"
    OUTPUT_JSON_POLICY_INVALID = "OUTPUT_JSON_POLICY_INVALID"


class DeepSeekOutputContentShape(StrEnum):
    """只保留失败正文的有限形态，避免任何模型文字进入账本。"""

    EMPTY = "EMPTY"
    MARKDOWN_CODE_FENCE = "MARKDOWN_CODE_FENCE"
    JSON_OBJECT_LIKE = "JSON_OBJECT_LIKE"
    OTHER_TEXT = "OTHER_TEXT"


class DeepSeekFinishReasonClass(StrEnum):
    """把 Provider 自由完成原因压缩为可审计的白名单类别。"""

    MISSING = "MISSING"
    STOP = "STOP"
    LENGTH = "LENGTH"
    TOOL_CALLS = "TOOL_CALLS"
    CONTENT_FILTER = "CONTENT_FILTER"
    OTHER = "OTHER"


@dataclass(frozen=True, slots=True)
class DeepSeekOutputParseDiagnostics:
    """一次失败可保留的最小协议诊断，不包含正文、Prompt 或思维链。"""

    stage: DeepSeekOutputParseStage
    content_shape: DeepSeekOutputContentShape | None
    finish_reason: DeepSeekFinishReasonClass
    reasoning_content_present: bool


class _ThinkingDisabledTransport:
    """为唯一 V4 请求装饰 HTTP payload，并在返回后立即派生脱敏诊断。

    包装器只在 ``post_json`` 栈帧内读取原始响应字节；它不会缓存 body、header、Provider ID
    或 ``reasoning_content``。共享 Adapter 仍负责 deadline、网络异常、HTTP 状态、模型身份、
    usage 和 JSON 安全策略，故探针不会复制或放宽生产调用语义。
    """

    def __init__(self, delegate: AsyncHttpTransport) -> None:
        self._delegate = delegate
        self._pending_diagnostics: DeepSeekOutputParseDiagnostics | None = None

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> AsyncHttpResponse:
        """只追加冻结的禁思考字段，并继续委托底层 Transport 执行唯一 HTTP 请求。"""

        # 每次发送前清空上一次的临时诊断。V4 账本在网络前已拒绝第二次 dispatch，所以
        # 这个对象的生命周期只会对应一个可持久化 attempt，清空仍能防止意外复用实例串值。
        self._pending_diagnostics = None
        decorated_payload = {
            **payload,
            "thinking": {"type": DeepSeekThinkingMode.DISABLED.value},
        }
        response = await self._delegate.post_json(
            url=url,
            headers=headers,
            payload=decorated_payload,
            timeout_seconds=timeout_seconds,
        )
        self._pending_diagnostics = self._diagnose_response(response)
        return response

    def pop_diagnostics(self) -> DeepSeekOutputParseDiagnostics | None:
        """取走并遗忘单次枚举诊断，禁止长生命周期对象累积模型响应信息。"""

        diagnostics, self._pending_diagnostics = self._pending_diagnostics, None
        return diagnostics

    def discard_diagnostics(self) -> None:
        """在共享 Adapter 未发请求或异常时丢弃暂存状态，避免旧事实被误关联。"""

        self._pending_diagnostics = None

    @staticmethod
    def _diagnose_response(
        response: AsyncHttpResponse,
    ) -> DeepSeekOutputParseDiagnostics | None:
        """从成功 HTTP 响应临时派生 JSON 消费诊断，所有自由文本在返回前即被丢弃。"""

        if not 200 <= response.status_code < 300:
            return None
        try:
            envelope = json.loads(response.body.decode("utf-8"))
            if not isinstance(envelope, dict):
                return None
            choices = envelope.get("choices")
            if not isinstance(choices, list) or not choices:
                return None
            choice = choices[0]
            if not isinstance(choice, dict):
                return None
            message = choice.get("message")
            if not isinstance(message, dict):
                return None
            finish_reason = choice.get("finish_reason")
            reasoning_content_present = message.get("reasoning_content") is not None
            if "content" not in message or message["content"] is None:
                return DeepSeekOutputParseDiagnostics(
                    stage=DeepSeekOutputParseStage.CONTENT_MISSING,
                    content_shape=None,
                    finish_reason=_finish_reason_class(finish_reason),
                    reasoning_content_present=reasoning_content_present,
                )
            content = message["content"]
            if not isinstance(content, str):
                return DeepSeekOutputParseDiagnostics(
                    stage=DeepSeekOutputParseStage.CONTENT_NON_STRING,
                    content_shape=None,
                    finish_reason=_finish_reason_class(finish_reason),
                    reasoning_content_present=reasoning_content_present,
                )
            try:
                output = json.loads(content)
            except (json.JSONDecodeError, RecursionError):
                return DeepSeekOutputParseDiagnostics(
                    stage=DeepSeekOutputParseStage.OUTPUT_JSON_SYNTAX_INVALID,
                    content_shape=_content_shape(content),
                    finish_reason=_finish_reason_class(finish_reason),
                    reasoning_content_present=reasoning_content_present,
                )
            if DeepSeekAgentModelAdapter._inspect_output(output) is not None:
                return DeepSeekOutputParseDiagnostics(
                    stage=DeepSeekOutputParseStage.OUTPUT_JSON_POLICY_INVALID,
                    content_shape=_content_shape(content),
                    finish_reason=_finish_reason_class(finish_reason),
                    reasoning_content_present=reasoning_content_present,
                )
        except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
            # 共享 Adapter 会将无效 envelope 归类为 INVALID_RESPONSE；此处没有足够的
            # 安全信息可进一步分类，因此返回 None 而不是猜测或保留原始异常。
            return None
        return None


class DeepSeekV4JsonProbeAdapter:
    """复用共享单次 Adapter、但冻结禁思考配置的 V4 专用模型端口。"""

    thinking_mode = DeepSeekThinkingMode.DISABLED

    def __init__(
        self,
        *,
        api_key: str,
        transport: AsyncHttpTransport | None = None,
        clock: Callable[[], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        # 即使测试注入 Fake Transport，也必须经过同一个装饰器，确保 payload 与解析诊断
        # 的契约不会只在真实 HTTP 路径才生效。
        self._probe_transport = _ThinkingDisabledTransport(
            transport or HttpxAsyncHttpTransport()
        )
        self._delegate = DeepSeekAgentModelAdapter(
            api_key=api_key,
            transport=self._probe_transport,
            clock=clock,
            monotonic=monotonic,
        )
        self._diagnostics_by_request_id: dict[str, DeepSeekOutputParseDiagnostics] = {}

    async def complete(self, request: ModelRequest) -> ModelOutcome:
        """执行共享 Adapter 的一次调用，并只为失败 outcome 暂存对应的脱敏诊断。"""

        self._diagnostics_by_request_id.pop(request.request_id, None)
        self._probe_transport.discard_diagnostics()
        outcome = await self._delegate.complete(request)
        diagnostics = self._probe_transport.pop_diagnostics()
        if isinstance(outcome, ModelFailure) and diagnostics is not None:
            self._diagnostics_by_request_id[request.request_id] = diagnostics
        return outcome

    def pop_output_parse_diagnostics(
        self, request_id: str
    ) -> DeepSeekOutputParseDiagnostics | None:
        """供 V4 Runner 在关闭失败 run 前取走一次性、无正文的解析分类。"""

        return self._diagnostics_by_request_id.pop(request_id, None)


def _content_shape(content: str) -> DeepSeekOutputContentShape:
    """将短暂读取的正文归类为有限枚举，不能返回长度、片段或原字符串。"""

    normalized = content.strip()
    if not normalized:
        return DeepSeekOutputContentShape.EMPTY
    if normalized.startswith("```"):
        return DeepSeekOutputContentShape.MARKDOWN_CODE_FENCE
    if normalized.startswith("{"):
        return DeepSeekOutputContentShape.JSON_OBJECT_LIKE
    return DeepSeekOutputContentShape.OTHER_TEXT


def _finish_reason_class(value: Any) -> DeepSeekFinishReasonClass:
    """将可能变化的 Provider finish reason 收敛为固定白名单投影。"""

    if not isinstance(value, str) or not value.strip():
        return DeepSeekFinishReasonClass.MISSING
    return {
        "stop": DeepSeekFinishReasonClass.STOP,
        "length": DeepSeekFinishReasonClass.LENGTH,
        "tool_calls": DeepSeekFinishReasonClass.TOOL_CALLS,
        "content_filter": DeepSeekFinishReasonClass.CONTENT_FILTER,
    }.get(value.strip().lower(), DeepSeekFinishReasonClass.OTHER)
