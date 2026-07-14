"""Phase 12B Event Inbox 的协议与线程安全内存实现。

Event Inbox 是库存事件权威源。首次事件事实不可覆盖，每次传输投递都形成
Occurrence；Worker 只能通过 lease 与 fencing token 推进 PROCESSING 记录。同一个
事件应用到同一个 root plan 时只允许一个 EventApplication。

本模块不连接 PostgreSQL 或 Kafka。Task 3 会实现同一协议的 PostgreSQL Store，
Task 4 才负责“数据库提交后再提交 offset”的传输顺序。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from src.plan_engine.event_state_machine import (
    EventApplicationState,
    EventInboxState,
    EventOccurrenceKind,
    assert_application_transition,
    assert_inbox_transition,
)
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.skill_runtime.models import FailureFact, _deep_freeze


class EventStoreError(RuntimeError):
    """Event Store 操作失败的公共基类。"""


class EventNotFoundError(EventStoreError):
    """请求了不存在的 Inbox 或 EventApplication。"""


class EventStoreInvariantError(EventStoreError):
    """请求与已持久化事件身份、版本或状态不一致。"""


class EventLeaseError(EventStoreInvariantError):
    """Worker 的 lease、owner 或 fencing token 已失效。"""


def _aware_utc(value: datetime, field_name: str) -> datetime:
    """把 Store 边界时间统一为 UTC，拒绝机器本地时区语义。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value.astimezone(timezone.utc)


class EventDelivery(BaseModel, frozen=True):
    """一次脱敏传输投递的身份，不包含 Kafka 原始消息体。"""

    occurrence_id: str = Field(..., min_length=1)
    transport: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    partition: int | None = Field(default=None, ge=0)
    offset: int | None = Field(default=None, ge=0)
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def _received_at_is_aware(cls, value: datetime) -> datetime:
        """接收时间统一为 UTC，供并发 claim 做稳定排序。"""
        return _aware_utc(value, "received_at")

    @property
    def transport_key(self) -> tuple[str, str, int | None, int | None]:
        """返回传输坐标，用于阻止同一 offset 被不同 occurrence ID 重写。"""
        return (self.transport, self.topic, self.partition, self.offset)


class EventInboxRecord(BaseModel, frozen=True):
    """Event Inbox 的冻结读取视图。"""

    event: InventoryFactEvent
    provenance: VerifiedIngressProvenance
    state: EventInboxState
    lease_owner: str | None = Field(default=None, min_length=1)
    lease_expires_at: datetime | None = None
    fencing_token: int = Field(default=0, ge=0)
    failure: FailureFact | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("lease_expires_at", "created_at", "updated_at")
    @classmethod
    def _timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        """所有 Store 时间都使用 aware UTC，避免 lease 比较受本地时区影响。"""
        return None if value is None else _aware_utc(value, "EventInbox 时间")

    @model_validator(mode="after")
    def _lease_shape_matches_state(self) -> "EventInboxRecord":
        """PROCESSING 必须有完整 lease，其他状态不得泄漏旧 owner。"""
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        if self.state is EventInboxState.PROCESSING:
            if self.lease_owner is None or self.lease_expires_at is None:
                raise ValueError("PROCESSING EventInbox 必须持有完整 lease")
        elif self.lease_owner is not None or self.lease_expires_at is not None:
            raise ValueError("非 PROCESSING EventInbox 不得保留 lease")
        return self


class EventOccurrenceRecord(BaseModel, frozen=True):
    """一次投递相对首次事件事实的不可变审计记录。"""

    occurrence_id: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    payload_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    transport: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    partition: int | None = Field(default=None, ge=0)
    offset: int | None = Field(default=None, ge=0)
    classification: EventOccurrenceKind
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def _received_at_is_aware(cls, value: datetime) -> datetime:
        """Occurrence 时间统一为 UTC。"""
        return _aware_utc(value, "occurrence received_at")

    @property
    def transport_key(self) -> tuple[str, str, int | None, int | None]:
        """返回与 EventDelivery 相同的传输坐标。"""
        return (self.transport, self.topic, self.partition, self.offset)


class EventApplicationRecord(BaseModel, frozen=True):
    """一个事件应用到一个 root plan 的冻结处理视图。"""

    application_id: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    root_plan_run_id: str = Field(..., min_length=1)
    source_plan_version: int = Field(..., ge=1)
    state: EventApplicationState
    emergency_plan_run_id: str | None = Field(default=None, min_length=1)
    applied_plan_version: int | None = Field(default=None, ge=1)
    impact_analysis: dict[str, Any] | None = None
    failure: FailureFact | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("impact_analysis", mode="after")
    @classmethod
    def _freeze_impact_analysis(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """深度冻结 ImpactAnalysis JSON，避免调用方改写 Store 内部证据。"""
        return None if value is None else _deep_freeze(value)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timestamps_are_aware(cls, value: datetime) -> datetime:
        """Application 时间统一为 UTC。"""
        return _aware_utc(value, "EventApplication 时间")

    @model_validator(mode="after")
    def _time_order_is_valid(self) -> "EventApplicationRecord":
        """更新时间不能倒退到创建时间之前。"""
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        return self


class EventRegistrationResult(BaseModel, frozen=True):
    """登记结果同时返回权威 Inbox 与本次 occurrence。"""

    created: bool
    inbox: EventInboxRecord
    occurrence: EventOccurrenceRecord


class EventClaim(BaseModel, frozen=True):
    """Worker claim 后取得的 Inbox 快照与 fencing token。"""

    record: EventInboxRecord
    fencing_token: int = Field(..., ge=1)


class EventApplicationCreateResult(BaseModel, frozen=True):
    """EventApplication 首次创建或幂等重放结果。"""

    created: bool
    application: EventApplicationRecord


class EventStore(Protocol):
    """内存与 PostgreSQL Event Store 共同实现的最小协议。"""

    def register_event(
        self,
        event: InventoryFactEvent,
        provenance: VerifiedIngressProvenance,
        delivery: EventDelivery,
    ) -> EventRegistrationResult:
        """登记首次、重复或冲突投递。"""
        ...

    def get_inbox(self, event_id: str) -> EventInboxRecord:
        """读取一个权威 Inbox 事实。"""
        ...

    def list_inbox(self) -> tuple[EventInboxRecord, ...]:
        """读取稳定排序的全部 Inbox 快照。"""
        ...

    def list_occurrences(self, event_id: str) -> tuple[EventOccurrenceRecord, ...]:
        """按接收顺序读取一个事件的全部投递。"""
        ...

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> EventClaim | None:
        """领取最早可处理事件或返回空。"""
        ...

    def heartbeat(
        self,
        event_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        lease_seconds: int,
    ) -> EventInboxRecord:
        """续租当前 claim。"""
        ...

    def transition_inbox(
        self,
        event_id: str,
        *,
        expected_state: EventInboxState,
        target_state: EventInboxState,
        now: datetime,
        worker_id: str | None = None,
        fencing_token: int | None = None,
        failure: FailureFact | None = None,
    ) -> EventInboxRecord:
        """按状态和 fencing 推进 Inbox。"""
        ...

    def create_application(
        self,
        event_id: str,
        *,
        root_plan_run_id: str,
        source_plan_version: int,
        now: datetime,
    ) -> EventApplicationCreateResult:
        """按 event/root 幂等创建应用记录。"""
        ...

    def get_application(
        self,
        event_id: str,
        root_plan_run_id: str,
    ) -> EventApplicationRecord:
        """读取 event/root Application。"""
        ...

    def list_applications(
        self,
        *,
        root_plan_run_id: str | None = None,
    ) -> tuple[EventApplicationRecord, ...]:
        """读取全部或指定 root 的 Application。"""
        ...

    def transition_application(
        self,
        event_id: str,
        root_plan_run_id: str,
        *,
        expected_state: EventApplicationState,
        target_state: EventApplicationState,
        now: datetime,
        emergency_plan_run_id: str | None = None,
        applied_plan_version: int | None = None,
        impact_analysis: dict[str, Any] | None = None,
        failure: FailureFact | None = None,
    ) -> EventApplicationRecord:
        """按白名单推进 Application 并记录结构化证据。"""
        ...


class InMemoryEventStore:
    """使用单进程锁模拟数据库原子边界的线程安全 Event Store。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._inbox: dict[str, EventInboxRecord] = {}
        self._occurrences: dict[str, list[EventOccurrenceRecord]] = {}
        self._occurrences_by_id: dict[str, EventOccurrenceRecord] = {}
        self._occurrence_id_by_transport_key: dict[
            tuple[str, str, int | None, int | None],
            str,
        ] = {}
        self._applications: dict[
            tuple[str, str],
            EventApplicationRecord,
        ] = {}

    def register_event(
        self,
        event: InventoryFactEvent,
        provenance: VerifiedIngressProvenance,
        delivery: EventDelivery,
    ) -> EventRegistrationResult:
        """原子登记事件和 occurrence，首次 payload 永不被后续投递覆盖。"""
        self._validate_registration(event, provenance, delivery)
        with self._lock:
            replay = self._replay_delivery_if_present(event, delivery)
            if replay is not None:
                return replay
            self._assert_delivery_identity_available(delivery)

            existing = self._inbox.get(event.event_id)
            if existing is None:
                inbox = EventInboxRecord(
                    event=event,
                    provenance=provenance,
                    state=EventInboxState.VERIFIED,
                    created_at=delivery.received_at,
                    updated_at=delivery.received_at,
                )
                classification = EventOccurrenceKind.ACCEPTED
                created = True
            elif existing.event.payload_digest == event.payload_digest:
                inbox = existing
                classification = EventOccurrenceKind.DUPLICATE
                created = False
            else:
                # 冲突是对事件身份的安全否定，不走普通业务状态机。无论 Worker 正在
                # 处理还是事件已到终态，首次事实都保留，但当前 claim 必须立即失效。
                inbox = self._copy_inbox(
                    existing,
                    state=EventInboxState.CONFLICT,
                    lease_owner=None,
                    lease_expires_at=None,
                    fencing_token=existing.fencing_token
                    + (1 if existing.lease_owner is not None else 0),
                    updated_at=max(existing.updated_at, delivery.received_at),
                )
                classification = EventOccurrenceKind.CONFLICT
                created = False

            occurrence = EventOccurrenceRecord(
                occurrence_id=delivery.occurrence_id,
                event_id=event.event_id,
                payload_digest=event.payload_digest,
                transport=delivery.transport,
                topic=delivery.topic,
                partition=delivery.partition,
                offset=delivery.offset,
                classification=classification,
                received_at=delivery.received_at,
            )
            # 所有 Pydantic 快照都验证成功后再一次性替换内部映射。这样即使未来模型
            # 增加新不变量，构造失败也不会留下已占用坐标或半条 Inbox 记录。
            self._inbox[event.event_id] = inbox
            self._occurrences.setdefault(event.event_id, []).append(occurrence)
            self._occurrences_by_id[occurrence.occurrence_id] = occurrence
            self._occurrence_id_by_transport_key[
                delivery.transport_key
            ] = delivery.occurrence_id
            return EventRegistrationResult(
                created=created,
                inbox=inbox,
                occurrence=occurrence,
            )

    def get_inbox(self, event_id: str) -> EventInboxRecord:
        """读取权威 Inbox；未知 ID 不返回空对象。"""
        with self._lock:
            try:
                return self._inbox[event_id]
            except KeyError as exc:
                raise EventNotFoundError(f"未知 event_id: {event_id}") from exc

    def list_inbox(self) -> tuple[EventInboxRecord, ...]:
        """按创建时间和事件 ID 返回稳定排序的冻结 Inbox 快照。"""
        with self._lock:
            return tuple(
                sorted(
                    self._inbox.values(),
                    key=lambda item: (item.created_at, item.event.event_id),
                )
            )

    def list_occurrences(self, event_id: str) -> tuple[EventOccurrenceRecord, ...]:
        """返回 occurrence 元组，调用方不能向 Store 内部列表追加记录。"""
        with self._lock:
            if event_id not in self._inbox:
                raise EventNotFoundError(f"未知 event_id: {event_id}")
            return tuple(self._occurrences.get(event_id, ()))

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> EventClaim | None:
        """领取最早 VERIFIED 或租约已过期的 PROCESSING 事件。"""
        if not worker_id:
            raise ValueError("worker_id 不能为空")
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于等于 1")
        current_time = _aware_utc(now, "claim now")
        with self._lock:
            eligible = [
                record
                for record in self._inbox.values()
                if record.created_at <= current_time
                and (
                    record.state is EventInboxState.VERIFIED
                    or (
                        record.state is EventInboxState.PROCESSING
                        and record.lease_expires_at is not None
                        and record.lease_expires_at <= current_time
                    )
                )
            ]
            if not eligible:
                return None
            record = min(
                eligible,
                key=lambda item: (item.created_at, item.event.event_id),
            )
            if record.state is EventInboxState.VERIFIED:
                assert_inbox_transition(record.state, EventInboxState.PROCESSING)
            updated = self._copy_inbox(
                record,
                state=EventInboxState.PROCESSING,
                lease_owner=worker_id,
                lease_expires_at=current_time + timedelta(seconds=lease_seconds),
                fencing_token=record.fencing_token + 1,
                updated_at=max(record.updated_at, current_time),
            )
            self._inbox[record.event.event_id] = updated
            return EventClaim(record=updated, fencing_token=updated.fencing_token)

    def heartbeat(
        self,
        event_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        lease_seconds: int,
    ) -> EventInboxRecord:
        """为当前 claim 续租；过期、换 owner 或旧 token 均拒绝。"""
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于等于 1")
        current_time = _aware_utc(now, "heartbeat now")
        with self._lock:
            record = self.get_inbox(event_id)
            self._assert_current_claim(
                record,
                worker_id=worker_id,
                fencing_token=fencing_token,
                now=current_time,
            )
            requested_expiry = current_time + timedelta(seconds=lease_seconds)
            assert record.lease_expires_at is not None
            updated = self._copy_inbox(
                record,
                lease_expires_at=max(record.lease_expires_at, requested_expiry),
                updated_at=max(record.updated_at, current_time),
            )
            self._inbox[event_id] = updated
            return updated

    def transition_inbox(
        self,
        event_id: str,
        *,
        expected_state: EventInboxState,
        target_state: EventInboxState,
        now: datetime,
        worker_id: str | None = None,
        fencing_token: int | None = None,
        failure: FailureFact | None = None,
    ) -> EventInboxRecord:
        """按预期状态推进 Inbox，PROCESSING 终态必须通过当前 fencing。"""
        current_time = _aware_utc(now, "transition now")
        with self._lock:
            record = self.get_inbox(event_id)
            if record.state is not expected_state:
                raise EventStoreInvariantError(
                    f"EventInbox 状态不匹配: 期望 {expected_state.value}，"
                    f"实际 {record.state.value}"
                )
            if record.state is EventInboxState.PROCESSING:
                if worker_id is None or fencing_token is None:
                    raise EventLeaseError("PROCESSING 转移必须提供 worker_id 与 fencing token")
                self._assert_current_claim(
                    record,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    now=current_time,
                )
            if target_state is EventInboxState.PROCESSING:
                raise EventStoreInvariantError("PROCESSING 只能由 claim_next 进入")
            assert_inbox_transition(record.state, target_state)
            if target_state is EventInboxState.FAILED and failure is None:
                raise EventStoreInvariantError("FAILED EventInbox 必须保存 FailureFact")
            if failure is not None and target_state not in {
                EventInboxState.FAILED,
                EventInboxState.WAITING_HUMAN,
                EventInboxState.VERIFIED,
            }:
                raise EventStoreInvariantError("当前 EventInbox 目标状态不能写入 FailureFact")

            updated = self._copy_inbox(
                record,
                state=target_state,
                lease_owner=None,
                lease_expires_at=None,
                failure=record.failure if failure is None else failure,
                updated_at=max(record.updated_at, current_time),
            )
            self._inbox[event_id] = updated
            return updated

    def create_application(
        self,
        event_id: str,
        *,
        root_plan_run_id: str,
        source_plan_version: int,
        now: datetime,
    ) -> EventApplicationCreateResult:
        """按 event/root 创建唯一 Application，相同意图返回首次记录。"""
        current_time = _aware_utc(now, "application now")
        with self._lock:
            if event_id not in self._inbox:
                raise EventNotFoundError(f"未知 event_id: {event_id}")
            key = (event_id, root_plan_run_id)
            existing = self._applications.get(key)
            if existing is not None:
                if existing.source_plan_version != source_plan_version:
                    raise EventStoreInvariantError(
                        "同一 event/root 的 source_plan_version 与首次意图不一致"
                    )
                return EventApplicationCreateResult(
                    created=False,
                    application=existing,
                )
            application = EventApplicationRecord(
                application_id=str(uuid4()),
                event_id=event_id,
                root_plan_run_id=root_plan_run_id,
                source_plan_version=source_plan_version,
                state=EventApplicationState.PENDING,
                created_at=current_time,
                updated_at=current_time,
            )
            self._applications[key] = application
            return EventApplicationCreateResult(
                created=True,
                application=application,
            )

    def get_application(
        self,
        event_id: str,
        root_plan_run_id: str,
    ) -> EventApplicationRecord:
        """读取 event/root Application；未知组合 fail-closed。"""
        with self._lock:
            try:
                return self._applications[(event_id, root_plan_run_id)]
            except KeyError as exc:
                raise EventNotFoundError(
                    f"未知 EventApplication: {event_id}/{root_plan_run_id}"
                ) from exc

    def list_applications(
        self,
        *,
        root_plan_run_id: str | None = None,
    ) -> tuple[EventApplicationRecord, ...]:
        """按创建时间返回全部或指定 root 的 Application 冻结视图。"""
        with self._lock:
            records = [
                record
                for record in self._applications.values()
                if root_plan_run_id is None
                or record.root_plan_run_id == root_plan_run_id
            ]
            return tuple(
                sorted(
                    records,
                    key=lambda item: (
                        item.created_at,
                        item.event_id,
                        item.root_plan_run_id,
                    ),
                )
            )

    def transition_application(
        self,
        event_id: str,
        root_plan_run_id: str,
        *,
        expected_state: EventApplicationState,
        target_state: EventApplicationState,
        now: datetime,
        emergency_plan_run_id: str | None = None,
        applied_plan_version: int | None = None,
        impact_analysis: dict[str, Any] | None = None,
        failure: FailureFact | None = None,
    ) -> EventApplicationRecord:
        """按白名单推进 Application，并合并本次新增的结构化证据。"""
        current_time = _aware_utc(now, "application transition now")
        with self._lock:
            record = self.get_application(event_id, root_plan_run_id)
            if record.state is not expected_state:
                raise EventStoreInvariantError(
                    f"EventApplication 状态不匹配: 期望 {expected_state.value}，"
                    f"实际 {record.state.value}"
                )
            assert_application_transition(record.state, target_state)
            if target_state is EventApplicationState.FAILED and failure is None:
                raise EventStoreInvariantError("FAILED EventApplication 必须保存 FailureFact")
            if failure is not None and target_state not in {
                EventApplicationState.FAILED,
                EventApplicationState.WAITING_RECONCILIATION,
            }:
                raise EventStoreInvariantError(
                    "当前 EventApplication 目标状态不能写入 FailureFact"
                )
            if (
                target_state is EventApplicationState.APPLIED
                and applied_plan_version is None
            ):
                raise EventStoreInvariantError("APPLIED Application 必须关联 applied_plan_version")
            self._assert_write_once(
                "emergency_plan_run_id",
                record.emergency_plan_run_id,
                emergency_plan_run_id,
            )
            self._assert_write_once(
                "applied_plan_version",
                record.applied_plan_version,
                applied_plan_version,
            )
            self._assert_write_once(
                "impact_analysis",
                record.impact_analysis,
                impact_analysis,
            )

            payload = record.model_dump(mode="python")
            payload.update(
                {
                    "state": target_state,
                    "updated_at": max(record.updated_at, current_time),
                    "emergency_plan_run_id": (
                        record.emergency_plan_run_id
                        if emergency_plan_run_id is None
                        else emergency_plan_run_id
                    ),
                    "applied_plan_version": (
                        record.applied_plan_version
                        if applied_plan_version is None
                        else applied_plan_version
                    ),
                    "impact_analysis": (
                        record.impact_analysis
                        if impact_analysis is None
                        else impact_analysis
                    ),
                    "failure": record.failure if failure is None else failure,
                }
            )
            updated = EventApplicationRecord.model_validate(payload)
            self._applications[(event_id, root_plan_run_id)] = updated
            return updated

    def _validate_registration(
        self,
        event: InventoryFactEvent,
        provenance: VerifiedIngressProvenance,
        delivery: EventDelivery,
    ) -> None:
        """在持锁前验证事件、来源与传输身份闭合，不接受 payload 自报信任。"""
        if event.calculate_payload_digest() != event.payload_digest:
            raise EventStoreInvariantError("事件字段与 payload 摘要不一致")
        if provenance.payload_digest != event.payload_digest:
            raise EventStoreInvariantError("事件与 provenance 摘要不一致")
        if provenance.source != event.source:
            raise EventStoreInvariantError("事件与 provenance source 不一致")
        if delivery.transport != provenance.transport or delivery.topic != provenance.topic:
            raise EventStoreInvariantError("delivery 与 provenance 传输身份不一致")

    def _replay_delivery_if_present(
        self,
        event: InventoryFactEvent,
        delivery: EventDelivery,
    ) -> EventRegistrationResult | None:
        """同一 delivery 完全重放时返回首次结果，不追加第二条 occurrence。"""
        existing = self._occurrences_by_id.get(delivery.occurrence_id)
        if existing is None:
            return None
        if (
            existing.event_id != event.event_id
            or existing.payload_digest != event.payload_digest
            or existing.transport_key != delivery.transport_key
            or existing.received_at != delivery.received_at
        ):
            raise EventStoreInvariantError(
                f"occurrence_id 已绑定不同投递: {delivery.occurrence_id}"
            )
        return EventRegistrationResult(
            created=False,
            inbox=self._inbox[event.event_id],
            occurrence=existing,
        )

    def _assert_delivery_identity_available(self, delivery: EventDelivery) -> None:
        """只校验传输坐标可用，实际占用延迟到登记事务最后。"""
        existing_id = self._occurrence_id_by_transport_key.get(delivery.transport_key)
        if existing_id is not None and existing_id != delivery.occurrence_id:
            raise EventStoreInvariantError(
                f"传输坐标已绑定 occurrence_id: {existing_id}"
            )

    @staticmethod
    def _copy_inbox(
        record: EventInboxRecord,
        **updates: Any,
    ) -> EventInboxRecord:
        """通过完整 Pydantic 重验证生成新快照，不用 model_copy 绕过不变量。"""
        payload = record.model_dump(mode="python")
        payload.update(updates)
        return EventInboxRecord.model_validate(payload)

    @staticmethod
    def _assert_current_claim(
        record: EventInboxRecord,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        """同时校验状态、owner、token 与绝对过期时间。"""
        if record.state is not EventInboxState.PROCESSING:
            raise EventLeaseError("EventInbox 当前不在 PROCESSING")
        if record.lease_owner != worker_id:
            raise EventLeaseError("EventInbox lease owner 不匹配")
        if record.fencing_token != fencing_token:
            raise EventLeaseError("EventInbox fencing token 已失效")
        if record.lease_expires_at is None or record.lease_expires_at <= now:
            raise EventLeaseError("EventInbox lease 已过期")

    @staticmethod
    def _assert_write_once(
        field_name: str,
        existing: Any,
        incoming: Any,
    ) -> None:
        """关联事实一旦写入只能重复相同值，不能在后续状态转移中被覆盖。"""
        if existing is not None and incoming is not None and existing != incoming:
            raise EventStoreInvariantError(f"{field_name} 与首次持久化事实不一致")
