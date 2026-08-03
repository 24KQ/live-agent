"""Phase 17 holdout 独立受控 DeepSeek Adapter（不触碰 phase16 冻结闭包）。

Phase 16 的 ``DeepSeekV5ControlledE2EAdapter`` 位于 V5 身份路径（phase16
历史冻结闭包）与 multi_agent source closure 保护之下：它的字节变更会让
phase16 全链路 fail-closed（MANIFEST_IDENTITY_MISMATCH / source code digest
drift），因此 phase16 文件不可修改。Phase 17 是独立执行契约，逐 attempt
审计（codex 第十八轮 P0-3）需要渠道链每次真实网络尝试的事实，故本模块提供
phase17 自己的 adapter：继承 V5 受控语义（禁思考、90s/尝试窗口、同端点重试、
429 换端、绝对 deadline 门），同时按真实调用顺序收集 ``attempt_details``。

运行时不读任何身份 env：Phase 17 契约身份（model_id / reasoning_effort /
endpoint_hosts）由契约冻结，``LLM_API_REASONING_EFFORT`` /
``LLM_API_MODEL_ID`` 非空即拒绝（fail-closed，杜绝运行环境覆盖契约身份）。
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol

from pydantic import ConfigDict, Field

from src.specialist_runtime.deepseek_adapter import (
    AsyncHttpResponse,
    AsyncHttpTransport,
    DeepSeekAgentModelAdapter,
    HttpxAsyncHttpTransport,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelOutcome,
    ModelRequest,
    ModelSuccess,
)
from src.specialist_runtime.models import StrictFrozenModel
from src.specialist_runtime.profiles import (
    FORMAL_ENDPOINT_HOSTS,
    FORMAL_MODEL_IDS,
    normalize_endpoint_host,
)

from src.decision_support.controlled_e2e_adapter_v5 import (
    DeepSeekV5ControlledE2EAdapter,
    _V5ThinkingDisabledTransport,
)
from src.decision_support.phase17_holdout_capture import (
    Phase17ArtifactCapture,
    Phase17CaptureAttempt,
    Phase17CaptureTransport,
)


class Phase17AttemptDetail(StrictFrozenModel):
    """渠道链内单次网络尝试的事实（codex 第十八轮 P0-3 逐 attempt 审计）。

    Phase 17 adapter 按序收集每次真实网络调用（含同端点重试与渠道换端），
    随最终 outcome 一并返回；runner 据此为每次尝试写入独立
    ``phase17_holdout_attempts`` 行。无明细的调用方（历史 fake port / 测试
    脚本端口）返回空 tuple，runner 按单次尝试兜底。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_index: int = Field(..., ge=1, strict=True)
    endpoint_host: str = Field(..., min_length=1)
    outcome: Literal["PASS", "FAILED"]
    category: ModelFailureCategory | None = None
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    latency_ms: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    response_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_response_id: str | None = Field(default=None, min_length=1)
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)
    artifact_path: str | None = Field(default=None, min_length=1)
    artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_capture_status: Literal["CAPTURED", "UNAVAILABLE", "FAILED"] = "UNAVAILABLE"


@dataclass(frozen=True)
class Phase17AdapterOutcome:
    """一次 stage 的最终 outcome 与其逐尝试明细（同一次渠道链执行的投影）。"""

    outcome: ModelOutcome
    attempt_details: tuple[Phase17AttemptDetail, ...]
    capture_failed: bool = False


class Phase17ModelPort(Protocol):
    """Phase 17 runner 期望的模型端口：返回 outcome + 逐尝试明细。"""

    async def complete(self, request: ModelRequest) -> Phase17AdapterOutcome:
        """执行一次渠道链；不隐藏重试、failover 或逐次尝试证据。"""


def _attempt_detail(
    outcome: ModelOutcome,
    *,
    attempt_index: int,
    endpoint_host: str,
    capture_attempt: Phase17CaptureAttempt | None = None,
) -> Phase17AttemptDetail:
    """把单次网络尝试的事实转成 Phase17AttemptDetail。"""

    artifact_fields = (
        {
            "artifact_path": capture_attempt.artifact_path,
            "artifact_digest": capture_attempt.artifact_digest,
            "artifact_capture_status": (
                "CAPTURED"
                if capture_attempt.captured
                else ("FAILED" if capture_attempt.error else "UNAVAILABLE")
            ),
        }
        if capture_attempt is not None
        else {}
    )
    if isinstance(outcome, ModelSuccess):
        usage = outcome.usage
        return Phase17AttemptDetail(
            attempt_index=attempt_index,
            endpoint_host=endpoint_host,
            outcome="PASS",
            category=None,
            http_status=None,
            latency_ms=outcome.latency_ms,
            response_digest=outcome.response_digest,
            provider_response_id=outcome.provider_response_id,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            **artifact_fields,
        )
    return Phase17AttemptDetail(
        attempt_index=attempt_index,
        endpoint_host=endpoint_host,
        outcome="FAILED",
        category=outcome.category,
        http_status=outcome.http_status,
        latency_ms=outcome.latency_ms,
        response_digest=outcome.response_digest,
        provider_response_id=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        **artifact_fields,
    )


def _stamp_attempt(outcome: ModelOutcome, *, attempts: int, endpoint_host: str) -> ModelOutcome:
    """给冻结结果补 attempts/endpoint_host 事实（与 V5 同款 JSON 往返重建）。

    ``StrictFrozenModel`` 禁止 ``model_copy(update=...)``，因此用 JSON 往返
    重建：全部字段原值回传，只覆盖两个事实字段。stamp 的是请求端点的
    normalized 值与非负尝试计数，不会改变 outcome 的语义。
    """

    payload = outcome.model_dump(mode="json")
    payload["attempts"] = attempts
    payload["endpoint_host"] = endpoint_host
    return type(outcome).model_validate(payload)


class Phase17V5ControlledE2EAdapter(DeepSeekV5ControlledE2EAdapter):
    """Phase 17 专属模型端口：V5 受控语义 + 逐尝试审计事实。

    继承 V5 的构造器白名单（FORMAL_ENDPOINT_HOSTS / key 非空 / host 唯一）与
    传输层重试/换端语义（同端点最多 2 次、TRANSPORT_ERROR/5xx/DEADLINE_EXCEEDED
    可重试、429 换端、每次尝试独立 90s 窗口、绝对 deadline 门）。唯一差异：

    - 运行时不接受任何身份 env（``LLM_API_REASONING_EFFORT`` /
      ``LLM_API_MODEL_ID`` 非空即拒绝）——Phase 17 身份由契约冻结；
    - ``complete()`` 返回 ``Phase17AdapterOutcome``（最终 outcome + 逐次
      网络尝试的明细），runner 据此逐行入账。
    """

    def __init__(
        self,
        *,
        endpoints: tuple[tuple[str, str], ...],
        transport: AsyncHttpTransport | None = None,
        capture: Phase17ArtifactCapture | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """先拒绝身份 env（契约身份不可由运行环境覆盖），再按 V5 语义装配。"""

        if os.environ.get("LLM_API_REASONING_EFFORT", "").strip():
            raise ValueError(
                "phase17 contract fixes reasoning_effort=null; "
                "LLM_API_REASONING_EFFORT must be unset"
            )
        if os.environ.get("LLM_API_MODEL_ID", "").strip():
            raise ValueError(
                "phase17 contract fixes model_id; LLM_API_MODEL_ID must be unset"
            )
        self._capture = capture
        base_transport = transport or HttpxAsyncHttpTransport()
        if capture is not None:
            base_transport = Phase17CaptureTransport(base_transport, capture=capture)
        super().__init__(
            endpoints=endpoints,
            transport=base_transport,
            clock=clock,
            monotonic=monotonic,
            sleep=sleep,
        )

    def bind_capture_context(
        self,
        *,
        run_id: str,
        case_id: str,
        stage: str,
    ):
        """绑定 runner 的 stage 身份；未启用 capture 时保持兼容的空上下文。"""

        if self._capture is None:
            return nullcontext()
        return self._capture.bind_stage(run_id=run_id, case_id=case_id, stage=stage)

    async def complete(self, request: ModelRequest) -> Phase17AdapterOutcome:
        """按渠道有序列表执行（V5 语义），并按真实调用顺序收集逐次尝试事实。

        与 V5 完全相同的重试/换端/deadline 行为；每轮尝试的 outcome 事实
        （端点、分类、状态、tokens、响应摘要）先进入 ``attempt_details``，
        最终返回值同时携带 stamp 后的最终 outcome 与全链尝试明细。
        """

        attempts = 0
        last_outcome: ModelOutcome | None = None
        endpoint_request = request
        attempt_details: list[Phase17AttemptDetail] = []
        for delegate, host in self._chain:
            for _ in range(self._MAX_ATTEMPTS_PER_ENDPOINT):
                attempts += 1
                # 每次尝试重建：换端 host + 独立窗口 deadline（覆盖 profile 总 deadline）。
                # StrictFrozenModel 禁止 model_copy(update=...)；重建会重跑 endpoint
                # 校验，渠道 host 已在上层手工通过 normalize + FORMAL 白名单。
                payload = request.model_dump(mode="json")
                payload["endpoint_host"] = host
                payload["deadline_at"] = self._attempt_deadline(
                    request, self._clock
                ).isoformat()
                endpoint_request = ModelRequest.model_validate(payload)
                attempt_context = (
                    self._capture.begin_attempt(attempt_index=attempts)
                    if self._capture is not None
                    else nullcontext(None)
                )
                with attempt_context as capture_attempt:
                    outcome = await delegate.complete(endpoint_request)
                    if capture_attempt is not None:
                        try:
                            self._capture.require_captured(capture_attempt)
                        except Exception:
                            # capture 失败是审计硬阻断，不允许把它伪装成可重试
                            # 的网络错误继续走渠道链；具体原因保留在 attempt
                            # 状态中，由 runner 写入 BLOCKED 终态。
                            pass
                attempt_details.append(
                    _attempt_detail(
                        outcome,
                        attempt_index=attempts,
                        endpoint_host=endpoint_request.endpoint_host,
                        capture_attempt=capture_attempt,
                    )
                )
                if capture_attempt is not None and capture_attempt.error is not None:
                    return Phase17AdapterOutcome(
                        outcome=_stamp_attempt(
                            outcome,
                            attempts=attempts,
                            endpoint_host=endpoint_request.endpoint_host,
                        ),
                        attempt_details=tuple(attempt_details),
                        capture_failed=True,
                    )
                if isinstance(outcome, ModelSuccess):
                    return Phase17AdapterOutcome(
                        outcome=_stamp_attempt(
                            outcome,
                            attempts=attempts,
                            endpoint_host=endpoint_request.endpoint_host,
                        ),
                        attempt_details=tuple(attempt_details),
                    )
                last_outcome = outcome
                if self._retryable(outcome):
                    if self._remaining_seconds(request, self._clock) < self._MIN_RETRY_WINDOW_SECONDS:
                        break
                    if outcome.http_status is not None and outcome.http_status >= 500:
                        # 退避后仍需保留最小重试窗口：剩余时间不足 2 倍窗口时，
                        # 压缩退避而非压掉窗口，确保重试调用不会在 deadline 边缘发出。
                        await self._sleep(
                            min(
                                self._RETRY_BACKOFF_SECONDS,
                                self._remaining_seconds(request, self._clock)
                                - self._MIN_RETRY_WINDOW_SECONDS,
                            )
                        )
                    continue
                if outcome.category is ModelFailureCategory.RATE_LIMITED:
                    # 429 不重试同端点：直接走渠道链下一端点（换端前仍受最小窗口门约束）。
                    break
                return Phase17AdapterOutcome(
                    outcome=_stamp_attempt(
                        outcome,
                        attempts=attempts,
                        endpoint_host=endpoint_request.endpoint_host,
                    ),
                    attempt_details=tuple(attempt_details),
                )
            if self._remaining_seconds(request, self._clock) < self._MIN_RETRY_WINDOW_SECONDS:
                break
        assert last_outcome is not None
        return Phase17AdapterOutcome(
            outcome=_stamp_attempt(
                last_outcome,
                attempts=attempts,
                endpoint_host=endpoint_request.endpoint_host,
            ),
            attempt_details=tuple(attempt_details),
        )


def phase17_adapter_digest(*, repository_root: Path) -> str:
    """Phase 17 实际发送 Adapter 的源文件 digest（替代 phase16 的 v5 版本）。

    candidate 的 adapter_digest 绑定本文件字节，防止"换实现仍复用 profile
    成绩"。phase16 的 ``qualification_adapter_digest`` 保持 digest V5 文件
    不变（phase16 历史冻结）；Phase 17 用自己的 digest 函数。
    """

    path = repository_root / "src" / "specialist_runtime" / "phase17_v5_adapter.py"
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise ValueError("phase17 adapter source must be UTF-8 LF without BOM")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("phase17 adapter source is not valid UTF-8") from exc
    return hashlib.sha256(raw).hexdigest()
