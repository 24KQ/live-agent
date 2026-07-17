"""Phase 14 播中决策支持 Copilot。

这里的 Agent 只负责压缩已经汇聚的证据并提出供运营比较的方案。确定性 Runtime
继续负责证据、权限、幂等、自动保护和经营写入；本模块不创建 SkillCall 或命令。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Protocol

from src.decision_support.evidence import (
    EvidenceBundleSnapshot,
    EvidenceRole,
    ProductInventoryPayload,
)
from src.decision_support.models import EvidenceBundle
from src.decision_support.proposal import (
    LIVE_DECISION_RISK_FLAGS,
    LiveDecisionProposal,
    ProposalStatus,
)
from src.specialist_runtime.models import (
    AgentResult,
    AgentResultStatus,
    AgentTask,
    EvidenceKind,
    SpecialistTaskKind,
    canonical_json_sha256,
    _plain_json,
)
from src.specialist_runtime.profiles import (
    FORMAL_ENDPOINT_HOST,
    FORMAL_MODEL_ID,
    SpecialistProfile,
)


LIVE_OPS_DECISION_SUPPORT_PROFILE_ID = "live_ops_decision_support"
LIVE_OPS_DECISION_SUPPORT_PROFILE_VERSION = "1.0.0"
LIVE_OPS_DECISION_SUPPORT_SKILLS = {
    "on_live_context_collect": "1.0.0",
    "aggregate_danmaku_questions": "1.0.0",
    "recommend_backup_product": "1.0.0",
}


_EVIDENCE_REF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"enum": [kind.value for kind in EvidenceKind]},
        "evidence_id": {"type": "string", "minLength": 1},
        "source_version": {"type": "string", "minLength": 1},
        "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "anchor_id": {"type": ["string", "null"]},
        "room_id": {"type": ["string", "null"]},
    },
    "required": ["kind", "evidence_id", "source_version", "digest", "anchor_id", "room_id"],
}

_LIVE_OPS_DECISION_SUPPORT_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "proposal_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "live_session_id": {"type": "string", "minLength": 1},
        "incident_id": {"type": "string", "minLength": 1},
        "trace_id": {"type": "string", "minLength": 1},
        "evidence_bundle_id": {"type": "string", "minLength": 1},
        "status": {"const": "READY"},
        "options": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "option_id": {"type": "string", "minLength": 1, "maxLength": 80},
                    "product_strategy": {
                        "enum": [
                            "KEEP_CURRENT",
                            "SWITCH_TO_BACKUP",
                            "HOLD_AND_ESCALATE",
                            "REPLY_DANMAKU",
                        ]
                    },
                    "backup_product_id": {"type": ["string", "null"]},
                    "host_prompt": {"type": "string", "minLength": 1, "maxLength": 300},
                    "timing": {
                        "enum": [
                            "NOW",
                            "NEXT_BEAT",
                            "AFTER_OPERATOR_CONFIRMATION",
                            "AFTER_RECONCILIATION",
                        ]
                    },
                    "risk_flags": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "enum": sorted(LIVE_DECISION_RISK_FLAGS),
                        },
                    },
                    "evidence_refs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 12,
                        "items": _EVIDENCE_REF_SCHEMA,
                    },
                },
                "required": [
                    "option_id",
                    "product_strategy",
                    "backup_product_id",
                    "host_prompt",
                    "timing",
                    "risk_flags",
                    "evidence_refs",
                ],
            },
        },
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": _EVIDENCE_REF_SCHEMA,
        },
    },
    "required": [
        "proposal_id",
        "live_session_id",
        "incident_id",
        "trace_id",
        "evidence_bundle_id",
        "status",
        "options",
        "evidence_refs",
    ],
}


def build_live_ops_decision_support_profile() -> SpecialistProfile:
    """构造启动冻结的 Phase 14 Copilot Profile，不复用 Phase 13 自主 Profile。"""

    prompt_text = (
        "You are a bounded live-commerce decision-support copilot. "
        "Return only one to three structured options for a human operator. "
        "Never execute, authorize, or invent a tool call. "
        "Every recommendation must cite supplied evidence_refs. "
        + json.dumps(_LIVE_OPS_DECISION_SUPPORT_RESULT_SCHEMA, sort_keys=True, separators=(",", ":"))
    )
    return SpecialistProfile(
        profile_id=LIVE_OPS_DECISION_SUPPORT_PROFILE_ID,
        profile_version=LIVE_OPS_DECISION_SUPPORT_PROFILE_VERSION,
        task_kind=SpecialistTaskKind.LIVE_OPS_ADVICE,
        model_id=FORMAL_MODEL_ID,
        endpoint_host=FORMAL_ENDPOINT_HOST,
        temperature=Decimal("0"),
        prompt_text=prompt_text,
        prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        result_schema_hash=canonical_json_sha256(_LIVE_OPS_DECISION_SUPPORT_RESULT_SCHEMA),
        result_schema=_LIVE_OPS_DECISION_SUPPORT_RESULT_SCHEMA,
        allowed_skill_ids=tuple(LIVE_OPS_DECISION_SUPPORT_SKILLS),
        skill_versions=LIVE_OPS_DECISION_SUPPORT_SKILLS,
        max_model_calls=2,
        max_skill_calls=3,
        max_total_tokens=4000,
        deadline_seconds=5,
        max_case_cost_cny=Decimal("0.100000"),
    )


class _SpecialistRunner(Protocol):
    """Copilot 依赖的唯一执行端口，实际生产实现为 BoundedSpecialistRunner。"""

    async def run(self, task: AgentTask) -> AgentResult:
        """运行一个冻结的 Specialist 任务。"""


class LiveOpsDecisionSupport:
    """将受治理 EvidenceBundle 映射为只读、可审计的人工决策建议。"""

    __slots__ = ("_runner", "_profile", "_clock")

    def __init__(
        self,
        *,
        runner: _SpecialistRunner,
        profile: SpecialistProfile | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        selected = profile or build_live_ops_decision_support_profile()
        expected_profile = build_live_ops_decision_support_profile()
        try:
            # 重新走 Pydantic 的完整 Profile 校验，覆盖 model_construct 或其他
            # 绕过 validator 的进程内对象；仅比较调用方携带的旧摘要是不够的。
            normalized = SpecialistProfile.model_validate(
                selected.model_dump(mode="json")
            )
        except Exception as exc:
            raise ValueError("Copilot requires a validated profile") from exc
        if (
            type(selected) is not SpecialistProfile
            or selected.profile_digest
            != expected_profile.profile_digest
            or normalized.profile_digest != expected_profile.profile_digest
            or selected.profile_id != LIVE_OPS_DECISION_SUPPORT_PROFILE_ID
            or selected.profile_version != LIVE_OPS_DECISION_SUPPORT_PROFILE_VERSION
            or selected.task_kind is not SpecialistTaskKind.LIVE_OPS_ADVICE
        ):
            raise ValueError("Copilot requires exact live_ops_decision_support profile")
        if clock is not None and not callable(clock):
            raise TypeError("Copilot clock must be callable")
        object.__setattr__(self, "_runner", runner)
        object.__setattr__(self, "_profile", selected)
        # 生产使用 UTC 墙钟；测试可以注入固定可信时钟来重放边界，时钟本身不进入
        # AgentTask 或模型输入，避免把本地测试时间伪装成业务证据。
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))

    def __setattr__(self, _name: str, _value: Any) -> None:
        """启动后禁止替换 Runner/Profile，避免运行期放宽模型或权限边界。"""

        raise TypeError("live ops decision support is startup-frozen")

    async def propose(self, bundle: EvidenceBundle) -> LiveDecisionProposal:
        """生成建议；任何失败都返回 DEGRADED 事实摘要，不伪造成功方案。"""

        try:
            validated = EvidenceBundle.model_validate(bundle.model_dump(mode="json"))
            snapshot = EvidenceBundleSnapshot.model_validate(validated.snapshot)
            references = tuple(component.reference for component in snapshot.components)
            if not snapshot.proposal_eligible:
                return self._degraded(validated, "PROPOSAL_INELIGIBLE")
            try:
                as_of = self._clock()
                if as_of.tzinfo is None or as_of.utcoffset() is None:
                    raise ValueError("Copilot clock must be timezone-aware")
                if snapshot.valid_until <= as_of.astimezone(timezone.utc):
                    return self._degraded(validated, "EVIDENCE_EXPIRED")
            except Exception:
                return self._degraded(validated, "CLOCK_INVALID")
            task = AgentTask(
                task_id=f"live-ops-decision-support:{validated.evidence_bundle_id}",
                task_kind=SpecialistTaskKind.LIVE_OPS_ADVICE,
                profile_id=self._profile.profile_id,
                profile_version=self._profile.profile_version,
                room_id=snapshot.scope.room_id,
                trace_id=snapshot.scope.trace_id,
                objective="Generate structured live-commerce options for human operator review.",
                # EvidenceBundle 内部是 FrozenDict；Runner 的严格 JSON 协议要求在
                # 任务快照边界还原为普通 JSON，不能把内部容器类型泄漏给模型适配器。
                input_snapshot={"evidence_bundle": _plain_json(validated.snapshot)},
                initial_evidence_refs=references,
            )
        except Exception:
            return self._degraded(bundle, "EVIDENCE_INVALID")

        try:
            result = await self._runner.run(task)
            if (
                result.task_id != task.task_id
                or result.profile_id != task.profile_id
                or result.profile_version != task.profile_version
            ):
                return self._degraded(validated, "TASK_IDENTITY_MISMATCH")
            if result.status is not AgentResultStatus.SUCCEEDED:
                return self._degraded(
                    validated,
                    result.failure.code if result.failure else result.status.value,
                )
            proposal = LiveDecisionProposal.model_validate(_plain_json(result.output))
            if (
                proposal.live_session_id != validated.live_session_id
                or proposal.incident_id != validated.incident_id
                or proposal.trace_id != snapshot.scope.trace_id
                or proposal.evidence_bundle_id != validated.evidence_bundle_id
                or tuple(proposal.evidence_refs) != references
            ):
                return self._degraded(validated, "EVIDENCE_MISMATCH")
            inventory = next(
                component
                for component in snapshot.components
                if component.role is EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT
            )
            if not isinstance(inventory.payload, ProductInventoryPayload):
                return self._degraded(validated, "INVENTORY_INVALID")
            available_backups = {
                product.product_id
                for product in inventory.payload.backup_products
                if product.is_active and product.inventory > 0
            }
            if any(
                option.backup_product_id not in available_backups
                for option in proposal.options
                if option.product_strategy.value == "SWITCH_TO_BACKUP"
            ):
                return self._degraded(validated, "BACKUP_PRODUCT_MISMATCH")
            return proposal
        except Exception:
            return self._degraded(validated, "INVALID_OUTPUT")

    @staticmethod
    def _degraded(bundle: EvidenceBundle, reason: str) -> LiveDecisionProposal:
        """从确定性 Snapshot 生成可回放的降级事实摘要，不添加模型或经营判断。"""

        snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
        blocking = ",".join(snapshot.blocking_reasons) or "NONE"
        return LiveDecisionProposal(
            proposal_id=f"degraded:{bundle.evidence_bundle_id}",
            live_session_id=bundle.live_session_id,
            incident_id=bundle.incident_id,
            trace_id=snapshot.scope.trace_id,
            evidence_bundle_id=bundle.evidence_bundle_id,
            status=ProposalStatus.DEGRADED,
            options=(),
            evidence_refs=tuple(component.reference for component in snapshot.components),
            fact_summary=f"Evidence bundle available; proposal unavailable; blocking={blocking}",
            degraded_reason=reason,
        )
