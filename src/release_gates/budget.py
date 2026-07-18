"""Phase 15 独立 Copilot smoke 预算账本。

Phase 13/14 的共享预算模块保持历史身份不变；Phase 15 使用自己的 scope、表和
0.60 元上限，避免后续发布门禁源码改变旧阶段的 code digest，也避免任何阶段
借用另一阶段的未消费额度。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg.rows import dict_row


PHASE15_COPILOT = "PHASE15_COPILOT"
PHASE15_BUDGET_CNY = Decimal("0.60")


class Phase15ReservationState(StrEnum):
    """Phase 15 smoke 费用预留的不可逆状态。"""

    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class Phase15BudgetInvariantError(RuntimeError):
    """同一 request 重放冲突或非法预算状态转换。"""


class Phase15BudgetLimitExceeded(RuntimeError):
    """预留会突破 Phase 15 固定 0.60 元上限。"""


@dataclass(frozen=True)
class Phase15BudgetReservation:
    """一次 smoke 请求的不可变费用事实。"""

    scope_id: str
    reservation_id: str
    request_id: str
    reserved_amount_cny: Decimal
    state: Phase15ReservationState
    settled_amount_cny: Decimal | None
    usage_known: bool | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Phase15ReservationClaim:
    """区分首次预留与重启后的幂等恢复。"""

    record: Phase15BudgetReservation
    created: bool


@dataclass(frozen=True)
class Phase15BudgetSnapshot:
    """面向 Release 报告的 Phase 15 只读余额。"""

    scope_id: str
    reserved_cny: Decimal
    committed_cny: Decimal
    available_cny: Decimal


def _reservation_id(scope_id: str, request_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{scope_id}\x1f{request_id}"))


def _amount(value: Decimal, *, allow_zero: bool = False) -> Decimal:
    """校验 six-place Decimal，拒绝 NaN、Infinity 和预算范围外金额。"""

    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise Phase15BudgetInvariantError("phase 15 budget amount must be finite and positive")
    try:
        quantized = amount.quantize(Decimal("0.000001"))
    except InvalidOperation as error:
        raise Phase15BudgetInvariantError("phase 15 budget amount exceeds precision") from error
    if amount != quantized or amount > PHASE15_BUDGET_CNY:
        raise Phase15BudgetInvariantError("phase 15 budget amount exceeds six-place limit")
    return amount


class Phase15BudgetStore:
    """锁内模拟 Phase 15 PostgreSQL ledger 的幂等内存实现。"""

    def __init__(self, *, scope_id: str = "phase15-release-v1") -> None:
        self._scope_id = scope_id
        self._lock = Lock()
        self._records: dict[str, Phase15BudgetReservation] = {}

    def reserve(self, request_id: str, amount_cny: Decimal) -> Phase15ReservationClaim:
        """发送前预留费用；相同 request 重放返回原 reservation。"""

        if not request_id:
            raise Phase15BudgetInvariantError("request_id is required")
        amount = _amount(amount_cny)
        with self._lock:
            existing = self._records.get(request_id)
            if existing is not None:
                if existing.reserved_amount_cny != amount:
                    raise Phase15BudgetInvariantError("conflicting phase 15 reservation replay")
                return Phase15ReservationClaim(existing, created=False)
            if self._exposure() + amount > PHASE15_BUDGET_CNY:
                raise Phase15BudgetLimitExceeded("phase 15 budget exceeded")
            now = datetime.now(timezone.utc)
            record = Phase15BudgetReservation(
                scope_id=self._scope_id,
                reservation_id=_reservation_id(self._scope_id, request_id),
                request_id=request_id,
                reserved_amount_cny=amount,
                state=Phase15ReservationState.RESERVED,
                settled_amount_cny=None,
                usage_known=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._records[request_id] = record
            return Phase15ReservationClaim(record, created=True)

    def settle(self, request_id: str, actual_cost_cny: Decimal | None) -> Phase15BudgetReservation:
        """结算已发送请求；usage 缺失时保守结算整个 reservation。"""

        with self._lock:
            record = self._required(request_id)
            amount = record.reserved_amount_cny if actual_cost_cny is None else _amount(actual_cost_cny, allow_zero=True)
            known = actual_cost_cny is not None
            if record.state is Phase15ReservationState.SETTLED:
                if record.settled_amount_cny != amount or record.usage_known is not known:
                    raise Phase15BudgetInvariantError("conflicting phase 15 settlement replay")
                return record
            if record.state is not Phase15ReservationState.RESERVED:
                raise Phase15BudgetInvariantError("phase 15 reservation is not pending")
            settled = replace(
                record,
                state=Phase15ReservationState.SETTLED,
                settled_amount_cny=amount,
                usage_known=known,
                version=record.version + 1,
                updated_at=datetime.now(timezone.utc),
            )
            self._records[request_id] = settled
            return settled

    def release(self, request_id: str) -> Phase15BudgetReservation:
        """只释放尚未发送的 reservation，已结算费用不可释放。"""

        with self._lock:
            record = self._required(request_id)
            if record.state is Phase15ReservationState.RELEASED:
                return record
            if record.state is not Phase15ReservationState.RESERVED:
                raise Phase15BudgetInvariantError("settled phase 15 reservation cannot be released")
            released = replace(
                record,
                state=Phase15ReservationState.RELEASED,
                version=record.version + 1,
                updated_at=datetime.now(timezone.utc),
            )
            self._records[request_id] = released
            return released

    def snapshot(self) -> Phase15BudgetSnapshot:
        """重算预留、已结算和可用余额。"""

        with self._lock:
            reserved = sum(
                (item.reserved_amount_cny for item in self._records.values() if item.state is Phase15ReservationState.RESERVED),
                Decimal("0"),
            )
            committed = sum(
                (item.settled_amount_cny or Decimal("0") for item in self._records.values() if item.state is Phase15ReservationState.SETTLED),
                Decimal("0"),
            )
            return Phase15BudgetSnapshot(
                scope_id=self._scope_id,
                reserved_cny=reserved,
                committed_cny=committed,
                available_cny=max(PHASE15_BUDGET_CNY - reserved - committed, Decimal("0")),
            )

    def list_pending_reservations(self) -> tuple[Phase15BudgetReservation, ...]:
        """返回重启恢复扫描所需的全部 RESERVED 记录。"""

        with self._lock:
            return tuple(sorted((item for item in self._records.values() if item.state is Phase15ReservationState.RESERVED), key=lambda item: item.request_id))

    def _required(self, request_id: str) -> Phase15BudgetReservation:
        try:
            return self._records[request_id]
        except KeyError as error:
            raise Phase15BudgetInvariantError("unknown phase 15 reservation") from error

    def _exposure(self) -> Decimal:
        return sum(
            (
                item.reserved_amount_cny
                if item.state is Phase15ReservationState.RESERVED
                else item.settled_amount_cny or Decimal("0")
            )
            for item in self._records.values()
            if item.state is not Phase15ReservationState.RELEASED
        )


class PostgresPhase15BudgetStore:
    """用 Phase 15 自有 ledger 表保存跨进程预算 reservation。"""

    def __init__(self, settings: Any, *, scope_id: str = "phase15-release-v1") -> None:
        self._settings = settings
        self._scope_id = scope_id
        self._ensure_scope()

    def reserve(self, request_id: str, amount_cny: Decimal) -> Phase15ReservationClaim:
        """在 ledger 行锁内检查余额并按 request 唯一写入。"""

        amount = _amount(amount_cny)
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM phase15_budget_ledgers WHERE scope_id=%s FOR UPDATE;", (self._scope_id,))
                ledger = cursor.fetchone()
                cursor.execute("SELECT * FROM phase15_budget_reservations WHERE scope_id=%s AND request_id=%s FOR UPDATE;", (self._scope_id, request_id))
                existing = cursor.fetchone()
                if existing is not None:
                    loaded = self._from_row(existing)
                    if loaded.reserved_amount_cny != amount:
                        raise Phase15BudgetInvariantError("conflicting phase 15 reservation replay")
                    return Phase15ReservationClaim(loaded, created=False)
                cursor.execute("SELECT COALESCE(sum(CASE WHEN state='RESERVED' THEN reserved_amount_cny WHEN state='SETTLED' THEN settled_amount_cny ELSE 0 END),0) AS exposure FROM phase15_budget_reservations WHERE scope_id=%s;", (self._scope_id,))
                exposure = Decimal(cursor.fetchone()["exposure"])
                if exposure + amount > Decimal(ledger["limit_cny"]):
                    raise Phase15BudgetLimitExceeded("phase 15 budget exceeded")
                reservation_id = _reservation_id(self._scope_id, request_id)
                cursor.execute("INSERT INTO phase15_budget_reservations (reservation_id, scope_id, request_id, reserved_amount_cny, state, version) VALUES (%s::uuid,%s,%s,%s,'RESERVED',1) RETURNING *;", (reservation_id, self._scope_id, request_id, amount))
                row = cursor.fetchone()
            conn.commit()
        return Phase15ReservationClaim(self._from_row(row), created=True)

    def settle(self, request_id: str, actual_cost_cny: Decimal | None) -> Phase15BudgetReservation:
        """按数据库事实结算 reservation，保持重复 settle 幂等。"""

        amount = None if actual_cost_cny is None else _amount(actual_cost_cny, allow_zero=True)
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM phase15_budget_reservations WHERE scope_id=%s AND request_id=%s FOR UPDATE;", (self._scope_id, request_id))
                row = cursor.fetchone()
                if row is None:
                    raise Phase15BudgetInvariantError("unknown phase 15 reservation")
                current = self._from_row(row)
                settled = current.reserved_amount_cny if amount is None else amount
                known = amount is not None
                if current.state is Phase15ReservationState.SETTLED:
                    if current.settled_amount_cny != settled or current.usage_known is not known:
                        raise Phase15BudgetInvariantError("conflicting phase 15 settlement replay")
                    return current
                if current.state is not Phase15ReservationState.RESERVED:
                    raise Phase15BudgetInvariantError("phase 15 reservation is not pending")
                cursor.execute("UPDATE phase15_budget_reservations SET state='SETTLED', settled_amount_cny=%s, usage_known=%s, version=version+1, updated_at=now() WHERE scope_id=%s AND request_id=%s RETURNING *;", (settled, known, self._scope_id, request_id))
                row = cursor.fetchone()
            conn.commit()
        return self._from_row(row)

    def snapshot(self) -> Phase15BudgetSnapshot:
        """从数据库重算 Phase 15 余额。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT limit_cny FROM phase15_budget_ledgers WHERE scope_id=%s;", (self._scope_id,))
                limit = Decimal(cursor.fetchone()["limit_cny"])
                cursor.execute("SELECT COALESCE(sum(reserved_amount_cny) FILTER (WHERE state='RESERVED'),0) AS reserved, COALESCE(sum(settled_amount_cny) FILTER (WHERE state='SETTLED'),0) AS committed FROM phase15_budget_reservations WHERE scope_id=%s;", (self._scope_id,))
                row = cursor.fetchone()
        reserved = Decimal(row["reserved"])
        committed = Decimal(row["committed"])
        return Phase15BudgetSnapshot(self._scope_id, reserved, committed, max(limit - reserved - committed, Decimal("0")))

    def list_pending_reservations(self) -> tuple[Phase15BudgetReservation, ...]:
        """从数据库读取崩溃恢复所需的 RESERVED 请求。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM phase15_budget_reservations WHERE scope_id=%s AND state='RESERVED' ORDER BY request_id;",
                    (self._scope_id,),
                )
                rows = cursor.fetchall()
        return tuple(self._from_row(row) for row in rows)

    def _ensure_scope(self) -> None:
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO phase15_budget_ledgers (scope_id, limit_cny) VALUES (%s,%s) ON CONFLICT (scope_id) DO NOTHING;", (self._scope_id, PHASE15_BUDGET_CNY))
            conn.commit()

    @staticmethod
    def _from_row(row: Any) -> Phase15BudgetReservation:
        return Phase15BudgetReservation(
            scope_id=row["scope_id"],
            reservation_id=str(row["reservation_id"]),
            request_id=row["request_id"],
            reserved_amount_cny=Decimal(row["reserved_amount_cny"]),
            state=Phase15ReservationState(row["state"]),
            settled_amount_cny=None if row["settled_amount_cny"] is None else Decimal(row["settled_amount_cny"]),
            usage_known=row["usage_known"],
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def initialize_phase15_budget_schema(settings: Any) -> None:
    """由 Phase 15 DDL 入口统一创建独立预算表。"""

    sql_path = Path(__file__).resolve().parents[2] / "docker" / "init_phase15_release_gates.sql"
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql_path.read_text(encoding="utf-8"))
        conn.commit()
