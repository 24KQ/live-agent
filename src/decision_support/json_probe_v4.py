"""Phase 16 V4 禁思考 JSON 协议探针及其独立 append-only 账本。

本模块不是经营决策 Agent，也不重跑 V1/V2/V3。它只验证 DeepSeek V4 Pro 在明确关闭
思考模式时，能否经现有 Adapter 返回一个固定、无业务含义的 JSON 对象。所有持久化事实
均为摘要、枚举、布尔值和计量值；Prompt、API Key、模型正文及思维链不会离开进程内存。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from hashlib import sha256
import hmac
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from pydantic import ConfigDict, Field, model_validator

from src.specialist_runtime.model_port import (
    AgentModelPort,
    ModelFailure,
    ModelFailureCategory,
    ModelMessage,
    ModelRequest,
    ModelSuccess,
)
from src.decision_support.v4_json_probe_adapter import (
    DeepSeekFinishReasonClass,
    DeepSeekOutputContentShape,
    DeepSeekOutputParseDiagnostics,
    DeepSeekOutputParseStage,
    DeepSeekThinkingMode,
)
from src.specialist_runtime.models import StrictFrozenModel, canonical_json_sha256
from src.specialist_runtime.profiles import FORMAL_ENDPOINT_HOST


PHASE16_V4_JSON_PROBE_RUN_ID = "phase16-v4-json-probe-001"
PHASE16_V4_JSON_PROBE_CASE_ID = "phase16-v4-json-probe-minimal-json-001"
PHASE16_V4_JSON_PROBE_MODEL_ID = "deepseek-v4-pro"
PHASE16_V4_JSON_PROBE_TOTAL_BUDGET_CNY = Decimal("1.000000")
# V1/V2/V3 的不可修改历史和已预约最大暴露，来自 V3 证据报告；探针用此保守值继续
# 计入同一个 Phase 16 上限，不能因为名称是“协议诊断”而绕开总预算。
PHASE16_V4_JSON_PROBE_PRIOR_EXPOSURE_CNY = Decimal("0.202165")
PHASE16_V4_JSON_PROBE_RESERVATION_CNY = Decimal("0.010000")
PHASE16_V4_JSON_PROBE_MAX_OUTPUT_TOKENS = 64
PHASE16_V4_JSON_PROBE_DEADLINE_SECONDS = 30
_EXPECTED_OUTPUT = {"status": "ok"}
_SYSTEM_PROMPT = "Return exactly one JSON object and no markdown."
_USER_PROMPT = 'Return this exact JSON object: {"status":"ok"}'


class Phase16V4JsonProbeError(RuntimeError):
    """V4 探针账本的稳定错误，不包含数据库异常、模型正文或凭据。"""


class Phase16V4JsonProbeStatus(StrEnum):
    """单次探针的封闭终态，任何已发送失败都不能被重试覆盖。"""

    PASS = "PASS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class Phase16V4JsonProbeProtocol(StrictFrozenModel):
    """一次协议探针的冻结输入身份，不保存可读 Prompt 正文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = PHASE16_V4_JSON_PROBE_MODEL_ID
    endpoint_host: str = FORMAL_ENDPOINT_HOST
    thinking_mode: DeepSeekThinkingMode = DeepSeekThinkingMode.DISABLED
    max_output_tokens: int = Field(PHASE16_V4_JSON_PROBE_MAX_OUTPUT_TOKENS, ge=1)
    deadline_seconds: int = Field(PHASE16_V4_JSON_PROBE_DEADLINE_SECONDS, ge=1)
    expected_output_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    prompt_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    protocol_digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def create(cls) -> "Phase16V4JsonProbeProtocol":
        """以代码内静态无业务 JSON 构造不可变协议，不接受命令行自由 Prompt。"""

        return cls(
            expected_output_digest=canonical_json_sha256(_EXPECTED_OUTPUT),
            prompt_digest=sha256(
                f"{_SYSTEM_PROMPT}\x1f{_USER_PROMPT}".encode("utf-8")
            ).hexdigest(),
        )

    @model_validator(mode="after")
    def _bind_protocol_identity(self) -> "Phase16V4JsonProbeProtocol":
        """禁止更换模型、端点、思考模式或 token 上限后仍复用同一探针身份。"""

        if self.model_id != PHASE16_V4_JSON_PROBE_MODEL_ID:
            raise ValueError("V4 probe model identity is frozen")
        if self.endpoint_host != FORMAL_ENDPOINT_HOST:
            raise ValueError("V4 probe endpoint identity is frozen")
        if self.thinking_mode is not DeepSeekThinkingMode.DISABLED:
            raise ValueError("V4 probe must explicitly disable thinking")
        if self.max_output_tokens != PHASE16_V4_JSON_PROBE_MAX_OUTPUT_TOKENS:
            raise ValueError("V4 probe max_output_tokens is frozen")
        if self.deadline_seconds != PHASE16_V4_JSON_PROBE_DEADLINE_SECONDS:
            raise ValueError("V4 probe deadline is frozen")
        payload = self.model_dump(
            mode="json", exclude={"protocol_digest"}, exclude_none=True
        )
        calculated = canonical_json_sha256(payload)
        if self.protocol_digest and self.protocol_digest != calculated:
            raise ValueError("V4 probe protocol_digest conflicts with protocol facts")
        object.__setattr__(self, "protocol_digest", calculated)
        return self


class Phase16V4JsonProbeFailureFact(StrictFrozenModel):
    """从共享 ModelFailure 投影出的脱敏失败事实，可安全写入 V4 独立账本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str = Field(..., min_length=1)
    category: ModelFailureCategory
    request_sent: bool
    response_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    retry_after_seconds: int | None = Field(default=None, ge=0, strict=True)
    latency_ms: Decimal = Field(..., ge=Decimal("0"))
    parse_stage: DeepSeekOutputParseStage | None = None
    content_shape: DeepSeekOutputContentShape | None = None
    finish_reason: DeepSeekFinishReasonClass | None = None
    reasoning_content_present: bool | None = None
    fact_digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _bind_fact_identity(self) -> "Phase16V4JsonProbeFailureFact":
        """将延迟量化并与全部安全字段绑定，避免 PostgreSQL 精度造成认证漂移。"""

        try:
            UUID(self.attempt_id)
        except (AttributeError, ValueError) as error:
            raise ValueError("V4 probe attempt_id must be a UUID") from error
        diagnostics = (
            self.parse_stage,
            self.content_shape,
            self.finish_reason,
            self.reasoning_content_present,
        )
        if any(item is not None for item in diagnostics) and not all(
            item is not None for item in diagnostics
        ):
            raise ValueError("V4 probe diagnostics must be all present or all absent")
        normalized_latency = self.latency_ms.quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )
        object.__setattr__(self, "latency_ms", normalized_latency)
        payload = self.model_dump(mode="json", exclude={"fact_digest"})
        calculated = canonical_json_sha256(payload)
        if self.fact_digest and self.fact_digest != calculated:
            raise ValueError("V4 probe failure fact digest conflicts with facts")
        object.__setattr__(self, "fact_digest", calculated)
        return self

    @classmethod
    def from_model_failure(
        cls,
        *,
        attempt_id: str,
        outcome: ModelFailure,
        diagnostics: DeepSeekOutputParseDiagnostics | None,
    ) -> "Phase16V4JsonProbeFailureFact":
        """逐项投影端口失败；没有诊断时必须保留为 None 而不是猜测模型正文。"""

        if not isinstance(outcome, ModelFailure):
            raise TypeError("V4 probe failure fact requires ModelFailure")
        return cls(
            attempt_id=attempt_id,
            category=outcome.category,
            request_sent=outcome.request_sent,
            response_digest=outcome.response_digest,
            http_status=outcome.http_status,
            retry_after_seconds=outcome.retry_after_seconds,
            latency_ms=outcome.latency_ms,
            parse_stage=None if diagnostics is None else diagnostics.stage,
            content_shape=None if diagnostics is None else diagnostics.content_shape,
            finish_reason=None if diagnostics is None else diagnostics.finish_reason,
            reasoning_content_present=(
                None if diagnostics is None else diagnostics.reasoning_content_present
            ),
        )


@dataclass(frozen=True)
class Phase16V4JsonProbeAttempt:
    """已在网络调用前线性化的唯一发送意图。"""

    attempt_id: str
    internal_request_id: str


@dataclass(frozen=True)
class Phase16V4JsonProbeReport:
    """命令入口可输出的最小脱敏结论，不含模型正文或 Provider 原始 ID。"""

    status: Phase16V4JsonProbeStatus
    reason_code: str
    attempt_id: str | None
    parse_stage: DeepSeekOutputParseStage | None = None
    content_shape: DeepSeekOutputContentShape | None = None
    finish_reason: DeepSeekFinishReasonClass | None = None
    reasoning_content_present: bool | None = None


class PostgresPhase16V4JsonProbeLedger:
    """V4 探针的最小 PostgreSQL 账本：单 run、单 attempt、append-only、零重试。"""

    def __init__(self, settings: Any, *, hmac_key: bytes) -> None:
        if len(hmac_key) < 32:
            raise ValueError("V4 probe HMAC key must contain at least 256 bits")
        self._settings = settings
        self._hmac_key = hmac_key

    def _connection(self):
        """每个账本操作独占事务连接，行锁才可以把预算与唯一发送意图线性化。"""

        return psycopg.connect(
            **self._settings.postgres_connection_kwargs,
            row_factory=dict_row,
        )

    def ensure_run(self, protocol: Phase16V4JsonProbeProtocol) -> None:
        """初始化或复验固定 run；同名 run 的协议摘要漂移必须 fail-closed。"""

        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_v4_json_probe_runs
                           (run_id, protocol_digest, total_budget_cny, reservation_cny)
                           VALUES (%s,%s,%s,%s)
                           ON CONFLICT (run_id) DO NOTHING""",
                        (
                            PHASE16_V4_JSON_PROBE_RUN_ID,
                            protocol.protocol_digest,
                            PHASE16_V4_JSON_PROBE_TOTAL_BUDGET_CNY,
                            PHASE16_V4_JSON_PROBE_RESERVATION_CNY,
                        ),
                    )
                    cursor.execute(
                        """INSERT INTO phase16_v4_json_probe_prior_exposures
                           (run_id, source_code, amount_cny)
                           VALUES (%s,%s,%s) ON CONFLICT (run_id, source_code) DO NOTHING""",
                        (
                            PHASE16_V4_JSON_PROBE_RUN_ID,
                            "PHASE16_V1_V2_V3_CONSERVATIVE_EXPOSURE",
                            PHASE16_V4_JSON_PROBE_PRIOR_EXPOSURE_CNY,
                        ),
                    )
                    cursor.execute(
                        """SELECT protocol_digest, total_budget_cny, reservation_cny
                             FROM phase16_v4_json_probe_runs WHERE run_id=%s FOR UPDATE""",
                        (PHASE16_V4_JSON_PROBE_RUN_ID,),
                    )
                    row = cursor.fetchone()
                    if row is None or (
                        row["protocol_digest"] != protocol.protocol_digest
                        or Decimal(row["total_budget_cny"])
                        != PHASE16_V4_JSON_PROBE_TOTAL_BUDGET_CNY
                        or Decimal(row["reservation_cny"])
                        != PHASE16_V4_JSON_PROBE_RESERVATION_CNY
                    ):
                        raise Phase16V4JsonProbeError("V4 JSON probe run identity conflicts")
                connection.commit()
        except Phase16V4JsonProbeError:
            raise
        except psycopg.Error as error:
            raise Phase16V4JsonProbeError("V4 JSON probe ledger initialization failed") from error

    def begin_dispatch(self, *, internal_request_id: str) -> Phase16V4JsonProbeAttempt:
        """先锁定预算和唯一 slot，再允许网络调用；第二次发送永远不能开始。"""

        self._require_uuid(internal_request_id, "internal_request_id")
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT total_budget_cny, reservation_cny
                             FROM phase16_v4_json_probe_runs WHERE run_id=%s FOR UPDATE""",
                        (PHASE16_V4_JSON_PROBE_RUN_ID,),
                    )
                    run = cursor.fetchone()
                    if run is None:
                        raise Phase16V4JsonProbeError("V4 JSON probe run is not initialized")
                    cursor.execute(
                        """SELECT 1 FROM phase16_v4_json_probe_outcomes WHERE run_id=%s""",
                        (PHASE16_V4_JSON_PROBE_RUN_ID,),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase16V4JsonProbeError("V4 JSON probe run is terminal")
                    cursor.execute(
                        """SELECT 1 FROM phase16_v4_json_probe_dispatch_attempts WHERE run_id=%s""",
                        (PHASE16_V4_JSON_PROBE_RUN_ID,),
                    )
                    if cursor.fetchone() is not None:
                        raise Phase16V4JsonProbeError("V4 JSON probe dispatch already exists")
                    cursor.execute(
                        """SELECT COALESCE(sum(amount_cny), 0) AS prior_exposure
                             FROM phase16_v4_json_probe_prior_exposures WHERE run_id=%s""",
                        (PHASE16_V4_JSON_PROBE_RUN_ID,),
                    )
                    prior_exposure = Decimal(cursor.fetchone()["prior_exposure"])
                    if prior_exposure + Decimal(run["reservation_cny"]) > Decimal(
                        run["total_budget_cny"]
                    ):
                        raise Phase16V4JsonProbeError("V4 JSON probe budget exposure exceeded")
                    attempt_id = str(uuid5(NAMESPACE_URL, f"{PHASE16_V4_JSON_PROBE_RUN_ID}:attempt"))
                    cursor.execute(
                        """INSERT INTO phase16_v4_json_probe_dispatch_attempts
                           (attempt_id, run_id, case_id, internal_request_id)
                           VALUES (%s,%s,%s,%s)""",
                        (
                            attempt_id,
                            PHASE16_V4_JSON_PROBE_RUN_ID,
                            PHASE16_V4_JSON_PROBE_CASE_ID,
                            internal_request_id,
                        ),
                    )
                connection.commit()
        except Phase16V4JsonProbeError:
            raise
        except psycopg.Error as error:
            raise Phase16V4JsonProbeError("V4 JSON probe dispatch failed") from error
        return Phase16V4JsonProbeAttempt(attempt_id=attempt_id, internal_request_id=internal_request_id)

    def append_failure(self, fact: Phase16V4JsonProbeFailureFact) -> None:
        """追加端口失败及其脱敏诊断；数据库禁止它与同 attempt 的成功回执并存。"""

        auth_tag = self._sign("failure", {"fact_digest": fact.fact_digest})
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_v4_json_probe_failure_facts
                           (attempt_id, failure_category, request_sent, response_digest, http_status,
                            retry_after_seconds, latency_ms, parse_stage, content_shape, finish_reason,
                            reasoning_content_present, fact_digest, auth_tag)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            fact.attempt_id,
                            fact.category.value,
                            fact.request_sent,
                            fact.response_digest,
                            fact.http_status,
                            fact.retry_after_seconds,
                            fact.latency_ms,
                            None if fact.parse_stage is None else fact.parse_stage.value,
                            None if fact.content_shape is None else fact.content_shape.value,
                            None if fact.finish_reason is None else fact.finish_reason.value,
                            fact.reasoning_content_present,
                            fact.fact_digest,
                            auth_tag,
                        ),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise Phase16V4JsonProbeError("V4 JSON probe failure append failed") from error

    def append_receipt(self, *, attempt_id: str, success: ModelSuccess) -> bool:
        """追加成功 HTTP/JSON 回执，但只有完整 usage 与 Provider 身份才可用于 PASS。"""

        self._require_uuid(attempt_id, "attempt_id")
        self._require_uuid(success.request_id, "success.request_id")
        provider_response_id_digest = (
            None
            if success.provider_response_id is None
            else sha256(success.provider_response_id.encode("utf-8")).hexdigest()
        )
        finish_reason = self._finish_reason_class(success.finish_reason)
        usage = success.usage
        receipt_payload = {
            "attempt_id": attempt_id,
            "provider_response_id_digest": provider_response_id_digest,
            "finish_reason": finish_reason.value,
            "model_id": success.model_id,
            "response_digest": success.response_digest,
            "input_tokens": None if usage is None else usage.input_tokens,
            "output_tokens": None if usage is None else usage.output_tokens,
            "total_tokens": None if usage is None else usage.total_tokens,
            "latency_ms": str(
                success.latency_ms.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            ),
            "output_digest": canonical_json_sha256(success.output),
        }
        complete = (
            provider_response_id_digest is not None
            and finish_reason is DeepSeekFinishReasonClass.STOP
            and success.model_id == PHASE16_V4_JSON_PROBE_MODEL_ID
            and usage is not None
        )
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT internal_request_id FROM phase16_v4_json_probe_dispatch_attempts
                             WHERE attempt_id=%s FOR UPDATE""",
                        (attempt_id,),
                    )
                    attempt = cursor.fetchone()
                    if attempt is None or str(attempt["internal_request_id"]) != success.request_id:
                        raise Phase16V4JsonProbeError("V4 JSON probe receipt identity conflicts")
                    cursor.execute(
                        """INSERT INTO phase16_v4_json_probe_receipts
                           (attempt_id, provider_response_id_digest, finish_reason, model_id, response_digest,
                            input_tokens, output_tokens, total_tokens, latency_ms, output_digest,
                            receipt_complete, auth_tag)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            attempt_id,
                            provider_response_id_digest,
                            finish_reason.value,
                            success.model_id,
                            success.response_digest,
                            receipt_payload["input_tokens"],
                            receipt_payload["output_tokens"],
                            receipt_payload["total_tokens"],
                            receipt_payload["latency_ms"],
                            receipt_payload["output_digest"],
                            complete,
                            self._sign("receipt", receipt_payload),
                        ),
                    )
                connection.commit()
        except Phase16V4JsonProbeError:
            raise
        except psycopg.Error as error:
            raise Phase16V4JsonProbeError("V4 JSON probe receipt append failed") from error
        return complete

    def close(self, *, status: Phase16V4JsonProbeStatus, reason_code: str) -> None:
        """追加唯一终态；触发器校验 PASS/FAILED/BLOCKED 与底层事实的对应关系。"""

        self._require_reason_code(reason_code)
        digest = canonical_json_sha256(
            {"status": status.value, "reason_code": reason_code}
        )
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO phase16_v4_json_probe_outcomes
                           (run_id, status, reason_code, outcome_digest)
                           VALUES (%s,%s,%s,%s)""",
                        (PHASE16_V4_JSON_PROBE_RUN_ID, status.value, reason_code, digest),
                    )
                connection.commit()
        except psycopg.Error as error:
            raise Phase16V4JsonProbeError("V4 JSON probe outcome append failed") from error

    def _sign(self, domain: str, payload: dict[str, Any]) -> str:
        """以独立 domain 签名脱敏事实，禁止 V3 或其他账本标签被跨表复用。"""

        message = f"phase16-v4-json-probe:{domain}:{canonical_json_sha256(payload)}".encode(
            "utf-8"
        )
        return hmac.new(self._hmac_key, message, "sha256").hexdigest()

    @staticmethod
    def _finish_reason_class(value: str | None) -> DeepSeekFinishReasonClass:
        """与 Adapter 使用同一固定分类，receipt 不接受 Provider 自由完成原因文本。"""

        if not value or not value.strip():
            return DeepSeekFinishReasonClass.MISSING
        return {
            "stop": DeepSeekFinishReasonClass.STOP,
            "length": DeepSeekFinishReasonClass.LENGTH,
            "tool_calls": DeepSeekFinishReasonClass.TOOL_CALLS,
            "content_filter": DeepSeekFinishReasonClass.CONTENT_FILTER,
        }.get(value.strip().lower(), DeepSeekFinishReasonClass.OTHER)

    @staticmethod
    def _require_uuid(value: str, name: str) -> None:
        """内部 request/attempt 身份必须是 UUID，异常文本不能进入身份列。"""

        try:
            UUID(value)
        except (AttributeError, ValueError) as error:
            raise ValueError(f"{name} must be a UUID") from error

    @staticmethod
    def _require_reason_code(value: str) -> None:
        """对外报告只消费封闭大写 reason code，不能拼接供应商异常详情。"""

        if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in value):
            raise ValueError("V4 JSON probe reason code is invalid")


class Phase16V4JsonProbeRunner:
    """执行唯一的无业务 JSON 请求，不创建 AgentAction、Proposal 或任何经营动作。"""

    def __init__(
        self,
        *,
        ledger: PostgresPhase16V4JsonProbeLedger,
        model_port: AgentModelPort,
        clock: Any | None = None,
    ) -> None:
        self._ledger = ledger
        self._model_port = model_port
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(self) -> Phase16V4JsonProbeReport:
        """预写 intent 后仅调用一次；任何已发送失败关闭 run，不提供重试分支。"""

        protocol = Phase16V4JsonProbeProtocol.create()
        if getattr(self._model_port, "thinking_mode", None) is not protocol.thinking_mode:
            # 思考开关是 Adapter 实例配置而非共享 ModelRequest 字段；发送前必须精确匹配
            # Probe Manifest，防止调用方拿普通默认 Adapter 伪装成禁思考测试。
            raise Phase16V4JsonProbeError("V4 JSON probe adapter thinking mode conflicts")
        self._ledger.ensure_run(protocol)
        request = self._request(protocol)
        attempt = self._ledger.begin_dispatch(internal_request_id=request.request_id)
        outcome = await self._model_port.complete(request)
        if isinstance(outcome, ModelFailure):
            fact = Phase16V4JsonProbeFailureFact.from_model_failure(
                attempt_id=attempt.attempt_id,
                outcome=outcome,
                diagnostics=self._take_parse_diagnostics(outcome.request_id),
            )
            self._ledger.append_failure(fact)
            status = (
                Phase16V4JsonProbeStatus.FAILED
                if fact.request_sent
                else Phase16V4JsonProbeStatus.BLOCKED
            )
            reason_code = f"MODEL_FAILURE_{fact.category.value}"
            self._ledger.close(status=status, reason_code=reason_code)
            return Phase16V4JsonProbeReport(
                status=status,
                reason_code=reason_code,
                attempt_id=attempt.attempt_id,
                parse_stage=fact.parse_stage,
                content_shape=fact.content_shape,
                finish_reason=fact.finish_reason,
                reasoning_content_present=fact.reasoning_content_present,
            )
        if not isinstance(outcome, ModelSuccess):
            # AgentModelPort 的静态协议之外没有可信发送状态，不能把未知结果伪造为模型失败。
            raise Phase16V4JsonProbeError("V4 JSON probe model port outcome contract breached")
        receipt_complete = self._ledger.append_receipt(
            attempt_id=attempt.attempt_id,
            success=outcome,
        )
        if not receipt_complete:
            status = Phase16V4JsonProbeStatus.FAILED
            reason_code = "PROVIDER_RECEIPT_INCOMPLETE"
        elif outcome.output != _EXPECTED_OUTPUT:
            status = Phase16V4JsonProbeStatus.FAILED
            reason_code = "PROBE_OUTPUT_MISMATCH"
        else:
            status = Phase16V4JsonProbeStatus.PASS
            reason_code = "JSON_PROTOCOL_PASS"
        self._ledger.close(status=status, reason_code=reason_code)
        return Phase16V4JsonProbeReport(
            status=status,
            reason_code=reason_code,
            attempt_id=attempt.attempt_id,
        )

    def _request(self, protocol: Phase16V4JsonProbeProtocol) -> ModelRequest:
        """构造固定、最小且无业务上下文的请求，所有可变输入均在网络前排除。"""

        request_id = str(
            uuid5(NAMESPACE_URL, f"{PHASE16_V4_JSON_PROBE_RUN_ID}:request")
        )
        return ModelRequest(
            request_id=request_id,
            endpoint_host=protocol.endpoint_host,
            model_id=protocol.model_id,
            temperature=Decimal("0"),
            prompt_hash=protocol.prompt_digest,
            result_schema_hash=protocol.expected_output_digest,
            messages=(
                ModelMessage(role="system", content=_SYSTEM_PROMPT),
                ModelMessage(role="user", content=_USER_PROMPT),
            ),
            max_output_tokens=protocol.max_output_tokens,
            deadline_at=self._clock() + timedelta(seconds=protocol.deadline_seconds),
        )

    def _take_parse_diagnostics(
        self, request_id: str
    ) -> DeepSeekOutputParseDiagnostics | None:
        """从 V4 专用 Adapter 取走一次性脱敏诊断，普通 Fake/端口缺少该能力时保持未知。"""

        getter = getattr(self._model_port, "pop_output_parse_diagnostics", None)
        if getter is None:
            return None
        diagnostic = getter(request_id)
        if diagnostic is not None and not isinstance(
            diagnostic, DeepSeekOutputParseDiagnostics
        ):
            raise Phase16V4JsonProbeError("V4 JSON probe diagnostics contract breached")
        return diagnostic


def initialize_phase16_v4_json_probe_ledger_schema(settings: Any) -> None:
    """执行 V4 专属 DDL；集成测试与统一迁移复用同一 SQL，避免 schema 漂移。"""

    sql_path = Path(__file__).resolve().parents[2] / "docker" / "init_phase16_v4_json_probe.sql"
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql_path.read_text(encoding="utf-8"))
        connection.commit()
