"""Phase 12B 售罄事件的确定性影响分析。

Analyzer 只读取权威 Inbox、当前 PlanRun 和 Store 返回的冻结节点快照。PRODUCT 范围
来自精确资源键匹配并扩展到下游依赖闭包；冲突或无法证明商品边界时提升 ROOM；
只有受控平台 FailureFact 才能提升 PLATFORM。任何 scope 都不读取 LLM 输出。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.plan_engine.event_state_machine import EventInboxState
from src.plan_engine.event_store import EventInboxRecord
from src.plan_engine.events import ImpactScope, canonical_json_sha256
from src.plan_engine.models import PlanNodeView, PlanRunState, PlanRunView
from src.skill_runtime.models import FailureCategory, FailureFact, SideEffectState


class ImpactAnalysisError(ValueError):
    """输入事实不足、跨计划或试图伪造 scope 时 fail-closed。"""


class ImpactAnalysis(BaseModel):
    """可持久化、可复算摘要的影响分析结果。"""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(..., min_length=1)
    event_payload_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    plan_run_id: str = Field(..., min_length=1)
    plan_version: int = Field(..., ge=1, strict=True)
    scope: ImpactScope
    affected_logical_keys: tuple[str, ...]
    affected_node_ids: tuple[str, ...]
    resource_keys: tuple[str, ...]
    reason_codes: tuple[str, ...]
    platform_failure_code: str | None = Field(default=None, min_length=1)
    analysis_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "affected_logical_keys",
        "affected_node_ids",
        "resource_keys",
        "reason_codes",
    )
    @classmethod
    def _tuples_are_sorted_unique(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        """分析集合统一按字典序冻结，消除数据库返回顺序对摘要的影响。"""
        normalized = tuple(value)
        if any(not item for item in normalized):
            raise ValueError("ImpactAnalysis 集合不能包含空字符串")
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("ImpactAnalysis 集合必须排序且唯一")
        return normalized

    @model_validator(mode="after")
    def _digest_matches_payload(self) -> "ImpactAnalysis":
        """拒绝调用方修改 scope/节点后沿用旧摘要。"""
        expected = canonical_json_sha256(self.digest_payload())
        if self.analysis_digest != expected:
            raise ValueError("analysis_digest 与规范影响事实不一致")
        return self

    def digest_payload(self) -> dict[str, Any]:
        """返回排除摘要自身后的规范 JSON。"""
        return {
            "event_id": self.event_id,
            "event_payload_digest": self.event_payload_digest,
            "plan_run_id": self.plan_run_id,
            "plan_version": self.plan_version,
            "scope": self.scope.value,
            "affected_logical_keys": list(self.affected_logical_keys),
            "affected_node_ids": list(self.affected_node_ids),
            "resource_keys": list(self.resource_keys),
            "reason_codes": list(self.reason_codes),
            "platform_failure_code": self.platform_failure_code,
        }


class ImpactAnalyzer:
    """从冻结事实推导 PRODUCT/ROOM/PLATFORM 影响范围。"""

    def analyze(
        self,
        *,
        inbox: EventInboxRecord,
        plan_run: PlanRunView,
        nodes: Sequence[PlanNodeView],
        platform_failure: FailureFact | None = None,
    ) -> ImpactAnalysis:
        """校验事实闭合后计算稳定依赖闭包与资源集合。"""
        node_snapshot = tuple(nodes)
        self._validate_inputs(inbox, plan_run, node_snapshot, platform_failure)
        all_logical_keys = {node.logical_key for node in node_snapshot}
        node_by_logical_key = {node.logical_key: node for node in node_snapshot}
        platform_failure_code: str | None = None

        if platform_failure is not None:
            scope = ImpactScope.PLATFORM
            affected_keys = set(all_logical_keys)
            reason_codes = {"PLATFORM_FAILURE_FACT"}
            platform_failure_code = platform_failure.external_code
        elif inbox.state is EventInboxState.CONFLICT:
            scope = ImpactScope.ROOM
            affected_keys = set(all_logical_keys)
            reason_codes = {"EVENT_IDENTITY_CONFLICT"}
        else:
            product_resource = (
                f"room:{plan_run.room_id}:product:{inbox.event.product_id}"
            )
            direct = {
                node.logical_key
                for node in node_snapshot
                if product_resource in node.resource_keys
            }
            if not direct:
                scope = ImpactScope.ROOM
                affected_keys = set(all_logical_keys)
                reason_codes = {"PRODUCT_RESOURCE_UNRESOLVED"}
            else:
                scope = ImpactScope.PRODUCT
                affected_keys = self._dependency_closure(node_snapshot, direct)
                reason_codes = {"SOLD_OUT_PRODUCT_MATCH"}
                if affected_keys != direct:
                    reason_codes.add("DEPENDENCY_CLOSURE")

        affected_nodes = tuple(
            sorted(
                node_by_logical_key[logical_key].node_id
                for logical_key in affected_keys
            )
        )
        resource_keys = tuple(
            sorted(
                {
                    resource_key
                    for logical_key in affected_keys
                    for resource_key in node_by_logical_key[
                        logical_key
                    ].resource_keys
                }
            )
        )
        payload: dict[str, Any] = {
            "event_id": inbox.event.event_id,
            "event_payload_digest": inbox.event.payload_digest,
            "plan_run_id": plan_run.plan_run_id,
            "plan_version": plan_run.current_version,
            "scope": scope,
            "affected_logical_keys": tuple(sorted(affected_keys)),
            "affected_node_ids": affected_nodes,
            "resource_keys": resource_keys,
            "reason_codes": tuple(sorted(reason_codes)),
            "platform_failure_code": platform_failure_code,
        }
        digest_payload = {
            key: (value.value if isinstance(value, ImpactScope) else list(value) if isinstance(value, tuple) else value)
            for key, value in payload.items()
        }
        return ImpactAnalysis(
            **payload,
            analysis_digest=canonical_json_sha256(digest_payload),
        )

    @staticmethod
    def _validate_inputs(
        inbox: EventInboxRecord,
        plan_run: PlanRunView,
        nodes: tuple[PlanNodeView, ...],
        platform_failure: FailureFact | None,
    ) -> None:
        """拒绝跨房间、跨版本、不完整 DAG 和伪平台失败事实。"""
        if plan_run.state not in {PlanRunState.ACTIVE, PlanRunState.FROZEN}:
            raise ImpactAnalysisError("只有活动或已冻结计划可以分析影响")
        if inbox.event.room_id != plan_run.room_id:
            raise ImpactAnalysisError("event room 与 PlanRun room 不一致")
        if inbox.state not in {
            EventInboxState.VERIFIED,
            EventInboxState.PROCESSING,
            EventInboxState.CONFLICT,
        }:
            raise ImpactAnalysisError("EventInbox 状态不允许影响分析")
        if not nodes:
            raise ImpactAnalysisError("当前 PlanVersion 没有节点")
        node_ids = [node.node_id for node in nodes]
        logical_keys = [node.logical_key for node in nodes]
        if len(set(node_ids)) != len(node_ids) or len(set(logical_keys)) != len(logical_keys):
            raise ImpactAnalysisError("PlanNode 身份重复")
        known_keys = set(logical_keys)
        for node in nodes:
            if (
                node.plan_run_id != plan_run.plan_run_id
                or node.version_number != plan_run.current_version
            ):
                raise ImpactAnalysisError("PlanNode 不属于当前 PlanVersion")
            if any(dependency not in known_keys for dependency in node.depends_on):
                raise ImpactAnalysisError("PlanNode 依赖引用不完整")
        if platform_failure is not None and (
            platform_failure.category
            not in {FailureCategory.TRANSIENT_INFRA, FailureCategory.INTERNAL_INVARIANT}
            or platform_failure.side_effect_state is not SideEffectState.NOT_SENT
            or not platform_failure.external_code.startswith("platform.")
        ):
            raise ImpactAnalysisError("FailureFact 不能证明平台级风险")

    @staticmethod
    def _dependency_closure(
        nodes: tuple[PlanNodeView, ...],
        direct: set[str],
    ) -> set[str]:
        """沿显式 depends_on 向下游扩展，直到没有新受影响节点。"""
        closure = set(direct)
        changed = True
        while changed:
            changed = False
            for node in nodes:
                if node.logical_key in closure:
                    continue
                if any(dependency in closure for dependency in node.depends_on):
                    closure.add(node.logical_key)
                    changed = True
        return closure
