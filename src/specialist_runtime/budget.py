"""Phase 13/14 共享的持久模型预算账本。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg.rows import dict_row


TOTAL_LIMIT_CNY = Decimal("3.00")
PHASE13_LIMIT_CNY = Decimal("2.40")
PHASE14_RESERVED_CNY = Decimal("0.60")


class BudgetCandidate(StrEnum):
    """Phase 13 三个独立候选的预算身份。"""

    LIVE_OPS = "LIVE_OPS"
    PLANNER = "PLANNER"
    REVIEW_MEMORY = "REVIEW_MEMORY"


CANDIDATE_LIMITS: Mapping[BudgetCandidate, Decimal] = MappingProxyType(
    {
        BudgetCandidate.LIVE_OPS: Decimal("0.60"),
        BudgetCandidate.PLANNER: Decimal("1.00"),
        BudgetCandidate.REVIEW_MEMORY: Decimal("0.80"),
    }
)


class ReservationState(StrEnum):
    """模型费用预留的不可逆状态。"""

    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class BudgetInvariantError(RuntimeError):
    """同一请求重放事实冲突或非法状态转换。"""


class BudgetLimitExceeded(RuntimeError):
    """预留会突破候选或阶段硬上限。"""


@dataclass(frozen=True)
class BudgetReservation:
    """一次模型请求在发送前持久化的费用上限。"""

    scope_id: str
    reservation_id: str
    request_id: str
    candidate: BudgetCandidate
    reserved_amount_cny: Decimal
    state: ReservationState
    settled_amount_cny: Decimal | None
    usage_known: bool | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ReservationClaim:
    """区分首次预留与幂等恢复重放。"""

    record: BudgetReservation
    created: bool


@dataclass(frozen=True)
class BudgetSnapshot:
    """面向审计的阶段预算只读汇总。"""

    scope_id: str
    total_limit_cny: Decimal
    phase13_limit_cny: Decimal
    phase14_reserved_cny: Decimal
    candidate_limits: Mapping[BudgetCandidate, Decimal]
    phase13_reserved_cny: Decimal
    phase13_committed_cny: Decimal
    phase14_available_cny: Decimal


def _reservation_id(scope_id: str, request_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{scope_id}\x1f{request_id}"))


def _validate_amount(value: Decimal, *, allow_zero: bool = False) -> Decimal:
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise BudgetInvariantError("budget amount must be finite and positive")
    if amount >= Decimal("1000000"):
        raise BudgetInvariantError("budget amount exceeds ledger range")
    try:
        quantized = amount.quantize(Decimal("0.000001"))
    except InvalidOperation as error:
        raise BudgetInvariantError("budget amount exceeds ledger range") from error
    if amount != quantized:
        raise BudgetInvariantError("budget amount exceeds ledger precision")
    return amount


class InMemoryModelBudgetStore:
    """使用单锁模拟生产 Ledger 行锁的确定性内存实现。"""

    def __init__(self, *, scope_id: str = "agent-runtime-completion-v1") -> None:
        self._scope_id = scope_id
        self._lock = Lock()
        self._records: dict[str, BudgetReservation] = {}
        self._released_candidates: set[BudgetCandidate] = set()

    def reserve(
        self,
        request_id: str,
        candidate: BudgetCandidate,
        amount_cny: Decimal,
    ) -> ReservationClaim:
        amount = _validate_amount(amount_cny)
        with self._lock:
            existing = self._records.get(request_id)
            if existing is not None:
                if existing.candidate is not candidate or existing.reserved_amount_cny != amount:
                    raise BudgetInvariantError("conflicting reservation replay")
                return ReservationClaim(existing, created=False)
            self._assert_capacity(candidate, amount)
            now = datetime.now(timezone.utc)
            record = BudgetReservation(
                scope_id=self._scope_id,
                reservation_id=_reservation_id(self._scope_id, request_id),
                request_id=request_id,
                candidate=candidate,
                reserved_amount_cny=amount,
                state=ReservationState.RESERVED,
                settled_amount_cny=None,
                usage_known=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._records[request_id] = record
            return ReservationClaim(record, created=True)

    def settle(
        self,
        request_id: str,
        actual_cost_cny: Decimal | None,
    ) -> BudgetReservation:
        with self._lock:
            record = self._required(request_id)
            amount = (
                record.reserved_amount_cny
                if actual_cost_cny is None
                else _validate_amount(actual_cost_cny, allow_zero=True)
            )
            # 价格策略异常或上游计量漂移不能改变“费用已经发生”的事实。即使实际费用
            # 超过预留，也必须如实提交到账本，使后续 reserve 看到真实暴露并 fail-closed。
            if record.state is ReservationState.SETTLED:
                if record.settled_amount_cny != amount or record.usage_known is not (actual_cost_cny is not None):
                    raise BudgetInvariantError("conflicting settlement replay")
                return record
            if record.state is not ReservationState.RESERVED:
                raise BudgetInvariantError("reservation is not awaiting settlement")
            settled = replace(
                record,
                state=ReservationState.SETTLED,
                settled_amount_cny=amount,
                usage_known=actual_cost_cny is not None,
                version=record.version + 1,
                updated_at=datetime.now(timezone.utc),
            )
            self._records[request_id] = settled
            return settled

    def release(self, request_id: str) -> BudgetReservation:
        with self._lock:
            record = self._required(request_id)
            if record.state is ReservationState.RELEASED:
                return record
            if record.state is not ReservationState.RESERVED:
                raise BudgetInvariantError("settled reservation cannot be released")
            released = replace(
                record,
                state=ReservationState.RELEASED,
                version=record.version + 1,
                updated_at=datetime.now(timezone.utc),
            )
            self._records[request_id] = released
            return released

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            reserved, committed = self._totals()
            return BudgetSnapshot(
                scope_id=self._scope_id,
                total_limit_cny=TOTAL_LIMIT_CNY,
                phase13_limit_cny=PHASE13_LIMIT_CNY,
                phase14_reserved_cny=PHASE14_RESERVED_CNY,
                candidate_limits=CANDIDATE_LIMITS,
                phase13_reserved_cny=reserved,
                phase13_committed_cny=committed,
                phase14_available_cny=PHASE14_RESERVED_CNY,
            )

    def list_pending_reservations(self) -> tuple[BudgetReservation, ...]:
        """按稳定 request ID 返回全部待结算记录，供崩溃恢复扫描。"""

        with self._lock:
            return tuple(
                sorted(
                    (r for r in self._records.values() if r.state is ReservationState.RESERVED),
                    key=lambda r: r.request_id,
                )
            )

    def release_candidate_allowance(self, candidate: BudgetCandidate) -> None:
        """候选提前拒绝后，把未消费初始额度转入共享池并永久关闭候选。"""

        with self._lock:
            if any(r.candidate is candidate and r.state is ReservationState.RESERVED for r in self._records.values()):
                raise BudgetInvariantError("candidate has pending reservations")
            self._released_candidates.add(candidate)

    def _required(self, request_id: str) -> BudgetReservation:
        try:
            return self._records[request_id]
        except KeyError as error:
            raise BudgetInvariantError("unknown reservation") from error

    def _totals(self) -> tuple[Decimal, Decimal]:
        reserved = sum(
            (r.reserved_amount_cny for r in self._records.values() if r.state is ReservationState.RESERVED),
            Decimal("0"),
        )
        committed = sum(
            (r.settled_amount_cny or Decimal("0") for r in self._records.values() if r.state is ReservationState.SETTLED),
            Decimal("0"),
        )
        return reserved, committed

    def _assert_capacity(self, candidate: BudgetCandidate, amount: Decimal) -> None:
        if candidate in self._released_candidates:
            raise BudgetInvariantError("candidate allowance is released")
        reserved, committed = self._totals()
        if reserved + committed + amount > PHASE13_LIMIT_CNY:
            raise BudgetLimitExceeded("phase budget exceeded")
        candidate_total = sum(
            (
                r.reserved_amount_cny
                if r.state is ReservationState.RESERVED
                else r.settled_amount_cny or Decimal("0")
            )
            for r in self._records.values()
            if r.candidate is candidate and r.state is not ReservationState.RELEASED
        )
        extra_needed = max(candidate_total + amount - CANDIDATE_LIMITS[candidate], Decimal("0"))
        shared_available = sum(
            (
                CANDIDATE_LIMITS[item]
                - sum(
                    (r.settled_amount_cny or Decimal("0"))
                    for r in self._records.values()
                    if r.candidate is item and r.state is ReservationState.SETTLED
                )
            )
            for item in self._released_candidates
        )
        existing_extra = sum(
            max(
                sum(
                    (
                        r.reserved_amount_cny
                        if r.state is ReservationState.RESERVED
                        else r.settled_amount_cny or Decimal("0")
                    )
                    for r in self._records.values()
                    if r.candidate is item and r.state is not ReservationState.RELEASED
                )
                - CANDIDATE_LIMITS[item],
                Decimal("0"),
            )
            for item in BudgetCandidate
            if item not in self._released_candidates
        )
        current_extra = max(candidate_total - CANDIDATE_LIMITS[candidate], Decimal("0"))
        extra_increment = extra_needed - current_extra
        if existing_extra + extra_increment > shared_available:
            raise BudgetLimitExceeded("candidate shared budget exceeded")


class PostgresModelBudgetStore:
    """以 scope Ledger ``FOR UPDATE`` 串行化跨进程预算预留。"""

    def __init__(self, settings: Any, *, scope_id: str = "agent-runtime-completion-v1") -> None:
        self._settings = settings
        self._scope_id = scope_id
        self._ensure_scope()

    def reserve(self, request_id: str, candidate: BudgetCandidate, amount_cny: Decimal) -> ReservationClaim:
        amount = _validate_amount(amount_cny)
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                policy = self._lock_scope(cursor)
                existing = self._load(cursor, request_id, for_update=True)
                if existing is not None:
                    if existing.candidate is not candidate or existing.reserved_amount_cny != amount:
                        raise BudgetInvariantError("conflicting reservation replay")
                    return ReservationClaim(existing, created=False)
                self._assert_sql_capacity(cursor, candidate, amount, policy)
                reservation_id = _reservation_id(self._scope_id, request_id)
                cursor.execute(
                    """
                    INSERT INTO specialist_model_budget_reservations (
                        reservation_id, scope_id, request_id, candidate_id,
                        reserved_amount_cny, state, version
                    ) VALUES (%s::uuid, %s, %s, %s, %s, 'RESERVED', 1)
                    RETURNING *;
                    """,
                    (reservation_id, self._scope_id, request_id, candidate.value, amount),
                )
                row = cursor.fetchone()
            conn.commit()
        assert row is not None
        return ReservationClaim(self._from_row(row), created=True)

    def settle(self, request_id: str, actual_cost_cny: Decimal | None) -> BudgetReservation:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                self._lock_scope(cursor)
                record = self._load(cursor, request_id, for_update=True)
                if record is None:
                    raise BudgetInvariantError("unknown reservation")
                amount = record.reserved_amount_cny if actual_cost_cny is None else _validate_amount(actual_cost_cny, allow_zero=True)
                usage_known = actual_cost_cny is not None
                # 已知实际费用允许高于预留：这是需要阻断后续调用的预算事故事实，
                # 不能因写入较低预留值而把超额消费重新释放成可用额度。
                if record.state is ReservationState.SETTLED:
                    if record.settled_amount_cny != amount or record.usage_known is not usage_known:
                        raise BudgetInvariantError("conflicting settlement replay")
                    return record
                if record.state is not ReservationState.RESERVED:
                    raise BudgetInvariantError("reservation is not awaiting settlement")
                cursor.execute(
                    """
                    UPDATE specialist_model_budget_reservations
                    SET state='SETTLED', settled_amount_cny=%s, usage_known=%s,
                        version=version+1, updated_at=now()
                    WHERE scope_id=%s AND request_id=%s AND state='RESERVED'
                    RETURNING *;
                    """,
                    (amount, usage_known, self._scope_id, request_id),
                )
                row = cursor.fetchone()
                cursor.execute(
                    """
                    INSERT INTO specialist_model_calls (
                        call_id, scope_id, request_id, reservation_state, settled_amount_cny, usage_known
                    ) VALUES (%s::uuid, %s, %s, 'SETTLED', %s, %s)
                    ON CONFLICT (scope_id, request_id) DO NOTHING;
                    """,
                    (_reservation_id(self._scope_id, f"call:{request_id}"), self._scope_id, request_id, amount, usage_known),
                )
            conn.commit()
        assert row is not None
        return self._from_row(row)

    def release(self, request_id: str) -> BudgetReservation:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                self._lock_scope(cursor)
                record = self._load(cursor, request_id, for_update=True)
                if record is None:
                    raise BudgetInvariantError("unknown reservation")
                if record.state is ReservationState.RELEASED:
                    return record
                if record.state is not ReservationState.RESERVED:
                    raise BudgetInvariantError("settled reservation cannot be released")
                cursor.execute(
                    """
                    UPDATE specialist_model_budget_reservations
                    SET state='RELEASED', version=version+1, updated_at=now()
                    WHERE scope_id=%s AND request_id=%s AND state='RESERVED'
                    RETURNING *;
                    """,
                    (self._scope_id, request_id),
                )
                row = cursor.fetchone()
            conn.commit()
        assert row is not None
        return self._from_row(row)

    def snapshot(self) -> BudgetSnapshot:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM specialist_model_budget_ledgers WHERE scope_id=%s;", (self._scope_id,))
                ledger = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT
                        COALESCE(sum(reserved_amount_cny) FILTER (WHERE state='RESERVED'), 0) AS reserved,
                        COALESCE(sum(settled_amount_cny) FILTER (WHERE state='SETTLED'), 0) AS committed
                    FROM specialist_model_budget_reservations WHERE scope_id=%s;
                    """,
                    (self._scope_id,),
                )
                row = cursor.fetchone()
                cursor.execute(
                    "SELECT candidate_id, initial_limit_cny FROM specialist_model_budget_candidates WHERE scope_id=%s;",
                    (self._scope_id,),
                )
                limits = {BudgetCandidate(item["candidate_id"]): Decimal(item["initial_limit_cny"]) for item in cursor.fetchall()}
        assert row is not None
        assert ledger is not None
        return BudgetSnapshot(
            scope_id=self._scope_id,
            total_limit_cny=Decimal(ledger["total_limit_cny"]),
            phase13_limit_cny=Decimal(ledger["phase13_limit_cny"]),
            phase14_reserved_cny=Decimal(ledger["phase14_reserved_cny"]),
            candidate_limits=MappingProxyType(limits),
            phase13_reserved_cny=Decimal(row["reserved"]),
            phase13_committed_cny=Decimal(row["committed"]),
            phase14_available_cny=Decimal(ledger["phase14_reserved_cny"]),
        )

    def list_pending_reservations(self) -> tuple[BudgetReservation, ...]:
        """从持久状态发现全部待结算请求，不依赖崩溃前内存。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM specialist_model_budget_reservations WHERE scope_id=%s AND state='RESERVED' ORDER BY request_id;",
                    (self._scope_id,),
                )
                return tuple(self._from_row(row) for row in cursor.fetchall())

    def release_candidate_allowance(self, candidate: BudgetCandidate) -> None:
        """持久关闭候选额度；只有无待结算请求时才允许进入共享池。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                self._lock_scope(cursor)
                cursor.execute(
                    "SELECT count(*) AS count FROM specialist_model_budget_reservations WHERE scope_id=%s AND candidate_id=%s AND state='RESERVED';",
                    (self._scope_id, candidate.value),
                )
                if int(cursor.fetchone()["count"]) != 0:
                    raise BudgetInvariantError("candidate has pending reservations")
                cursor.execute(
                    "UPDATE specialist_model_budget_candidates SET state='RELEASED', version=version+1, updated_at=now() WHERE scope_id=%s AND candidate_id=%s AND state='ACTIVE';",
                    (self._scope_id, candidate.value),
                )
            conn.commit()

    def _ensure_scope(self) -> None:
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO specialist_model_budget_ledgers (
                        scope_id, total_limit_cny, phase13_limit_cny, phase14_reserved_cny
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (scope_id) DO NOTHING;
                    """,
                    (self._scope_id, TOTAL_LIMIT_CNY, PHASE13_LIMIT_CNY, PHASE14_RESERVED_CNY),
                )
                for candidate, limit in CANDIDATE_LIMITS.items():
                    cursor.execute(
                        "INSERT INTO specialist_model_budget_candidates (scope_id, candidate_id, initial_limit_cny) VALUES (%s, %s, %s) ON CONFLICT (scope_id, candidate_id) DO NOTHING;",
                        (self._scope_id, candidate.value, limit),
                    )
            conn.commit()

    def _lock_scope(self, cursor: Any) -> Mapping[str, Any]:
        cursor.execute(
            "SELECT * FROM specialist_model_budget_ledgers WHERE scope_id=%s FOR UPDATE;",
            (self._scope_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise BudgetInvariantError("budget scope is missing")
        return row

    def _load(self, cursor: Any, request_id: str, *, for_update: bool) -> BudgetReservation | None:
        cursor.execute(
            f"SELECT * FROM specialist_model_budget_reservations WHERE scope_id=%s AND request_id=%s {'FOR UPDATE' if for_update else ''};",
            (self._scope_id, request_id),
        )
        row = cursor.fetchone()
        return None if row is None else self._from_row(row)

    def _assert_sql_capacity(self, cursor: Any, candidate: BudgetCandidate, amount: Decimal, policy: Mapping[str, Any]) -> None:
        cursor.execute(
            "SELECT * FROM specialist_model_budget_candidates WHERE scope_id=%s ORDER BY candidate_id FOR UPDATE;",
            (self._scope_id,),
        )
        candidate_rows = {BudgetCandidate(row["candidate_id"]): row for row in cursor.fetchall()}
        candidate_policy = candidate_rows[candidate]
        if candidate_policy["state"] == "RELEASED":
            raise BudgetInvariantError("candidate allowance is released")
        cursor.execute(
            """
            SELECT candidate_id,
                COALESCE(sum(CASE WHEN state='RESERVED' THEN reserved_amount_cny WHEN state='SETTLED' THEN settled_amount_cny ELSE 0 END), 0) AS exposure
            FROM specialist_model_budget_reservations WHERE scope_id=%s GROUP BY candidate_id;
            """,
            (self._scope_id,),
        )
        exposures = {BudgetCandidate(row["candidate_id"]): Decimal(row["exposure"]) for row in cursor.fetchall()}
        total_exposure = sum(exposures.values(), Decimal("0"))
        candidate_total = exposures.get(candidate, Decimal("0"))
        effective_phase13 = min(
            Decimal(policy["phase13_limit_cny"]),
            Decimal(policy["total_limit_cny"]) - Decimal(policy["phase14_reserved_cny"]),
        )
        if total_exposure + amount > effective_phase13:
            raise BudgetLimitExceeded("phase budget exceeded")
        own_limit = Decimal(candidate_policy["initial_limit_cny"])
        extra_needed = max(candidate_total + amount - own_limit, Decimal("0"))
        shared_available = Decimal("0")
        for released_candidate, released_policy in candidate_rows.items():
            if released_policy["state"] != "RELEASED":
                continue
            # 已释放候选若曾借用共享池，超出自身额度的已结算费用是负贡献；
            # 不能逐候选截断为 0，否则“借用后释放”会把同一额度重新放回池中。
            shared_available += (
                Decimal(released_policy["initial_limit_cny"])
                - exposures.get(released_candidate, Decimal("0"))
            )
        existing_extra = sum(
            max(exposures.get(item, Decimal("0")) - Decimal(row["initial_limit_cny"]), Decimal("0"))
            for item, row in candidate_rows.items()
            if row["state"] == "ACTIVE"
        )
        current_extra = max(candidate_total - own_limit, Decimal("0"))
        if existing_extra + (extra_needed - current_extra) > shared_available:
            raise BudgetLimitExceeded("candidate shared budget exceeded")

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> BudgetReservation:
        return BudgetReservation(
            scope_id=row["scope_id"],
            reservation_id=str(row["reservation_id"]),
            request_id=row["request_id"],
            candidate=BudgetCandidate(row["candidate_id"]),
            reserved_amount_cny=Decimal(row["reserved_amount_cny"]),
            state=ReservationState(row["state"]),
            settled_amount_cny=None if row["settled_amount_cny"] is None else Decimal(row["settled_amount_cny"]),
            usage_known=row["usage_known"],
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def initialize_specialist_budget_schema(settings: Any) -> None:
    """执行幂等 Phase 13 DDL，供集成测试和部署迁移共同使用。"""

    sql_path = Path(__file__).resolve().parents[2] / "docker" / "init_phase13_specialist_evaluations.sql"
    sql = sql_path.read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
        conn.commit()
