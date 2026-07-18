"""Phase 16 受控双 Agent 真实 smoke 的预检、预算预约和最小发送门。

该模块刻意不装配 Coordinator、Store、OperatorDecision 或任何经营执行路径。它只在
显式 smoke 场景中验证外部模型身份与成本证据，默认回归继续使用 Task 9 的
ScriptedModel 重放，生产默认路由始终保持 ``DETERMINISTIC_ONLY``。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg.rows import dict_row
from pydantic import ConfigDict, Field, PrivateAttr, model_validator

from src.decision_support.multi_agent import (
    build_decision_planner_profile,
    build_evidence_analyst_profile,
)
from src.decision_support.multi_agent_evaluation import (
    Phase16EvaluationCase,
    Phase16EvaluationDataset,
    _validate_dataset_for_run,
)
from src.specialist_runtime.model_port import (
    AgentModelPort,
    ModelFailure,
    ModelMessage,
    ModelRequest,
    ModelSuccess,
    ModelUsage,
)
from src.specialist_runtime.models import StrictFrozenModel
from src.specialist_runtime.profiles import SpecialistProfile


PHASE16_MULTI_AGENT_SMOKE = "PHASE16_MULTI_AGENT_SMOKE"
PHASE16_SMOKE_BUDGET_CNY = Decimal("1.000000")
PHASE16_SMOKE_MAX_CASES = 10
PHASE16_ANALYST_RESERVATION_CNY = Decimal("0.030000")
PHASE16_PLANNER_RESERVATION_CNY = Decimal("0.070000")
PHASE16_CASE_RESERVATION_CNY = (
    PHASE16_ANALYST_RESERVATION_CNY + PHASE16_PLANNER_RESERVATION_CNY
)
FORMAL_MODEL_ID = "deepseek-v4-flash"
FORMAL_ENDPOINT_HOST = "api.deepseek.com"
INPUT_PRICE_CNY_PER_MILLION = Decimal("1.008000")
OUTPUT_PRICE_CNY_PER_MILLION = Decimal("2.016000")
_HASH_PATTERN = r"^[0-9a-f]{64}$"


class Phase16SmokeStatus(StrEnum):
    """真实 smoke 的技术证据状态，不表达业务路由或经营批准。"""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"


class Phase16SmokeReservationState(StrEnum):
    """case 级 reservation 的不可逆状态机。"""

    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class Phase16SmokeBudgetInvariantError(RuntimeError):
    """账本重放冲突、非法状态转换或无效金额的稳定错误。"""


class Phase16SmokeBudgetLimitExceeded(RuntimeError):
    """新的 case reservation 将突破 Phase 16 一元硬上限。"""


def _validate_terminal_outcome(
    *,
    reservation_state: Phase16SmokeReservationState,
    outcome_status: Phase16SmokeStatus,
) -> None:
    """让内存与 PostgreSQL ledger 共享同一个可重放终态约束。"""

    if outcome_status not in {
        Phase16SmokeStatus.PASS,
        Phase16SmokeStatus.FAIL,
        Phase16SmokeStatus.INCONCLUSIVE,
    }:
        raise Phase16SmokeBudgetInvariantError("smoke outcome must be PASS, FAIL, or INCONCLUSIVE")
    if (
        reservation_state is Phase16SmokeReservationState.RELEASED
        and outcome_status is not Phase16SmokeStatus.FAIL
    ):
        # release 只表示 Analyst 在外部请求尚未发送前失败。它没有任何可计价调用，
        # 因而既不能伪造 PASS，也不能以 INCONCLUSIVE 掩盖一个本应没有发送的 case。
        raise Phase16SmokeBudgetInvariantError("released smoke reservation must record FAIL outcome")


class Phase16SmokeConfig(StrictFrozenModel):
    """绑定真实 smoke 所有冻结身份、上限和独立 runtime 摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = Field(..., min_length=1)
    manifest_digest: str = Field(..., pattern=_HASH_PATTERN)
    dataset_digest: str = Field(..., pattern=_HASH_PATTERN)
    source_code_digest: str = Field(..., pattern=_HASH_PATTERN)
    evidence_analyst_profile_digest: str = Field(..., pattern=_HASH_PATTERN)
    decision_planner_profile_digest: str = Field(..., pattern=_HASH_PATTERN)
    official_price_digest: str = Field(..., pattern=_HASH_PATTERN)
    smoke_runtime_digest: str = Field(..., pattern=_HASH_PATTERN)
    model_id: str
    endpoint_host: str
    max_smoke_cases: int = Field(default=PHASE16_SMOKE_MAX_CASES, ge=1, le=PHASE16_SMOKE_MAX_CASES, strict=True)
    budget_cny: Decimal = Field(default=PHASE16_SMOKE_BUDGET_CNY, gt=0)
    reserved_case_budget_cny: Decimal = Field(default=PHASE16_CASE_RESERVATION_CNY, gt=0)
    usage_required: bool = True

    @model_validator(mode="after")
    def _validate_frozen_budget(self) -> "Phase16SmokeConfig":
        """防止调用方把十例一元的配置误扩展为更高的真实费用暴露。"""

        if self.model_id != FORMAL_MODEL_ID:
            raise ValueError("Phase 16 smoke model identity is frozen")
        if self.endpoint_host != FORMAL_ENDPOINT_HOST:
            raise ValueError("Phase 16 smoke endpoint identity is frozen")
        if self.max_smoke_cases != PHASE16_SMOKE_MAX_CASES:
            raise ValueError("Phase 16 smoke case limit must be exactly 10")
        if self.budget_cny != PHASE16_SMOKE_BUDGET_CNY:
            raise ValueError("Phase 16 smoke budget must be exactly 1.00 CNY")
        if self.reserved_case_budget_cny != PHASE16_CASE_RESERVATION_CNY:
            raise ValueError("Phase 16 smoke reservation must be exactly 0.10 CNY")
        if self.reserved_case_budget_cny * self.max_smoke_cases > self.budget_cny:
            raise ValueError("Phase 16 smoke reservations exceed the fixed budget")
        return self


class Phase16OfficialPriceEvidence(StrictFrozenModel):
    """官方价格表的最小可核对快照，不保存网页正文或 API 凭据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    endpoint_host: str
    input_cny_per_million: Decimal = Field(..., ge=0)
    output_cny_per_million: Decimal = Field(..., ge=0)
    official_price_digest: str = Field(..., pattern=_HASH_PATTERN)


class Phase16SmokePreflight(StrictFrozenModel):
    """真实发送前的不可变门禁结果，私有 provenance 防止调用方手工打开。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Phase16SmokeStatus
    can_send: bool
    reason_codes: tuple[str, ...] = ()
    config_digest: str = Field(..., pattern=_HASH_PATTERN)
    scope_id: str = Field(..., min_length=1)
    max_smoke_cases: int = Field(..., ge=1, le=PHASE16_SMOKE_MAX_CASES, strict=True)
    reserved_case_budget_cny: Decimal = Field(..., gt=0)
    _verified: bool = PrivateAttr(default=False)
    _dataset: Phase16EvaluationDataset | None = PrivateAttr(default=None)

    @property
    def provenance_verified(self) -> bool:
        """只有模块内 preflight 工厂创建的结果可以被 Runner 视作可信。"""

        return self._verified


@dataclass(frozen=True)
class Phase16SmokeBudgetReservation:
    """一次 case 的发送前费用预约；两次模型调用共享同一额度。"""

    scope_id: str
    reservation_id: str
    request_id: str
    reserved_amount_cny: Decimal
    state: Phase16SmokeReservationState
    settled_amount_cny: Decimal | None
    usage_known: bool | None
    outcome_status: Phase16SmokeStatus | None
    outcome_reason_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Phase16SmokeReservationClaim:
    """区分新预约和重启后按相同 case 身份恢复的既有事实。"""

    record: Phase16SmokeBudgetReservation
    created: bool


@dataclass(frozen=True)
class Phase16SmokeBudgetSnapshot:
    """供报告读取的独立 scope 余额，不引用任何历史阶段账本。"""

    scope_id: str
    reserved_cny: Decimal
    committed_cny: Decimal
    available_cny: Decimal


class Phase16SmokeReport(StrictFrozenModel):
    """真实 smoke 的最小技术报告，不含模型自由文本或经营建议。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Phase16SmokeStatus
    scope_id: str
    reason_codes: tuple[str, ...] = ()
    smoke_case_count: int = Field(..., ge=0, le=PHASE16_SMOKE_MAX_CASES, strict=True)
    model_request_count: int = Field(..., ge=0, strict=True)
    unknown_usage_case_count: int = Field(..., ge=0, strict=True)
    settled_cost_cny: Decimal = Field(..., ge=0)
    replayed_case_count: int = Field(..., ge=0, strict=True)


def _canonical_json_digest(value: Any) -> str:
    """把 Config 等冻结 JSON 规范化为稳定摘要，防止预检跨配置复用。"""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def phase16_smoke_runtime_digest() -> str:
    """计算本发送门源码摘要，使 Task 9 的 Coordinator 闭包之外仍有显式代码身份。"""

    source = Path(__file__).read_text(encoding="utf-8-sig")
    normalized = source.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return sha256(normalized).hexdigest()


def _config_digest(config: Phase16SmokeConfig) -> str:
    """只从 Pydantic JSON 边界计算配置摘要，避免 Decimal/枚举运行时表示漂移。"""

    return _canonical_json_digest(config.model_dump(mode="json"))


def _verified_preflight(**facts: Any) -> Phase16SmokePreflight:
    """集中设置私有可信标记，外部构造 ``can_send=True`` 不具备发送能力。"""

    result = Phase16SmokePreflight.model_validate(facts)
    object.__setattr__(result, "_verified", True)
    return result


def _phase16_scope_id(scope_id: str) -> str:
    """只允许唯一全局 Phase 16 scope，禁止通过后缀创建额外的一元预算池。"""

    if scope_id == PHASE16_MULTI_AGENT_SMOKE:
        return scope_id
    raise Phase16SmokeBudgetInvariantError("Phase 16 smoke scope must equal PHASE16_MULTI_AGENT_SMOKE")


def _amount(value: Decimal, *, allow_zero: bool = False) -> Decimal:
    """拒绝 NaN、Infinity、过高精度和超出一元上限的账本金额。"""

    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise Phase16SmokeBudgetInvariantError("Phase 16 smoke amount must be finite and positive")
    try:
        quantized = amount.quantize(Decimal("0.000001"))
    except InvalidOperation as error:
        raise Phase16SmokeBudgetInvariantError("Phase 16 smoke amount exceeds six-place precision") from error
    if amount != quantized or amount > PHASE16_SMOKE_BUDGET_CNY:
        raise Phase16SmokeBudgetInvariantError("Phase 16 smoke amount exceeds the fixed budget")
    return amount


def _reservation_id(scope_id: str, request_id: str) -> str:
    """从 scope 和 case 身份派生可重放 UUID，不让调用方提供自由 reservation ID。"""

    return str(uuid5(NAMESPACE_URL, f"{scope_id}\x1f{request_id}"))


def preflight_phase16_multi_agent_smoke(
    config: Phase16SmokeConfig,
    *,
    dataset: Phase16EvaluationDataset,
    official_price: Phase16OfficialPriceEvidence,
    endpoint_available: bool,
    usage_contract_available: bool,
    scope_id: str = PHASE16_MULTI_AGENT_SMOKE,
) -> Phase16SmokePreflight:
    """验证真实发送的全部前置事实；本函数不会探测网络或读取任何 API key。"""

    scope = _phase16_scope_id(scope_id)
    reasons: list[str] = []
    # 预检接到的聚合虽然来自可信启动装配，仍必须重算 Task 9 generator、source closure、
    # case 与数据集摘要。这样嵌套 dict 在同进程被意外修改时，也不能进入模型正文。
    try:
        _validate_dataset_for_run(dataset)
    except (OSError, UnicodeError, ValueError):
        # 预检错误也必须形成稳定、可审计的零发送结果，不能把磁盘或摘要异常直接泄漏给调用方。
        reasons.append("TASK9_DATASET_INVALID")
    analyst = build_evidence_analyst_profile()
    planner = build_decision_planner_profile()
    manifest = dataset.manifest
    if not endpoint_available:
        reasons.append("ENDPOINT_UNAVAILABLE")
    if not usage_contract_available or not config.usage_required:
        reasons.append("USAGE_CONTRACT_UNAVAILABLE")
    if config.model_id != FORMAL_MODEL_ID:
        reasons.append("MODEL_ID_MISMATCH")
    if config.endpoint_host != FORMAL_ENDPOINT_HOST:
        reasons.append("ENDPOINT_MISMATCH")
    if config.manifest_id != manifest.dataset_id:
        reasons.append("MANIFEST_ID_MISMATCH")
    if config.manifest_digest != manifest.manifest_digest:
        reasons.append("MANIFEST_DIGEST_MISMATCH")
    if config.dataset_digest != manifest.dataset_digest:
        reasons.append("DATASET_DIGEST_MISMATCH")
    if config.source_code_digest != manifest.source_code_digest:
        reasons.append("SOURCE_CODE_DIGEST_MISMATCH")
    if config.evidence_analyst_profile_digest != analyst.profile_digest:
        reasons.append("ANALYST_PROFILE_DIGEST_MISMATCH")
    if config.decision_planner_profile_digest != planner.profile_digest:
        reasons.append("PLANNER_PROFILE_DIGEST_MISMATCH")
    if config.smoke_runtime_digest != phase16_smoke_runtime_digest():
        reasons.append("SMOKE_RUNTIME_DIGEST_MISMATCH")
    if official_price.model_id != config.model_id:
        reasons.append("MODEL_ID_MISMATCH")
    if official_price.endpoint_host != config.endpoint_host:
        reasons.append("ENDPOINT_MISMATCH")
    if official_price.official_price_digest != config.official_price_digest:
        reasons.append("OFFICIAL_PRICE_DIGEST_MISMATCH")
    if (
        official_price.input_cny_per_million != INPUT_PRICE_CNY_PER_MILLION
        or official_price.output_cny_per_million != OUTPUT_PRICE_CNY_PER_MILLION
    ):
        reasons.append("OFFICIAL_PRICE_MISMATCH")
    result = _verified_preflight(
        status=Phase16SmokeStatus.BLOCKED if reasons else Phase16SmokeStatus.PASS,
        can_send=not reasons,
        reason_codes=tuple(sorted(set(reasons))),
        config_digest=_config_digest(config),
        scope_id=scope,
        max_smoke_cases=config.max_smoke_cases,
        reserved_case_budget_cny=config.reserved_case_budget_cny,
    )
    # Dataset 不是 HTTP 传入对象，而是同一次 preflight 已重验摘要后的只读运行时依赖。
    # 私有属性不会序列化或成为可伪造的公共发送字段；D-121 的同进程可信边界保持不变。
    object.__setattr__(result, "_dataset", dataset)
    return result


class Phase16SmokeBudgetStore:
    """用锁内事实模拟 PostgreSQL case reservation 的独立内存实现。"""

    def __init__(self, *, scope_id: str = PHASE16_MULTI_AGENT_SMOKE) -> None:
        self._scope_id = _phase16_scope_id(scope_id)
        self._lock = Lock()
        self._records: dict[str, Phase16SmokeBudgetReservation] = {}

    @property
    def scope_id(self) -> str:
        """暴露只读 scope 身份，报告不需了解内部账本容器。"""

        return self._scope_id

    def reserve(self, request_id: str, amount_cny: Decimal) -> Phase16SmokeReservationClaim:
        """在任一 Agent 发送前预约完整 case 上限，相同 case 只恢复既有记录。"""

        if not request_id:
            raise Phase16SmokeBudgetInvariantError("smoke request_id is required")
        amount = _amount(amount_cny)
        with self._lock:
            existing = self._records.get(request_id)
            if existing is not None:
                if existing.reserved_amount_cny != amount:
                    raise Phase16SmokeBudgetInvariantError("conflicting Phase 16 smoke reservation replay")
                return Phase16SmokeReservationClaim(existing, created=False)
            if self._consumed_case_slots() >= PHASE16_SMOKE_MAX_CASES:
                raise Phase16SmokeBudgetLimitExceeded("Phase 16 smoke case limit exceeded")
            if self._exposure() + amount > PHASE16_SMOKE_BUDGET_CNY:
                raise Phase16SmokeBudgetLimitExceeded("Phase 16 smoke budget exceeded")
            now = datetime.now(timezone.utc)
            record = Phase16SmokeBudgetReservation(
                scope_id=self._scope_id,
                reservation_id=_reservation_id(self._scope_id, request_id),
                request_id=request_id,
                reserved_amount_cny=amount,
                state=Phase16SmokeReservationState.RESERVED,
                settled_amount_cny=None,
                usage_known=None,
                outcome_status=None,
                outcome_reason_code=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._records[request_id] = record
            return Phase16SmokeReservationClaim(record, created=True)

    def settle(
        self,
        request_id: str,
        actual_cost_cny: Decimal | None,
        *,
        outcome_status: Phase16SmokeStatus = Phase16SmokeStatus.INCONCLUSIVE,
        outcome_reason_code: str | None = None,
    ) -> Phase16SmokeBudgetReservation:
        """已发送请求按真实 usage 或完整 reservation 结算，保守路径不可释放。"""

        with self._lock:
            record = self._required(request_id)
            _validate_terminal_outcome(
                reservation_state=Phase16SmokeReservationState.SETTLED,
                outcome_status=outcome_status,
            )
            amount = record.reserved_amount_cny if actual_cost_cny is None else _amount(actual_cost_cny, allow_zero=True)
            known = actual_cost_cny is not None
            reason = outcome_reason_code or f"OUTCOME_{outcome_status.value}"
            if amount > record.reserved_amount_cny:
                raise Phase16SmokeBudgetInvariantError("usage cost exceeds Phase 16 case reservation")
            if record.state is Phase16SmokeReservationState.SETTLED:
                if (
                    record.settled_amount_cny != amount
                    or record.usage_known is not known
                    or record.outcome_status is not outcome_status
                    or record.outcome_reason_code != reason
                ):
                    raise Phase16SmokeBudgetInvariantError("conflicting Phase 16 smoke settlement replay")
                return record
            if record.state is not Phase16SmokeReservationState.RESERVED:
                raise Phase16SmokeBudgetInvariantError("Phase 16 smoke reservation is not pending")
            settled = replace(
                record,
                state=Phase16SmokeReservationState.SETTLED,
                settled_amount_cny=amount,
                usage_known=known,
                outcome_status=outcome_status,
                outcome_reason_code=reason,
                version=record.version + 1,
                updated_at=datetime.now(timezone.utc),
            )
            self._records[request_id] = settled
            return settled

    def release(
        self,
        request_id: str,
        *,
        outcome_status: Phase16SmokeStatus = Phase16SmokeStatus.FAIL,
        outcome_reason_code: str | None = None,
    ) -> Phase16SmokeBudgetReservation:
        """仅在 Model Port 明确未发送时释放 reservation，不能释放已结算费用。"""

        with self._lock:
            record = self._required(request_id)
            _validate_terminal_outcome(
                reservation_state=Phase16SmokeReservationState.RELEASED,
                outcome_status=outcome_status,
            )
            reason = outcome_reason_code or f"OUTCOME_{outcome_status.value}"
            if record.state is Phase16SmokeReservationState.RELEASED:
                return record
            if record.state is not Phase16SmokeReservationState.RESERVED:
                raise Phase16SmokeBudgetInvariantError("settled Phase 16 smoke reservation cannot be released")
            released = replace(
                record,
                state=Phase16SmokeReservationState.RELEASED,
                outcome_status=outcome_status,
                outcome_reason_code=reason,
                version=record.version + 1,
                updated_at=datetime.now(timezone.utc),
            )
            self._records[request_id] = released
            return released

    def snapshot(self) -> Phase16SmokeBudgetSnapshot:
        """从 reservation 事实重算 exposure，任何释放记录都不再占用额度。"""

        with self._lock:
            reserved = sum(
                (item.reserved_amount_cny for item in self._records.values() if item.state is Phase16SmokeReservationState.RESERVED),
                Decimal("0"),
            )
            committed = sum(
                (item.settled_amount_cny or Decimal("0") for item in self._records.values() if item.state is Phase16SmokeReservationState.SETTLED),
                Decimal("0"),
            )
            return Phase16SmokeBudgetSnapshot(
                scope_id=self._scope_id,
                reserved_cny=reserved,
                committed_cny=committed,
                available_cny=max(PHASE16_SMOKE_BUDGET_CNY - reserved - committed, Decimal("0")),
            )

    def _required(self, request_id: str) -> Phase16SmokeBudgetReservation:
        try:
            return self._records[request_id]
        except KeyError as error:
            raise Phase16SmokeBudgetInvariantError("unknown Phase 16 smoke reservation") from error

    def _exposure(self) -> Decimal:
        return sum(
            (
                item.reserved_amount_cny
                if item.state is Phase16SmokeReservationState.RESERVED
                else item.settled_amount_cny or Decimal("0")
            )
            for item in self._records.values()
            if item.state is not Phase16SmokeReservationState.RELEASED
        )

    def _consumed_case_slots(self) -> int:
        """已预约或已结算的 case 都占用十例 slot，低实际价格不能释放样本上限。"""

        return sum(
            item.state is not Phase16SmokeReservationState.RELEASED
            for item in self._records.values()
        )


class PostgresPhase16SmokeBudgetStore:
    """用 Phase 16 专属表实现跨进程 reservation、行锁和重启恢复。"""

    def __init__(self, settings: Any, *, scope_id: str = PHASE16_MULTI_AGENT_SMOKE) -> None:
        self._settings = settings
        self._scope_id = _phase16_scope_id(scope_id)
        self._ensure_scope()

    @property
    def scope_id(self) -> str:
        """返回 PostgreSQL ledger 的只读 scope。"""

        return self._scope_id

    def reserve(self, request_id: str, amount_cny: Decimal) -> Phase16SmokeReservationClaim:
        """先锁 ledger 行再计算 exposure，两个进程不能同时写穿一元上限。"""

        if not request_id:
            raise Phase16SmokeBudgetInvariantError("smoke request_id is required")
        amount = _amount(amount_cny)
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM phase16_smoke_budget_ledgers WHERE scope_id=%s FOR UPDATE;",
                    (self._scope_id,),
                )
                ledger = cursor.fetchone()
                cursor.execute(
                    "SELECT * FROM phase16_smoke_budget_reservations WHERE scope_id=%s AND request_id=%s FOR UPDATE;",
                    (self._scope_id, request_id),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    loaded = self._from_row(existing)
                    if loaded.reserved_amount_cny != amount:
                        raise Phase16SmokeBudgetInvariantError("conflicting Phase 16 smoke reservation replay")
                    return Phase16SmokeReservationClaim(loaded, created=False)
                cursor.execute(
                    "SELECT count(*) FILTER (WHERE state <> 'RELEASED') AS case_slots, COALESCE(sum(CASE WHEN state='RESERVED' THEN reserved_amount_cny WHEN state='SETTLED' THEN settled_amount_cny ELSE 0 END),0) AS exposure FROM phase16_smoke_budget_reservations WHERE scope_id=%s;",
                    (self._scope_id,),
                )
                budget_state = cursor.fetchone()
                if int(budget_state["case_slots"]) >= PHASE16_SMOKE_MAX_CASES:
                    raise Phase16SmokeBudgetLimitExceeded("Phase 16 smoke case limit exceeded")
                exposure = Decimal(budget_state["exposure"])
                if exposure + amount > Decimal(ledger["limit_cny"]):
                    raise Phase16SmokeBudgetLimitExceeded("Phase 16 smoke budget exceeded")
                cursor.execute(
                    "INSERT INTO phase16_smoke_budget_reservations (reservation_id, scope_id, request_id, reserved_amount_cny, state, version) VALUES (%s::uuid,%s,%s,%s,'RESERVED',1) RETURNING *;",
                    (_reservation_id(self._scope_id, request_id), self._scope_id, request_id, amount),
                )
                row = cursor.fetchone()
            conn.commit()
        return Phase16SmokeReservationClaim(self._from_row(row), created=True)

    def settle(
        self,
        request_id: str,
        actual_cost_cny: Decimal | None,
        *,
        outcome_status: Phase16SmokeStatus = Phase16SmokeStatus.INCONCLUSIVE,
        outcome_reason_code: str | None = None,
    ) -> Phase16SmokeBudgetReservation:
        """在行锁内结算已发送 case，usage 缺失保持完整 reservation 的保守成本。"""

        amount = None if actual_cost_cny is None else _amount(actual_cost_cny, allow_zero=True)
        _validate_terminal_outcome(
            reservation_state=Phase16SmokeReservationState.SETTLED,
            outcome_status=outcome_status,
        )
        reason = outcome_reason_code or f"OUTCOME_{outcome_status.value}"
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM phase16_smoke_budget_reservations WHERE scope_id=%s AND request_id=%s FOR UPDATE;",
                    (self._scope_id, request_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise Phase16SmokeBudgetInvariantError("unknown Phase 16 smoke reservation")
                current = self._from_row(row)
                settled = current.reserved_amount_cny if amount is None else amount
                known = amount is not None
                if settled > current.reserved_amount_cny:
                    raise Phase16SmokeBudgetInvariantError("usage cost exceeds Phase 16 case reservation")
                if current.state is Phase16SmokeReservationState.SETTLED:
                    if (
                        current.settled_amount_cny != settled
                        or current.usage_known is not known
                        or current.outcome_status is not outcome_status
                        or current.outcome_reason_code != reason
                    ):
                        raise Phase16SmokeBudgetInvariantError("conflicting Phase 16 smoke settlement replay")
                    return current
                if current.state is not Phase16SmokeReservationState.RESERVED:
                    raise Phase16SmokeBudgetInvariantError("Phase 16 smoke reservation is not pending")
                cursor.execute(
                    "UPDATE phase16_smoke_budget_reservations SET state='SETTLED', settled_amount_cny=%s, usage_known=%s, outcome_status=%s, outcome_reason_code=%s, version=version+1, updated_at=now() WHERE scope_id=%s AND request_id=%s RETURNING *;",
                    (settled, known, outcome_status.value, reason, self._scope_id, request_id),
                )
                row = cursor.fetchone()
            conn.commit()
        return self._from_row(row)

    def release(
        self,
        request_id: str,
        *,
        outcome_status: Phase16SmokeStatus = Phase16SmokeStatus.FAIL,
        outcome_reason_code: str | None = None,
    ) -> Phase16SmokeBudgetReservation:
        """仅关闭确认未发送的 reservation，数据库重启后规则仍保持相同。"""

        reason = outcome_reason_code or f"OUTCOME_{outcome_status.value}"
        _validate_terminal_outcome(
            reservation_state=Phase16SmokeReservationState.RELEASED,
            outcome_status=outcome_status,
        )
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM phase16_smoke_budget_reservations WHERE scope_id=%s AND request_id=%s FOR UPDATE;",
                    (self._scope_id, request_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise Phase16SmokeBudgetInvariantError("unknown Phase 16 smoke reservation")
                current = self._from_row(row)
                if current.state is Phase16SmokeReservationState.RELEASED:
                    return current
                if current.state is not Phase16SmokeReservationState.RESERVED:
                    raise Phase16SmokeBudgetInvariantError("settled Phase 16 smoke reservation cannot be released")
                cursor.execute(
                    "UPDATE phase16_smoke_budget_reservations SET state='RELEASED', outcome_status=%s, outcome_reason_code=%s, version=version+1, updated_at=now() WHERE scope_id=%s AND request_id=%s RETURNING *;",
                    (outcome_status.value, reason, self._scope_id, request_id),
                )
                row = cursor.fetchone()
            conn.commit()
        return self._from_row(row)

    def snapshot(self) -> Phase16SmokeBudgetSnapshot:
        """从持久化 reservation 重建余额，避免依赖进程内累计计数。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT limit_cny FROM phase16_smoke_budget_ledgers WHERE scope_id=%s;", (self._scope_id,))
                limit = Decimal(cursor.fetchone()["limit_cny"])
                cursor.execute(
                    "SELECT COALESCE(sum(reserved_amount_cny) FILTER (WHERE state='RESERVED'),0) AS reserved, COALESCE(sum(settled_amount_cny) FILTER (WHERE state='SETTLED'),0) AS committed FROM phase16_smoke_budget_reservations WHERE scope_id=%s;",
                    (self._scope_id,),
                )
                row = cursor.fetchone()
        reserved = Decimal(row["reserved"])
        committed = Decimal(row["committed"])
        return Phase16SmokeBudgetSnapshot(self._scope_id, reserved, committed, max(limit - reserved - committed, Decimal("0")))

    def _ensure_scope(self) -> None:
        """幂等创建当前 scope；表本身由统一迁移入口负责创建。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO phase16_smoke_budget_ledgers (scope_id, limit_cny) VALUES (%s,%s) ON CONFLICT (scope_id) DO NOTHING;",
                    (self._scope_id, PHASE16_SMOKE_BUDGET_CNY),
                )
            conn.commit()

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> Phase16SmokeBudgetReservation:
        """显式还原 Decimal、枚举和时态字段，拒绝数据库驱动的隐式语义差异。"""

        return Phase16SmokeBudgetReservation(
            scope_id=row["scope_id"],
            reservation_id=str(row["reservation_id"]),
            request_id=row["request_id"],
            reserved_amount_cny=Decimal(row["reserved_amount_cny"]),
            state=Phase16SmokeReservationState(row["state"]),
            settled_amount_cny=None if row["settled_amount_cny"] is None else Decimal(row["settled_amount_cny"]),
            usage_known=row["usage_known"],
            outcome_status=None if row["outcome_status"] is None else Phase16SmokeStatus(row["outcome_status"]),
            outcome_reason_code=row["outcome_reason_code"],
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def initialize_phase16_smoke_budget_schema(settings: Any) -> None:
    """执行独立 Phase 16 smoke DDL，测试和部署都走同一个 UTF-8 文件。"""

    sql_path = Path(__file__).resolve().parents[2] / "docker" / "init_phase16_smoke.sql"
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql_path.read_text(encoding="utf-8"))
        conn.commit()


def _usage_cost(usage: ModelUsage) -> Decimal:
    """按已冻结 cache-miss 官方价格保守计算 token 成本并量化为六位小数。"""

    # ModelUsage 只提供总输入/输出 token，不能证明任意输入命中了供应商 cache；因此所有
    # input token 均按公开 cache-miss 输入价结算。该规则可能高估费用，但不会低估或借此
    # 放宽一元上限；将来若供应商提供可验证的 cache usage，必须以新决策扩展账本证据。
    raw = (
        Decimal(usage.input_tokens) * INPUT_PRICE_CNY_PER_MILLION
        + Decimal(usage.output_tokens) * OUTPUT_PRICE_CNY_PER_MILLION
    ) / Decimal("1000000")
    return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


class Phase16SmokeRunner:
    """在可信预检后对每个高冲突 case 依次发送 Analyst 和 Planner 的单次 smoke。"""

    def __init__(
        self,
        *,
        config: Phase16SmokeConfig,
        preflight: Phase16SmokePreflight,
        budget_store: Phase16SmokeBudgetStore | PostgresPhase16SmokeBudgetStore,
        model_port: AgentModelPort,
    ) -> None:
        self._config = config
        self._preflight = preflight
        self._budget = budget_store
        self._model_port = model_port

    async def run(self, case_ids: tuple[str, ...]) -> Phase16SmokeReport:
        """按 case 级 reservation 执行；此处不读取密钥、不 fallback、不产生业务 Proposal。"""

        if not self._preflight.provenance_verified or not self._preflight.can_send:
            return self._report(
                Phase16SmokeStatus.BLOCKED,
                ("PHASE16_SMOKE_PREFLIGHT_REQUIRED", *self._preflight.reason_codes),
            )
        if self._preflight.config_digest != _config_digest(self._config):
            return self._report(Phase16SmokeStatus.BLOCKED, ("PREFLIGHT_CONFIG_MISMATCH",))
        if self._preflight.scope_id != self._budget.scope_id:
            return self._report(Phase16SmokeStatus.BLOCKED, ("PREFLIGHT_SCOPE_MISMATCH",))
        if not 1 <= len(case_ids) <= self._config.max_smoke_cases:
            raise ValueError("Phase 16 smoke case count must be between 1 and 10")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("Phase 16 smoke case IDs must be unique")

        dataset = self._dataset_from_preflight()
        try:
            # 预检签发到真正调用 Port 之间仍可能存在同进程可变嵌套对象；因此每轮发送
            # 前重验 Task 9 资产，阻断任何未绑定 Manifest 的事实进入真实模型正文。
            _validate_dataset_for_run(dataset)
        except (OSError, UnicodeError, ValueError):
            return self._report(Phase16SmokeStatus.BLOCKED, ("TASK9_DATASET_INVALID",))
        smoke_ids = set(dataset.manifest.smoke_eligible_case_ids)
        if not set(case_ids).issubset(smoke_ids):
            raise ValueError("all Phase 16 smoke cases must be Manifest smoke eligible")

        reasons: list[str] = []
        request_count = 0
        unknown_usage_cases = 0
        settled_cost = Decimal("0")
        replayed = 0
        cases_by_id = {case.case_id: case for case in dataset.cases}
        for case_id in case_ids:
            case = cases_by_id[case_id]
            request_key = f"{self._config.manifest_digest}:{case_id}"
            try:
                claim = self._budget.reserve(request_key, self._config.reserved_case_budget_cny)
            except Phase16SmokeBudgetLimitExceeded:
                reasons.append("PHASE16_SMOKE_BUDGET_EXCEEDED")
                return self._report(
                    Phase16SmokeStatus.BLOCKED,
                    reasons,
                    smoke_case_count=len(case_ids),
                    model_request_count=request_count,
                    unknown_usage_case_count=unknown_usage_cases,
                    settled_cost_cny=settled_cost,
                    replayed_case_count=replayed,
                )
            if not claim.created:
                # 相同 case 的重启重放绝不能再次发送。预算 reservation 同时保存最终技术
                # 结论，不能因为 state=SETTLED 就把 usage 不明或 Planner 失败伪装成 PASS。
                if claim.record.state in {
                    Phase16SmokeReservationState.SETTLED,
                    Phase16SmokeReservationState.RELEASED,
                }:
                    replayed += 1
                    settled_cost += claim.record.settled_amount_cny or Decimal("0")
                    stored_status = claim.record.outcome_status or Phase16SmokeStatus.INCONCLUSIVE
                    if claim.record.outcome_reason_code:
                        reasons.append(claim.record.outcome_reason_code)
                    if stored_status is Phase16SmokeStatus.PASS:
                        continue
                    return self._report(
                        stored_status,
                        reasons,
                        smoke_case_count=len(case_ids),
                        model_request_count=request_count,
                        unknown_usage_case_count=(
                            unknown_usage_cases
                            + int(stored_status is Phase16SmokeStatus.INCONCLUSIVE)
                        ),
                        settled_cost_cny=settled_cost,
                        replayed_case_count=replayed,
                    )
                reasons.append("PENDING_SMOKE_RESERVATION_REQUIRES_MANUAL_RECOVERY")
                return self._report(
                    Phase16SmokeStatus.BLOCKED,
                    reasons,
                    smoke_case_count=len(case_ids),
                    model_request_count=request_count,
                    unknown_usage_case_count=unknown_usage_cases,
                    settled_cost_cny=settled_cost,
                    replayed_case_count=replayed,
                )

            case_status, case_requests, case_cost, case_reason = await self._run_case(case, request_key)
            request_count += case_requests
            settled_cost += case_cost
            if case_reason:
                reasons.append(case_reason)
            if case_status is Phase16SmokeStatus.INCONCLUSIVE:
                unknown_usage_cases += 1
                return self._report(
                    Phase16SmokeStatus.INCONCLUSIVE,
                    reasons,
                    smoke_case_count=len(case_ids),
                    model_request_count=request_count,
                    unknown_usage_case_count=unknown_usage_cases,
                    settled_cost_cny=settled_cost,
                    replayed_case_count=replayed,
                )
            if case_status is Phase16SmokeStatus.FAIL:
                return self._report(
                    Phase16SmokeStatus.FAIL,
                    reasons,
                    smoke_case_count=len(case_ids),
                    model_request_count=request_count,
                    unknown_usage_case_count=unknown_usage_cases,
                    settled_cost_cny=settled_cost,
                    replayed_case_count=replayed,
                )
        return self._report(
            Phase16SmokeStatus.PASS,
            reasons,
            smoke_case_count=len(case_ids),
            model_request_count=request_count,
            unknown_usage_case_count=unknown_usage_cases,
            settled_cost_cny=settled_cost,
            replayed_case_count=replayed,
        )

    def _dataset_from_preflight(self) -> Phase16EvaluationDataset:
        """预检 Runner 只使用预验证的 Task 9 数据集，不允许调用者替换为自由 case。"""

        # Config 绑定 Manifest 摘要，真实数据集本身由调用 preflight 的上层加载器验证。为保持
        # Runner 无全局磁盘依赖，本任务将 dataset 缓存在可信 preflight 闭包中，由以下私有属性读取。
        dataset = self._preflight._dataset
        if dataset is None:
            raise Phase16SmokeBudgetInvariantError("verified preflight dataset is unavailable")
        return dataset

    async def _run_case(
        self,
        case: Phase16EvaluationCase,
        request_key: str,
    ) -> tuple[Phase16SmokeStatus, int, Decimal, str | None]:
        """先 Analyst 再 Planner；任一已发送但不可计价的调用立刻保守封闭整个 case。"""

        accumulated_cost = Decimal("0")
        requests = 0
        for profile, stage in (
            (build_evidence_analyst_profile(), "CONFLICT_ANALYSIS"),
            (build_decision_planner_profile(), "LIVE_DECISION_PLANNING"),
        ):
            request = self._request_for(profile, case, request_key, stage)
            requests += 1
            try:
                outcome = await self._model_port.complete(request)
            except Exception:  # noqa: BLE001 - 一旦已进入 Port，发送状态未知必须保守结算。
                settled = self._budget.settle(
                    request_key,
                    None,
                    outcome_status=Phase16SmokeStatus.INCONCLUSIVE,
                    outcome_reason_code="MODEL_PORT_EXCEPTION_USAGE_UNKNOWN",
                )
                return Phase16SmokeStatus.INCONCLUSIVE, requests, settled.settled_amount_cny or Decimal("0"), "MODEL_PORT_EXCEPTION_USAGE_UNKNOWN"
            if isinstance(outcome, ModelFailure):
                if outcome.request_sent:
                    settled = self._budget.settle(
                        request_key,
                        None,
                        outcome_status=Phase16SmokeStatus.INCONCLUSIVE,
                        outcome_reason_code="MODEL_FAILURE_USAGE_UNKNOWN",
                    )
                    return Phase16SmokeStatus.INCONCLUSIVE, requests, settled.settled_amount_cny or Decimal("0"), "MODEL_FAILURE_USAGE_UNKNOWN"
                if requests == 1:
                    # Analyst 尚未发送时整例没有外部成本，允许释放并由后续显式 case 重新预约。
                    self._budget.release(
                        request_key,
                        outcome_status=Phase16SmokeStatus.FAIL,
                        outcome_reason_code="MODEL_REQUEST_NOT_SENT",
                    )
                    return Phase16SmokeStatus.FAIL, requests, Decimal("0"), "MODEL_REQUEST_NOT_SENT"
                # Planner 未发送不等于 Analyst 从未发送。必须保留已知 Analyst 成本和 case slot，
                # 否则一次真实调用可被 release 回滚并绕过预算或十例限制。
                settled = self._budget.settle(
                    request_key,
                    accumulated_cost,
                    outcome_status=Phase16SmokeStatus.FAIL,
                    outcome_reason_code="PLANNER_REQUEST_NOT_SENT_AFTER_ANALYST",
                )
                return Phase16SmokeStatus.FAIL, requests, settled.settled_amount_cny or Decimal("0"), "PLANNER_REQUEST_NOT_SENT_AFTER_ANALYST"
            if not isinstance(outcome, ModelSuccess) or outcome.request_id != request.request_id or outcome.model_id != request.model_id:
                # 不可信身份与 usage 一样不能安全区分是否已经造成外部成本。
                settled = self._budget.settle(
                    request_key,
                    None,
                    outcome_status=Phase16SmokeStatus.INCONCLUSIVE,
                    outcome_reason_code="MODEL_IDENTITY_OR_RESPONSE_MISMATCH",
                )
                return Phase16SmokeStatus.INCONCLUSIVE, requests, settled.settled_amount_cny or Decimal("0"), "MODEL_IDENTITY_OR_RESPONSE_MISMATCH"
            if outcome.usage is None:
                settled = self._budget.settle(
                    request_key,
                    None,
                    outcome_status=Phase16SmokeStatus.INCONCLUSIVE,
                    outcome_reason_code="USAGE_UNKNOWN_SETTLED_AT_RESERVATION",
                )
                return Phase16SmokeStatus.INCONCLUSIVE, requests, settled.settled_amount_cny or Decimal("0"), "USAGE_UNKNOWN_SETTLED_AT_RESERVATION"
            accumulated_cost += _usage_cost(outcome.usage)
            if accumulated_cost > self._config.reserved_case_budget_cny:
                settled = self._budget.settle(
                    request_key,
                    None,
                    outcome_status=Phase16SmokeStatus.INCONCLUSIVE,
                    outcome_reason_code="USAGE_EXCEEDS_CASE_RESERVATION",
                )
                return Phase16SmokeStatus.INCONCLUSIVE, requests, settled.settled_amount_cny or Decimal("0"), "USAGE_EXCEEDS_CASE_RESERVATION"
        settled = self._budget.settle(
            request_key,
            accumulated_cost,
            outcome_status=Phase16SmokeStatus.PASS,
        )
        return Phase16SmokeStatus.PASS, requests, settled.settled_amount_cny or Decimal("0"), None

    def _request_for(
        self,
        profile: SpecialistProfile,
        case: Phase16EvaluationCase,
        request_key: str,
        stage: str,
    ) -> ModelRequest:
        """用冻结 Profile 生成一条无标签、无业务写权限的最小 smoke 请求。"""

        # case_id/split/kind 是评估元数据，不能进入真实模型正文；只传 case 内容摘要和
        # 业务事实，使 Task 10 的外部请求与 Task 9 的元数据隔离原则一致。
        content = json.dumps(
            {
                "case_digest": sha256(
                    json.dumps(case.input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "facts": case.input,
                "stage": stage,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_id = sha256(f"{request_key}\x1f{profile.profile_id}".encode("utf-8")).hexdigest()
        return ModelRequest(
            request_id=request_id,
            endpoint_host=profile.endpoint_host,
            model_id=profile.model_id,
            temperature=profile.temperature,
            prompt_hash=profile.prompt_hash,
            result_schema_hash=profile.result_schema_hash,
            messages=(
                ModelMessage(role="system", content=profile.prompt_text),
                ModelMessage(role="user", content=content),
            ),
            max_output_tokens=profile.max_total_tokens,
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=profile.deadline_seconds),
        )

    def _report(
        self,
        status: Phase16SmokeStatus,
        reason_codes: list[str] | tuple[str, ...],
        *,
        smoke_case_count: int = 0,
        model_request_count: int = 0,
        unknown_usage_case_count: int = 0,
        settled_cost_cny: Decimal = Decimal("0"),
        replayed_case_count: int = 0,
    ) -> Phase16SmokeReport:
        """统一归一稳定 reason code，避免向报告泄漏异常正文或模型自由内容。"""

        return Phase16SmokeReport(
            status=status,
            scope_id=self._budget.scope_id,
            reason_codes=tuple(sorted(set(reason_codes))),
            smoke_case_count=smoke_case_count,
            model_request_count=model_request_count,
            unknown_usage_case_count=unknown_usage_case_count,
            settled_cost_cny=settled_cost_cny,
            replayed_case_count=replayed_case_count,
        )
