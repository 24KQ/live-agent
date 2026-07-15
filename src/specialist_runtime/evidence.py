"""Phase 13 权威 EvidenceRef 解析与作用域校验。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Callable, Protocol

from pydantic import ConfigDict, Field, field_serializer, field_validator

from src.specialist_runtime.models import (
    EvidenceKind,
    EvidenceRef,
    StrictFrozenModel,
    _freeze_json,
    _plain_json,
)


class EvidenceResolutionError(RuntimeError):
    """EvidenceRef 无法解析或与权威事实不一致。"""


class ResolvedEvidence(StrictFrozenModel):
    """Store loader 返回的权威证据快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EvidenceKind
    evidence_id: str = Field(..., min_length=1)
    source_version: str = Field(..., min_length=1)
    digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    anchor_id: str | None = Field(default=None, min_length=1)
    room_id: str | None = Field(default=None, min_length=1)
    payload: Any

    @field_validator("payload", mode="after")
    @classmethod
    def _freeze_payload(cls, value: Any) -> Any:
        return _freeze_json(value)

    @field_serializer("payload", when_used="json")
    def _serialize_payload(self, value: Any) -> Any:
        return _plain_json(value)


class EvidenceLoader(Protocol):
    """某一权威 Store 的最小只读查询接口。"""

    def load(self, evidence_id: str) -> ResolvedEvidence | None:
        """按稳定 ID 返回当前权威事实。"""


class ProjectedStoreEvidenceLoader:
    """把某个权威 Store 的只读记录投影成统一 Evidence，不缓存业务事实。"""

    kind: EvidenceKind

    def __init__(
        self,
        *,
        getter: Callable[[str], Any | None],
        projector: Callable[[Any], ResolvedEvidence],
    ) -> None:
        self._getter = getter
        self._projector = projector

    def load(self, evidence_id: str) -> ResolvedEvidence | None:
        record = self._getter(evidence_id)
        if record is None:
            return None
        resolved = self._projector(record)
        if resolved.kind is not self.kind or resolved.evidence_id != evidence_id:
            raise EvidenceResolutionError("Store projector returned mismatched evidence identity")
        return resolved


class EventStoreEvidenceLoader(ProjectedStoreEvidenceLoader):
    kind = EvidenceKind.EVENT


class PlanStoreEvidenceLoader(ProjectedStoreEvidenceLoader):
    kind = EvidenceKind.PLAN


class PlanNodeStoreEvidenceLoader(ProjectedStoreEvidenceLoader):
    kind = EvidenceKind.PLAN_NODE


class SkillAttemptStoreEvidenceLoader(ProjectedStoreEvidenceLoader):
    kind = EvidenceKind.SKILL_ATTEMPT


class AuditStoreEvidenceLoader(ProjectedStoreEvidenceLoader):
    kind = EvidenceKind.AUDIT


class ReplayStoreEvidenceLoader(ProjectedStoreEvidenceLoader):
    kind = EvidenceKind.REPLAY


class MemoryStoreEvidenceLoader(ProjectedStoreEvidenceLoader):
    kind = EvidenceKind.MEMORY


class EvaluationStoreEvidenceLoader(ProjectedStoreEvidenceLoader):
    kind = EvidenceKind.EVALUATION


class EvidenceResolverRegistry:
    """为八类 EvidenceKind 固定 loader，并执行统一交叉校验。"""

    def __init__(self, loaders: Mapping[EvidenceKind, EvidenceLoader]) -> None:
        missing = set(EvidenceKind) - set(loaders)
        extra = set(loaders) - set(EvidenceKind)
        if missing or extra:
            raise EvidenceResolutionError("Evidence loader set must cover every EvidenceKind")
        self._loaders = MappingProxyType(dict(loaders))

    def resolve(
        self,
        reference: EvidenceRef,
        *,
        expected_room_id: str,
        expected_anchor_id: str | None,
    ) -> ResolvedEvidence:
        """核对来源、版本、摘要和 room/anchor 作用域，任一不符即拒绝。"""

        resolved = self._loaders[reference.kind].load(reference.evidence_id)
        if resolved is None:
            raise EvidenceResolutionError("evidence_id not found")
        checks = {
            "kind": resolved.kind is reference.kind,
            "evidence_id": resolved.evidence_id == reference.evidence_id,
            "source_version": resolved.source_version == reference.source_version,
            "digest": resolved.digest == reference.digest,
            # AgentTask 始终是 room-scoped；权威事实和引用都不能用 None 绕过作用域。
            "room_id": resolved.room_id == reference.room_id == expected_room_id,
            "anchor_id": (
                resolved.anchor_id == reference.anchor_id == expected_anchor_id
                if expected_anchor_id is not None
                or resolved.anchor_id is not None
                or reference.anchor_id is not None
                else True
            ),
        }
        failed = next((field for field, valid in checks.items() if not valid), None)
        if failed is not None:
            raise EvidenceResolutionError(f"{failed} does not match authoritative evidence")
        return resolved

    def resolve_many(
        self,
        references: tuple[EvidenceRef, ...],
        *,
        expected_room_id: str,
        expected_anchor_id: str | None,
    ) -> tuple[ResolvedEvidence, ...]:
        """保持输入顺序解析全部引用；任何一个失败时不返回部分结果。"""

        return tuple(
            self.resolve(
                reference,
                expected_room_id=expected_room_id,
                expected_anchor_id=expected_anchor_id,
            )
            for reference in references
        )
