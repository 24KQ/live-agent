"""Phase 14 播中决策支持的确定性证据聚合与最小权限只读解析。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import re
from threading import RLock
from types import MappingProxyType
from typing import Annotated, Any, Callable, Literal, Protocol
import unicodedata
from weakref import WeakKeyDictionary

from pydantic import (
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from src.decision_support.models import (
    DecisionSupportFrozenModel,
    EvidenceBundle,
    Incident,
    LiveSessionWorkspace,
    WorkspaceView,
)
from src.plan_engine.event_state_machine import EventApplicationState, EventInboxState
from src.plan_engine.events import (
    InventoryFactEvent,
    VerifiedIngressProvenance,
    _build_event_authorization_context,
)
from src.plan_engine.models import PlanRunKind, PlanRunState
from src.skill_runtime.models import SideEffectState
from src.specialist_runtime.models import (
    EvidenceKind,
    EvidenceRef,
    canonical_json_sha256,
)


class EvidenceAssemblyError(RuntimeError):
    """证据缺失、越界、陈旧、冲突或摘要不闭合时的稳定领域错误。"""


class AssembledEvidenceBundle:
    """仅由受治理 Assembler 签发的 Store 写入能力包装。"""

    __slots__ = ("_bundle", "__weakref__")

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        """禁止公开构造，receipt 只能由启动冻结的 Assembler 闭包签发。"""

        raise TypeError("assembled evidence must be issued by governed assembler")

    @property
    def bundle(self) -> EvidenceBundle:
        """返回已完成全部权威读取与校验的不可变 Bundle 事实。"""

        return self._bundle

    def __getattr__(self, name: str) -> Any:
        """只读转发事实字段，避免调用方为读取稳定 ID 而拆掉写入能力包装。"""

        return getattr(self._bundle, name)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("assembled evidence bundle is immutable")


class EvidenceBundlePersistencePort(Protocol):
    """应用服务唯一需要的持久化能力，禁止把完整 Store/SQL 交给证据调用方。"""

    def append_evidence_bundle(
        self,
        fact: AssembledEvidenceBundle,
        *,
        expected_workspace_version: int,
    ) -> LiveSessionWorkspace:
        """追加已签发证据，并由底层 Store 原子校验版本和父事实。"""


class EvidenceRole(StrEnum):
    """Phase 14 播中方案生成前必须解析的固定证据角色。"""

    VERIFIED_EVENT = "VERIFIED_EVENT"
    PRODUCT_INVENTORY_SNAPSHOT = "PRODUCT_INVENTORY_SNAPSHOT"
    ROOT_PLAN_SNAPSHOT = "ROOT_PLAN_SNAPSHOT"
    EMERGENCY_PLAN_SNAPSHOT = "EMERGENCY_PLAN_SNAPSHOT"
    DANMAKU_AGGREGATE = "DANMAKU_AGGREGATE"
    RHYTHM_SIGNAL = "RHYTHM_SIGNAL"


class DanmakuNoiseLevel(StrEnum):
    """弹幕窗口中的确定性噪声等级。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RhythmSignalKind(StrEnum):
    """只读节奏 Provider 可以返回的封闭信号。"""

    STEADY = "STEADY"
    ACCELERATING = "ACCELERATING"
    PAUSE_REQUIRED = "PAUSE_REQUIRED"
    RECOVERY_WINDOW = "RECOVERY_WINDOW"


ROLE_EVIDENCE_KIND: Mapping[EvidenceRole, EvidenceKind] = MappingProxyType(
    {
        EvidenceRole.VERIFIED_EVENT: EvidenceKind.EVENT,
        # 商品上下文来自受审计的只读平台解析 Attempt，而不是 Catalog 任意查询。
        EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT: EvidenceKind.SKILL_ATTEMPT,
        EvidenceRole.ROOT_PLAN_SNAPSHOT: EvidenceKind.PLAN,
        EvidenceRole.EMERGENCY_PLAN_SNAPSHOT: EvidenceKind.PLAN,
        EvidenceRole.DANMAKU_AGGREGATE: EvidenceKind.AUDIT,
        EvidenceRole.RHYTHM_SIGNAL: EvidenceKind.AUDIT,
    }
)

DEFAULT_EVIDENCE_TTL_SECONDS: Mapping[EvidenceRole, int] = MappingProxyType(
    {
        EvidenceRole.VERIFIED_EVENT: 30,
        EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT: 15,
        EvidenceRole.ROOT_PLAN_SNAPSHOT: 60,
        EvidenceRole.EMERGENCY_PLAN_SNAPSHOT: 30,
        EvidenceRole.DANMAKU_AGGREGATE: 15,
        EvidenceRole.RHYTHM_SIGNAL: 10,
    }
)

_DANMAKU_TOPIC_SUMMARIES: Mapping[str, str] = MappingProxyType(
    {
        "PRODUCT_AVAILABILITY": "用户集中询问主商品是否还有库存",
        "BACKUP_AVAILABILITY": "用户集中询问可替代商品是否可购买",
        "PRICE_CONCERN": "用户集中询问商品价格与优惠信息",
        "HOST_PACING": "用户集中反馈当前讲解节奏",
        "OTHER": "其他已聚合且无法归类的问题",
    }
)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    """拒绝裸时间并统一为 UTC，避免 freshness 依赖进程本地时区。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class EvidenceScope(DecisionSupportFrozenModel):
    """比通用 EvidenceRef 更完整的直播决策证据作用域。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    live_session_id: str = Field(..., min_length=1)
    incident_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    anchor_id: str = Field(..., min_length=1)
    root_plan_run_id: str = Field(..., min_length=1)


class ProductSnapshotEvidence(DecisionSupportFrozenModel):
    """计划时或事故时的单商品版本化只读快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=80)
    price: str = Field(..., pattern=r"^\d+(?:\.\d{1,2})?$")
    inventory: int = Field(..., ge=0, strict=True)
    version: int = Field(..., ge=1, strict=True)
    is_active: bool = Field(..., strict=True)

    @field_validator("product_id", "name")
    @classmethod
    def _reject_blank_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("product text cannot contain surrounding whitespace")
        return value


class VerifiedEventPayload(DecisionSupportFrozenModel):
    """Event Inbox、provenance 和 EventApplication 的冻结联合投影。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_type: Literal["VERIFIED_EVENT"] = "VERIFIED_EVENT"
    event: InventoryFactEvent
    provenance: VerifiedIngressProvenance
    inbox_state: EventInboxState
    application_state: EventApplicationState
    emergency_plan_run_id: str | None = Field(default=None, min_length=1)
    applied_plan_version: int | None = Field(default=None, ge=1, strict=True)
    side_effect_state: SideEffectState


class ProductInventoryPayload(DecisionSupportFrozenModel):
    """冻结计划商品与事故时库存/CAS 版本的对照事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_type: Literal["PRODUCT_INVENTORY"] = "PRODUCT_INVENTORY"
    captured_at: datetime
    sold_out_product_id: str = Field(..., min_length=1)
    expected_version: int = Field(..., ge=1, strict=True)
    planned_product: ProductSnapshotEvidence
    current_product: ProductSnapshotEvidence
    backup_products: tuple[ProductSnapshotEvidence, ...] = Field(
        ..., min_length=1, max_length=10
    )

    @field_validator("captured_at")
    @classmethod
    def _normalize_captured_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "product captured_at")

    @model_validator(mode="after")
    def _backup_set_is_unambiguous(self) -> "ProductInventoryPayload":
        ids = [item.product_id for item in self.backup_products]
        if len(ids) != len(set(ids)) or self.sold_out_product_id in ids:
            raise ValueError("backup product identities must be unique")
        return self


class PlanEvidencePayload(DecisionSupportFrozenModel):
    """根计划或售罄紧急 child 的版本、lineage 和对账状态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_type: Literal["PLAN"] = "PLAN"
    captured_at: datetime
    plan_run_id: str = Field(..., min_length=1)
    root_plan_run_id: str = Field(..., min_length=1)
    parent_plan_run_id: str | None = Field(default=None, min_length=1)
    trigger_event_id: str | None = Field(default=None, min_length=1)
    plan_kind: PlanRunKind
    plan_state: PlanRunState
    plan_version: int = Field(..., ge=1, strict=True)
    reconciliation_required: bool = Field(..., strict=True)
    side_effect_unknown: bool = Field(..., strict=True)

    @field_validator("captured_at")
    @classmethod
    def _normalize_captured_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "plan captured_at")


class DanmakuTopicEvidence(DecisionSupportFrozenModel):
    """弹幕聚合中的一个脱敏主题，不保存完整原始消息流。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]*$")
    summary: str = Field(..., min_length=1, max_length=160)
    count: int = Field(..., ge=1, strict=True)
    sample_hashes: tuple[str, ...] = Field(default=(), max_length=3)

    @field_validator("sample_hashes")
    @classmethod
    def _sample_hashes_are_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in value):
            raise ValueError("sample_hashes must contain SHA-256 values")
        if len(value) != len(set(value)):
            raise ValueError("sample_hashes cannot contain duplicates")
        return value

    @field_validator("summary")
    @classmethod
    def _summary_is_trimmed(cls, value: str) -> str:
        if value != value.strip() or any(
            unicodedata.category(char).startswith("C") for char in value
        ):
            raise ValueError("summary contains unsafe control text")
        return value

    @model_validator(mode="after")
    def _summary_matches_category_template(self) -> "DanmakuTopicEvidence":
        """只接受确定性主题模板，阻断自由文本和提示注入进入模型上下文。"""

        expected = _DANMAKU_TOPIC_SUMMARIES.get(self.category)
        if expected is None or self.summary != expected:
            raise ValueError("summary must match deterministic category template")
        return self


class DanmakuAggregatePayload(DecisionSupportFrozenModel):
    """持久化聚合 Provider 返回的版本化弹幕窗口。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_type: Literal["DANMAKU_AGGREGATE"] = "DANMAKU_AGGREGATE"
    aggregate_id: str = Field(..., min_length=1)
    window_start: datetime
    window_end: datetime
    noise_level: DanmakuNoiseLevel
    topics: tuple[DanmakuTopicEvidence, ...] = Field(
        ..., min_length=1, max_length=20
    )

    @field_validator("window_start", "window_end")
    @classmethod
    def _normalize_time(cls, value: datetime) -> datetime:
        return _aware_utc(value, "danmaku window")

    @model_validator(mode="after")
    def _window_is_ordered(self) -> "DanmakuAggregatePayload":
        if self.window_end <= self.window_start:
            raise ValueError("danmaku window must be ordered")
        return self


class AnchorRhythmPayload(DecisionSupportFrozenModel):
    """独立只读 Provider 产出的主播节奏窗口，不从弹幕文本临时猜测。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_type: Literal["RHYTHM_SIGNAL"] = "RHYTHM_SIGNAL"
    signal_id: str = Field(..., min_length=1)
    window_start: datetime
    window_end: datetime
    signal_kind: RhythmSignalKind
    pace_score: int = Field(..., ge=0, le=100, strict=True)

    @field_validator("window_start", "window_end")
    @classmethod
    def _normalize_time(cls, value: datetime) -> datetime:
        return _aware_utc(value, "rhythm window")

    @model_validator(mode="after")
    def _window_is_ordered(self) -> "AnchorRhythmPayload":
        if self.window_end <= self.window_start:
            raise ValueError("rhythm window must be ordered")
        return self


EvidencePayload = Annotated[
    VerifiedEventPayload
    | ProductInventoryPayload
    | PlanEvidencePayload
    | DanmakuAggregatePayload
    | AnchorRhythmPayload,
    Field(discriminator="payload_type"),
]


def governed_evidence_digest(
    *,
    role: EvidenceRole,
    scope: EvidenceScope,
    evidence_id: str,
    source_version: str,
    observed_version: int,
    observed_at: datetime,
    received_at: datetime,
    payload: EvidencePayload,
) -> str:
    """摘要完整 Resolver envelope，确保同一引用只能对应一组权威事实。"""

    return canonical_json_sha256(
        {
            "role": role.value,
            "scope": scope.model_dump(mode="json"),
            "evidence_id": evidence_id,
            "source_version": source_version,
            "observed_version": observed_version,
            "observed_at": _aware_utc(observed_at, "observed_at").isoformat().replace(
                "+00:00", "Z"
            ),
            "received_at": _aware_utc(received_at, "received_at").isoformat().replace(
                "+00:00", "Z"
            ),
            "payload": payload.model_dump(mode="json"),
        }
    )


class GovernedEvidenceComponent(DecisionSupportFrozenModel):
    """Resolver 返回的完整权威组件；正文摘要必须由本地规范 JSON 重算。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: EvidenceRole
    reference: EvidenceRef
    scope: EvidenceScope
    observed_version: int = Field(..., ge=1, strict=True)
    observed_at: datetime
    received_at: datetime
    payload: EvidencePayload

    @field_validator("observed_at", "received_at")
    @classmethod
    def _normalize_time(cls, value: datetime) -> datetime:
        return _aware_utc(value, "evidence component time")

    @model_validator(mode="after")
    def _close_component_identity(self) -> "GovernedEvidenceComponent":
        expected_payload_type = {
            EvidenceRole.VERIFIED_EVENT: "VERIFIED_EVENT",
            EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT: "PRODUCT_INVENTORY",
            EvidenceRole.ROOT_PLAN_SNAPSHOT: "PLAN",
            EvidenceRole.EMERGENCY_PLAN_SNAPSHOT: "PLAN",
            EvidenceRole.DANMAKU_AGGREGATE: "DANMAKU_AGGREGATE",
            EvidenceRole.RHYTHM_SIGNAL: "RHYTHM_SIGNAL",
        }[self.role]
        if self.payload.payload_type != expected_payload_type:
            raise ValueError("evidence role does not match payload type")
        if self.reference.kind is not ROLE_EVIDENCE_KIND[self.role]:
            raise ValueError("evidence role does not match EvidenceKind")
        if self.reference.room_id != self.scope.room_id:
            raise ValueError("reference room_id does not match component scope")
        if self.reference.anchor_id != self.scope.anchor_id:
            raise ValueError("reference anchor_id does not match component scope")
        if self.received_at < self.observed_at:
            raise ValueError("received_at cannot precede observed_at")
        calculated = governed_evidence_digest(
            role=self.role,
            scope=self.scope,
            evidence_id=self.reference.evidence_id,
            source_version=self.reference.source_version,
            observed_version=self.observed_version,
            observed_at=self.observed_at,
            received_at=self.received_at,
            payload=self.payload,
        )
        if self.reference.digest != calculated:
            raise ValueError("reference digest does not match evidence envelope")
        return self


class RoleEvidenceReference(DecisionSupportFrozenModel):
    """把通用 EvidenceRef 固定到 Phase 14 的一个场景角色。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: EvidenceRole
    reference: EvidenceRef

    @model_validator(mode="after")
    def _kind_matches_role(self) -> "RoleEvidenceReference":
        if self.reference.kind is not ROLE_EVIDENCE_KIND[self.role]:
            raise ValueError("reference kind does not match evidence role")
        return self


class EvidenceAssemblyRequest(DecisionSupportFrozenModel):
    """调用方只能提交父事实 ID 和固定引用，不能自报权威父事实正文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_bundle_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    live_session_id: str = Field(..., min_length=1)
    incident_id: str = Field(..., min_length=1)
    references: tuple[RoleEvidenceReference, ...]

    @model_validator(mode="after")
    def _close_request_scope(self) -> "EvidenceAssemblyRequest":
        roles = [item.role for item in self.references]
        if set(roles) != set(EvidenceRole) or len(roles) != len(EvidenceRole):
            raise ValueError("references must cover exact evidence role whitelist")
        sorted_refs = tuple(
            sorted(self.references, key=lambda item: tuple(EvidenceRole).index(item.role))
        )
        object.__setattr__(self, "references", sorted_refs)
        return self


class IncidentEvidenceBinding(DecisionSupportFrozenModel):
    """Bundle 对不可变 Incident 业务正文的最小可验证绑定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_type: str = Field(..., min_length=1)
    source_ref_ids: tuple[str, ...] = Field(..., min_length=1)
    snapshot_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_incident(cls, incident: Incident) -> "IncidentEvidenceBinding":
        """只保存业务正文摘要，避免 Bundle 复制 Incident 中的完整敏感快照。"""

        return cls(
            incident_type=incident.incident_type,
            source_ref_ids=incident.source_ref_ids,
            snapshot_digest=canonical_json_sha256(incident.snapshot),
        )


class GovernedEvidenceContextResolver:
    """仅按稳定 ID 加载 Workspace/Incident 的启动冻结只读边界。"""

    __slots__ = ("_workspace_loader", "_incident_loader")

    def __init__(
        self,
        *,
        workspace_loader: Callable[[str], LiveSessionWorkspace],
        incident_loader: Callable[[str], Incident],
    ) -> None:
        object.__setattr__(self, "_workspace_loader", workspace_loader)
        object.__setattr__(self, "_incident_loader", incident_loader)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("evidence context resolver is startup-frozen")

    def resolve(
        self, live_session_id: str, incident_id: str
    ) -> tuple[LiveSessionWorkspace, Incident, EvidenceScope]:
        """重验证权威父事实并导出调用方不可覆盖的完整证据作用域。"""

        try:
            workspace = LiveSessionWorkspace.model_validate(
                self._workspace_loader(live_session_id).model_dump(mode="python")
            )
            incident = Incident.model_validate(
                self._incident_loader(incident_id).model_dump(mode="json")
            )
        except Exception as exc:
            raise EvidenceAssemblyError("authoritative parent fact is unavailable") from exc
        if workspace.live_session_id != live_session_id:
            raise EvidenceAssemblyError("workspace identity does not match request")
        if incident.incident_id != incident_id:
            raise EvidenceAssemblyError("incident identity does not match request")
        if incident.live_session_id != workspace.live_session_id:
            raise EvidenceAssemblyError("incident does not belong to workspace")
        if workspace.view is not WorkspaceView.LIVE:
            raise EvidenceAssemblyError("evidence assembly requires Workspace LIVE view")
        if incident.incident_type != "SOLD_OUT_COMPOSITE":
            raise EvidenceAssemblyError("incident_type must be SOLD_OUT_COMPOSITE")
        scope = EvidenceScope(
            live_session_id=workspace.live_session_id,
            incident_id=incident.incident_id,
            room_id=workspace.room_id,
            trace_id=workspace.trace_id,
            anchor_id=workspace.anchor_id,
            root_plan_run_id=workspace.root_plan_run_id,
        )
        return workspace, incident, scope


class EvidenceFreshnessPolicy(DecisionSupportFrozenModel):
    """六个角色的固定 TTL；Bundle 有效期取全部组件最早到期时间。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ttl_seconds: dict[EvidenceRole, int]

    @field_validator("ttl_seconds", mode="after")
    @classmethod
    def _freeze_ttl(
        cls, value: dict[EvidenceRole, int]
    ) -> Mapping[EvidenceRole, int]:
        if set(value) != set(EvidenceRole):
            raise ValueError("freshness policy must cover exact evidence roles")
        if any(type(seconds) is not int or seconds <= 0 for seconds in value.values()):
            raise ValueError("freshness TTL must be a positive integer")
        if dict(value) != dict(DEFAULT_EVIDENCE_TTL_SECONDS):
            raise ValueError("Phase 14 freshness policy is startup-frozen")
        return MappingProxyType(dict(value))

    @field_serializer("ttl_seconds", when_used="json")
    def _serialize_ttl(self, value: Mapping[EvidenceRole, int]) -> dict[str, int]:
        return {role.value: value[role] for role in EvidenceRole}

    @classmethod
    def default(cls) -> "EvidenceFreshnessPolicy":
        """固定播中 TTL；动态配置和热加载不进入 Phase 14。"""

        return cls(
            ttl_seconds=dict(DEFAULT_EVIDENCE_TTL_SECONDS)
        )

    def ttl(self, role: EvidenceRole) -> int:
        return self.ttl_seconds[role]


class EvidenceBundleSnapshot(DecisionSupportFrozenModel):
    """写入 Task 2 EvidenceBundle.snapshot 的版本化确定性内容。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    scope: EvidenceScope
    incident_binding: IncidentEvidenceBinding
    assembled_at: datetime
    valid_until: datetime
    components: tuple[GovernedEvidenceComponent, ...]
    proposal_eligible: bool
    blocking_reasons: tuple[str, ...] = ()
    bundle_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @field_validator("assembled_at", "valid_until")
    @classmethod
    def _normalize_time(cls, value: datetime) -> datetime:
        return _aware_utc(value, "bundle time")

    def canonical_payload(self) -> dict[str, Any]:
        """排除摘要自身，返回跨进程稳定的 Bundle 内容。"""

        data = self.model_dump(mode="json")
        data.pop("bundle_digest", None)
        return data

    @model_validator(mode="after")
    def _digest_and_shape_are_valid(self) -> "EvidenceBundleSnapshot":
        if self.valid_until <= self.assembled_at:
            raise ValueError("bundle is already stale")
        if tuple(item.role for item in self.components) != tuple(EvidenceRole):
            raise ValueError("bundle components are not in canonical role order")
        if any(item.scope != self.scope for item in self.components):
            raise ValueError("component scope does not match bundle scope")
        expected_valid_until = min(
            item.observed_at
            + timedelta(seconds=DEFAULT_EVIDENCE_TTL_SECONDS[item.role])
            for item in self.components
        )
        if self.valid_until != expected_valid_until:
            raise ValueError("valid_until does not match component freshness TTL")
        if any(
            item.observed_at > self.assembled_at
            or item.received_at > self.assembled_at
            for item in self.components
        ):
            raise ValueError("bundle contains future evidence")
        if self.proposal_eligible == bool(self.blocking_reasons):
            raise ValueError("proposal eligibility does not match blocking reasons")
        if self.bundle_digest != canonical_json_sha256(self.canonical_payload()):
            raise ValueError("bundle digest does not match canonical snapshot")
        return self


class ReadOnlyEvidenceResolver(Protocol):
    """单角色最小只读 Port；没有 Store 扫描、SQL、Skill 或写方法。"""

    role: EvidenceRole

    def resolve(
        self, reference: EvidenceRef, *, context: EvidenceScope
    ) -> GovernedEvidenceComponent:
        """按显式引用返回一份权威冻结组件。"""


class GovernedReadOnlyEvidenceResolver:
    """把单 ID 只读 loader 封装成唯一可注册 Resolver，隐藏底层 Store。"""

    __slots__ = ("_loader", "resolver_id", "resolver_version", "role")

    def __init__(
        self,
        *,
        resolver_id: str,
        resolver_version: str,
        role: EvidenceRole,
        loader: Callable[[str], GovernedEvidenceComponent | None],
    ) -> None:
        if not callable(loader):
            raise TypeError("evidence loader must be callable")
        if not resolver_id or resolver_id != resolver_id.strip():
            raise ValueError("resolver_id must be a non-blank stable identity")
        if not re.fullmatch(r"\d+\.\d+\.\d+", resolver_version):
            raise ValueError("resolver_version must be semantic version")
        object.__setattr__(self, "resolver_id", resolver_id)
        object.__setattr__(self, "resolver_version", resolver_version)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "_loader", loader)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("governed read-only resolver identity is frozen")

    def resolve(
        self, reference: EvidenceRef, *, context: EvidenceScope
    ) -> GovernedEvidenceComponent:
        """只向 loader 传稳定 ID；scope 由 Registry 在返回后统一校验。"""

        del context
        component = self._loader(reference.evidence_id)
        if component is None:
            raise EvidenceAssemblyError("evidence_id not found")
        return component


class LiveEvidenceResolverRegistry:
    """启动冻结的六角色白名单，不接受通用 Store 或动态 kind。"""

    __slots__ = ("_resolvers", "resolver_manifest")

    def __init__(
        self, resolvers: Mapping[EvidenceRole, ReadOnlyEvidenceResolver]
    ) -> None:
        if set(resolvers) != set(EvidenceRole):
            raise EvidenceAssemblyError("resolver set must cover exact role whitelist")
        for role, resolver in resolvers.items():
            if type(resolver) is not GovernedReadOnlyEvidenceResolver:
                raise EvidenceAssemblyError(
                    "registry requires governed read-only resolver"
                )
            if resolver.role is not role:
                raise EvidenceAssemblyError("resolver role does not match registry key")
        identities = [
            (resolver.resolver_id, resolver.resolver_version)
            for resolver in resolvers.values()
        ]
        if len(identities) != len(set(identities)):
            raise EvidenceAssemblyError("resolver identities must be unique")
        object.__setattr__(self, "_resolvers", MappingProxyType(dict(resolvers)))
        object.__setattr__(
            self,
            "resolver_manifest",
            tuple(
                (role.value, resolvers[role].resolver_id, resolvers[role].resolver_version)
                for role in EvidenceRole
            ),
        )

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("live evidence resolver registry is startup-frozen")

    def resolve_all(
        self,
        references: tuple[RoleEvidenceReference, ...],
        *,
        context: EvidenceScope,
    ) -> tuple[GovernedEvidenceComponent, ...]:
        """按规范角色顺序解析；任一失败时不返回部分证据。"""

        resolved: list[GovernedEvidenceComponent] = []
        for item in references:
            try:
                raw = self._resolvers[item.role].resolve(
                    item.reference, context=context
                )
                component = GovernedEvidenceComponent.model_validate(
                    raw.model_dump(mode="json")
                )
            except EvidenceAssemblyError:
                raise
            except Exception as exc:
                raise EvidenceAssemblyError(
                    f"resolver failed for {item.role.value}"
                ) from exc
            if component.role is not item.role:
                raise EvidenceAssemblyError("resolver returned mismatched role")
            if component.reference != item.reference:
                for field in (
                    "kind",
                    "evidence_id",
                    "source_version",
                    "digest",
                    "room_id",
                    "anchor_id",
                ):
                    if getattr(component.reference, field) != getattr(
                        item.reference, field
                    ):
                        raise EvidenceAssemblyError(
                            f"resolved {field} does not match requested reference"
                        )
                raise EvidenceAssemblyError("resolved reference does not match request")
            self._require_scope(component.scope, context)
            resolved.append(component)
        return tuple(resolved)

    @staticmethod
    def _require_scope(actual: EvidenceScope, expected: EvidenceScope) -> None:
        for field in (
            "live_session_id",
            "incident_id",
            "room_id",
            "trace_id",
            "anchor_id",
            "root_plan_run_id",
        ):
            if getattr(actual, field) != getattr(expected, field):
                raise EvidenceAssemblyError(f"{field} does not match evidence scope")


class EvidenceBundleAssembler:
    """聚合六个权威只读来源并生成可持久化、可重放的 EvidenceBundle。"""

    __slots__ = ("_context_resolver", "_registry", "_freshness", "_clock")

    def __init__(
        self,
        *,
        context_resolver: GovernedEvidenceContextResolver,
        registry: LiveEvidenceResolverRegistry,
        freshness_policy: EvidenceFreshnessPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        if type(context_resolver) is not GovernedEvidenceContextResolver:
            raise EvidenceAssemblyError(
                "context resolver must be a governed exact type"
            )
        # 三项依赖均属于启动时冻结配置。若允许运行期替换时钟、TTL 或
        # Resolver，调用方就可能绕过新鲜度和只读来源门禁。
        object.__setattr__(self, "_context_resolver", context_resolver)
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(
            self,
            "_freshness",
            EvidenceFreshnessPolicy.model_validate(
                freshness_policy.model_dump(mode="json")
            ),
        )
        object.__setattr__(self, "_clock", clock)

    def __setattr__(self, _name: str, _value: Any) -> None:
        """拒绝构造后的依赖替换，维持启动冻结的安全边界。"""

        raise TypeError("evidence bundle assembler is startup-frozen")

    def _assemble_bundle(self, request: EvidenceAssemblyRequest) -> EvidenceBundle:
        """一次性闭合摘要、scope、时间、版本、lineage 和对账风险。"""

        try:
            validated = EvidenceAssemblyRequest.model_validate(
                request.model_dump(mode="json")
            )
            as_of = _aware_utc(self._clock(), "assembler clock")
        except EvidenceAssemblyError:
            raise
        except Exception as exc:
            raise EvidenceAssemblyError("request or trusted clock is invalid") from exc
        workspace, incident, scope = self._context_resolver.resolve(
            validated.live_session_id, validated.incident_id
        )
        components = self._registry.resolve_all(
            validated.references, context=scope
        )
        valid_until = self._validate_freshness(components, as_of)
        # 可信时钟只回答“现在能否使用这些证据”，不能进入持久化事实。
        # 使用组件最大接收时间作为确定性事实时间，使相同引用在 TTL 内
        # 重试时生成字节一致的 Bundle，同时仍拒绝未来或过期证据。
        evidence_time = max(item.received_at for item in components)
        by_role = {item.role: item for item in components}
        event = self._validate_event(
            workspace, incident, by_role[EvidenceRole.VERIFIED_EVENT]
        )
        self._validate_inventory(
            event, by_role[EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT]
        )
        self._validate_plans(
            workspace,
            event,
            by_role[EvidenceRole.ROOT_PLAN_SNAPSHOT],
            by_role[EvidenceRole.EMERGENCY_PLAN_SNAPSHOT],
        )
        self._validate_windows(
            by_role[EvidenceRole.DANMAKU_AGGREGATE],
            by_role[EvidenceRole.RHYTHM_SIGNAL],
        )
        blocking = self._blocking_reasons(by_role)
        snapshot_data = {
            "schema_version": "1.0.0",
            "scope": scope.model_dump(mode="json"),
            "incident_binding": IncidentEvidenceBinding.from_incident(
                incident
            ).model_dump(mode="json"),
            "assembled_at": evidence_time.isoformat().replace("+00:00", "Z"),
            "valid_until": valid_until.isoformat().replace("+00:00", "Z"),
            "components": [item.model_dump(mode="json") for item in components],
            "proposal_eligible": not blocking,
            "blocking_reasons": list(blocking),
        }
        snapshot = EvidenceBundleSnapshot(
            **snapshot_data,
            bundle_digest=canonical_json_sha256(snapshot_data),
        )
        plain_snapshot = snapshot.model_dump(mode="json")
        bundle = EvidenceBundle(
            evidence_bundle_id=validated.evidence_bundle_id,
            live_session_id=workspace.live_session_id,
            incident_id=incident.incident_id,
            idempotency_key=validated.idempotency_key,
            evidence_ref_ids=tuple(
                item.reference.evidence_id for item in components
            ),
            snapshot=plain_snapshot,
            input_fingerprint=canonical_json_sha256(plain_snapshot),
            created_at=evidence_time,
        )
        return bundle

    def _validate_freshness(
        self,
        components: tuple[GovernedEvidenceComponent, ...],
        as_of: datetime,
    ) -> datetime:
        expirations: list[datetime] = []
        for component in components:
            if component.observed_at > as_of or component.received_at > as_of:
                raise EvidenceAssemblyError(
                    f"{component.role.value} evidence is from the future"
                )
            expires_at = component.observed_at + timedelta(
                seconds=self._freshness.ttl(component.role)
            )
            if expires_at <= as_of:
                raise EvidenceAssemblyError(
                    f"{component.role.value} evidence is stale"
                )
            expirations.append(expires_at)
        return min(expirations)

    @staticmethod
    def _validate_event(
        workspace: LiveSessionWorkspace,
        incident: Incident,
        component: GovernedEvidenceComponent,
    ) -> VerifiedEventPayload:
        payload = component.payload
        if not isinstance(payload, VerifiedEventPayload):
            raise EvidenceAssemblyError("verified event payload type is invalid")
        try:
            _build_event_authorization_context(payload.event, payload.provenance)
        except Exception as exc:
            raise EvidenceAssemblyError("event provenance does not close") from exc
        expected_product = incident.snapshot.get("product_id")
        expected_version = incident.snapshot.get("expected_version")
        if (
            payload.event.event_id not in incident.source_ref_ids
            or payload.event.room_id != workspace.room_id
            or payload.event.product_id != expected_product
            or payload.event.observed_version != expected_version
            or component.reference.evidence_id != payload.event.event_id
            or component.observed_version != payload.event.observed_version
        ):
            raise EvidenceAssemblyError("event identity or incident binding is invalid")
        if (
            component.observed_at != payload.event.occurred_at
            or component.received_at != payload.provenance.received_at
        ):
            raise EvidenceAssemblyError("event source time does not match component")
        normal = (
            payload.inbox_state is EventInboxState.APPLIED
            and payload.application_state is EventApplicationState.APPLIED
            and payload.side_effect_state is SideEffectState.CONFIRMED
            and payload.applied_plan_version is not None
        )
        reconciling = (
            payload.inbox_state is EventInboxState.WAITING_HUMAN
            and payload.application_state
            is EventApplicationState.WAITING_RECONCILIATION
            and payload.side_effect_state is SideEffectState.UNKNOWN
            and payload.applied_plan_version is None
        )
        if not normal and not reconciling:
            raise EvidenceAssemblyError("event state is not decision-support eligible")
        if not payload.emergency_plan_run_id:
            raise EvidenceAssemblyError("event application lacks emergency plan lineage")
        return payload

    @staticmethod
    def _validate_inventory(
        event: VerifiedEventPayload,
        component: GovernedEvidenceComponent,
    ) -> None:
        payload = component.payload
        if not isinstance(payload, ProductInventoryPayload):
            raise EvidenceAssemblyError("product inventory payload type is invalid")
        product_id = event.event.product_id
        if (
            payload.sold_out_product_id != product_id
            or payload.planned_product.product_id != product_id
            or payload.current_product.product_id != product_id
            or payload.expected_version != event.event.observed_version
        ):
            raise EvidenceAssemblyError("product identity does not match sold-out event")
        if (
            payload.current_product.version < event.event.observed_version
            or component.observed_version != payload.current_product.version
        ):
            raise EvidenceAssemblyError("inventory version is older than sold-out event")
        if payload.planned_product.version > payload.current_product.version:
            raise EvidenceAssemblyError(
                "planned product version cannot exceed current inventory version"
            )
        if component.observed_at != payload.captured_at:
            raise EvidenceAssemblyError("product source time does not match component")
        if payload.current_product.inventory != 0 or payload.current_product.is_active:
            raise EvidenceAssemblyError("current product is not a confirmed sold-out snapshot")
        if any(not item.is_active or item.inventory <= 0 for item in payload.backup_products):
            raise EvidenceAssemblyError("backup product is not currently available")

    @staticmethod
    def _validate_plans(
        workspace: LiveSessionWorkspace,
        event: VerifiedEventPayload,
        root_component: GovernedEvidenceComponent,
        emergency_component: GovernedEvidenceComponent,
    ) -> None:
        root = root_component.payload
        emergency = emergency_component.payload
        if not isinstance(root, PlanEvidencePayload) or not isinstance(
            emergency, PlanEvidencePayload
        ):
            raise EvidenceAssemblyError("plan payload type is invalid")
        root_id = workspace.root_plan_run_id
        if (
            root.plan_kind is not PlanRunKind.CARD_BATCH
            or root.plan_run_id != root_id
            or root.root_plan_run_id != root_id
            or root.parent_plan_run_id is not None
            or root.trigger_event_id is not None
            or root_component.observed_version != root.plan_version
        ):
            raise EvidenceAssemblyError("root plan lineage is invalid")
        if root_component.observed_at != root.captured_at:
            raise EvidenceAssemblyError("root plan source time does not match component")
        if root.plan_state is not PlanRunState.FROZEN:
            raise EvidenceAssemblyError("root plan state must remain FROZEN")
        if (
            event.application_state is EventApplicationState.APPLIED
            and event.applied_plan_version != root.plan_version
        ):
            raise EvidenceAssemblyError(
                "applied plan version does not match root PlanVersion"
            )
        if (
            emergency.plan_kind is not PlanRunKind.EMERGENCY_SOLD_OUT
            or emergency.plan_run_id != event.emergency_plan_run_id
            or emergency.root_plan_run_id != root_id
            or emergency.parent_plan_run_id != root_id
            or emergency.trigger_event_id != event.event.event_id
            or emergency_component.observed_version != emergency.plan_version
        ):
            raise EvidenceAssemblyError("emergency plan lineage is invalid")
        if emergency_component.observed_at != emergency.captured_at:
            raise EvidenceAssemblyError(
                "emergency plan source time does not match component"
            )
        if (
            event.application_state is EventApplicationState.APPLIED
            and emergency.plan_state is not PlanRunState.SUCCEEDED
        ):
            raise EvidenceAssemblyError(
                "emergency plan state must be SUCCEEDED for applied event"
            )
        if (
            event.application_state is EventApplicationState.WAITING_RECONCILIATION
            and emergency.plan_state is not PlanRunState.FROZEN
        ):
            raise EvidenceAssemblyError(
                "emergency plan state must be FROZEN during reconciliation"
            )

    @staticmethod
    def _validate_windows(
        danmaku_component: GovernedEvidenceComponent,
        rhythm_component: GovernedEvidenceComponent,
    ) -> None:
        danmaku = danmaku_component.payload
        rhythm = rhythm_component.payload
        if not isinstance(danmaku, DanmakuAggregatePayload) or not isinstance(
            rhythm, AnchorRhythmPayload
        ):
            raise EvidenceAssemblyError("live signal payload type is invalid")
        if danmaku.aggregate_id != danmaku_component.reference.evidence_id:
            raise EvidenceAssemblyError("danmaku aggregate identity is invalid")
        if rhythm.signal_id != rhythm_component.reference.evidence_id:
            raise EvidenceAssemblyError("rhythm signal identity is invalid")
        if danmaku_component.observed_at != danmaku.window_end:
            raise EvidenceAssemblyError("danmaku source time does not match component")
        if rhythm_component.observed_at != rhythm.window_end:
            raise EvidenceAssemblyError("rhythm source time does not match component")
        if max(danmaku.window_start, rhythm.window_start) >= min(
            danmaku.window_end, rhythm.window_end
        ):
            raise EvidenceAssemblyError("danmaku and rhythm windows do not overlap")

    @staticmethod
    def _blocking_reasons(
        by_role: Mapping[EvidenceRole, GovernedEvidenceComponent],
    ) -> tuple[str, ...]:
        event = by_role[EvidenceRole.VERIFIED_EVENT].payload
        root = by_role[EvidenceRole.ROOT_PLAN_SNAPSHOT].payload
        emergency = by_role[EvidenceRole.EMERGENCY_PLAN_SNAPSHOT].payload
        waiting = (
            isinstance(event, VerifiedEventPayload)
            and event.application_state
            is EventApplicationState.WAITING_RECONCILIATION
        ) or any(
            isinstance(plan, PlanEvidencePayload)
            and (plan.reconciliation_required or plan.side_effect_unknown)
            for plan in (root, emergency)
        )
        return ("WAITING_RECONCILIATION",) if waiting else ()


def _install_governed_receipt_api() -> Callable[[object], EvidenceBundle]:
    """把签发权封存在 Assembler 方法闭包，Store 只获得不可反向签发的校验器。"""

    # 进程内弱键映射是能力账本而不是持久化业务状态：它同时记录某个 receipt
    # 确实由受治理 Assembler 产出，并绑定其签发时的原始 Bundle 对象身份。
    # 重启后的 Bundle 走 Store 重放读取，不会把序列化对象重新解释为有效 receipt。
    issued_receipts: WeakKeyDictionary[AssembledEvidenceBundle, EvidenceBundle] = (
        WeakKeyDictionary()
    )
    receipt_lock = RLock()

    def assemble(
        assembler: EvidenceBundleAssembler,
        request: EvidenceAssemblyRequest,
    ) -> AssembledEvidenceBundle:
        """在所有权威解析完成后签发正常调用路径不可伪造的进程内写入能力。"""

        bundle = assembler._assemble_bundle(request)
        receipt = object.__new__(AssembledEvidenceBundle)
        object.__setattr__(receipt, "_bundle", bundle)
        with receipt_lock:
            issued_receipts[receipt] = bundle
        return receipt

    def require_governed_receipt(fact: object) -> EvidenceBundle:
        """验证 receipt 的实际签发身份，拒绝同类型布局伪造和裸 Bundle。"""

        with receipt_lock:
            if type(fact) is not AssembledEvidenceBundle:
                raise TypeError("evidence requires governed assembly receipt")
            issued_bundle = issued_receipts.get(fact)
            # 不信任 wrapper 的私有字段：底层反射即使重绑 `_bundle`，也必须
            # 因签发账本中的对象身份不一致而 fail-closed，不能借用旧 receipt。
            if issued_bundle is None or fact.bundle is not issued_bundle:
                raise TypeError("evidence requires governed assembly receipt")
            return issued_bundle

    # `assemble` 捕获唯一的签发账本而不暴露签发函数名；调用方只能调用公开
    # Assembler 完成完整的六角色权威读取，不能把自造 Bundle 包装成 receipt。
    EvidenceBundleAssembler.assemble = assemble  # type: ignore[attr-defined]
    return require_governed_receipt


_require_governed_evidence_receipt = _install_governed_receipt_api()


class EvidenceBundleAssemblyService:
    """把外部 EvidenceRef 请求收敛为受控汇聚和追加，避免暴露 receipt 或 Store。"""

    __slots__ = ("_assembler", "_writer")

    def __init__(
        self,
        *,
        assembler: EvidenceBundleAssembler,
        writer: EvidenceBundlePersistencePort,
    ) -> None:
        if type(assembler) is not EvidenceBundleAssembler:
            raise EvidenceAssemblyError("assembler must be the governed exact type")
        if not callable(getattr(writer, "append_evidence_bundle", None)):
            raise EvidenceAssemblyError("writer must expose governed evidence persistence")
        # 依赖仅在应用组合时注入一次。运行时调用者只能提交严格请求，既无法
        # 替换只读 Resolver，也无法取得 receipt 或完整数据库访问能力。
        object.__setattr__(self, "_assembler", assembler)
        object.__setattr__(self, "_writer", writer)

    def __setattr__(self, _name: str, _value: Any) -> None:
        """禁止运行期替换汇聚器或持久化端口，维持启动冻结边界。"""

        raise TypeError("evidence bundle assembly service is startup-frozen")

    def assemble_and_append(
        self,
        request: EvidenceAssemblyRequest,
        *,
        expected_workspace_version: int,
    ) -> LiveSessionWorkspace:
        """内部签发 receipt 后立刻追加，公开调用面始终只有严格请求与 CAS 输入。"""

        receipt = self._assembler.assemble(request)
        return self._writer.append_evidence_bundle(
            receipt,
            expected_workspace_version=expected_workspace_version,
        )
