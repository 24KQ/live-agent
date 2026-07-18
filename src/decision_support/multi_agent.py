"""Phase 16 受控双 Agent 的启动冻结 Profile 协议。

本模块在 Task 3 只负责固定模型输入/输出边界与预算身份；Task 5 才会在同一模块中加入
确定性升级选择器和协调器。这里没有 Store、Skill、命令或网络调用能力。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Protocol

from src.decision_support.evidence import EvidenceBundleSnapshot
from src.decision_support.models import (
    ConflictAnalysisCode,
    ConflictConstraintCode,
    ConflictRiskCode,
    ConflictAnalysis,
    EscalationMode,
    EscalationRecord,
    EvidenceBundle,
    MultiAgentFailureCode,
    MultiAgentOutcome,
    MultiAgentOutcomeStatus,
    WorkspaceView,
)
from src.decision_support.store import (
    WorkspaceConflictError,
    derive_automatic_escalation_codes,
)
from src.specialist_runtime.models import (
    AgentResult,
    AgentResultStatus,
    AgentTask,
    EvidenceKind,
    EvidenceRef,
    SpecialistTaskKind,
    canonical_json_sha256,
)
from src.specialist_runtime.profiles import (
    FORMAL_ENDPOINT_HOST,
    FORMAL_MODEL_ID,
    SpecialistProfile,
)


EVIDENCE_ANALYST_PROFILE_ID = "evidence_analyst"
DECISION_PLANNER_PROFILE_ID = "decision_planner"
CONTROLLED_MULTI_AGENT_PROFILE_VERSION = "1.0.0"


# Agent 只能返回引用身份，不接收正文解析、自由工具参数或任意额外字段。
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
    "required": [
        "kind",
        "evidence_id",
        "source_version",
        "digest",
        "anchor_id",
        "room_id",
    ],
}


# Analyst 不能输出商品排序、策略、Prompt、Skill 或执行字段；Coordinator 才会补齐父事实。
_CONFLICT_ANALYSIS_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "finding_codes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "uniqueItems": True,
            "items": {"enum": [code.value for code in ConflictAnalysisCode]},
        },
        "constraint_codes": {
            "type": "array",
            "maxItems": 3,
            "uniqueItems": True,
            "items": {"enum": [code.value for code in ConflictConstraintCode]},
        },
        "risk_codes": {
            "type": "array",
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"enum": [code.value for code in ConflictRiskCode]},
        },
        "explanation": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            # 标准 JSON Schema 无法便携表示 Unicode category C；先拒绝前后 Unicode
            # 空白和 ASCII 控制字符，Pydantic 再对全部 C 类做最终 fail-closed 校验。
            "pattern": "^(?!\\s)(?!.*\\s$)[^\\x00-\\x1F\\x7F]+$",
        },
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": _EVIDENCE_REF_SCHEMA,
        },
    },
    "required": [
        "finding_codes",
        "constraint_codes",
        "risk_codes",
        "explanation",
        "evidence_refs",
    ],
}


# Planner 只返回候选 option；升级、分析、Bundle、Profile、时间和最终 Proposal ID
# 均由确定性 Coordinator 注入，防止模型伪造上游事实或取得路由控制权。
_LIVE_DECISION_PLANNING_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "options": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "option_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 80,
                        "pattern": "^[a-z0-9][a-z0-9-]*$",
                    },
                    "product_strategy": {
                        "enum": [
                            "KEEP_CURRENT",
                            "SWITCH_TO_BACKUP",
                            "HOLD_AND_ESCALATE",
                            "REPLY_DANMAKU",
                        ]
                    },
                    "backup_product_id": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "host_prompt": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 300,
                        # 与上方 explanation 相同：Schema 预筛可表达的展示危险字符，
                        # 领域 Pydantic 模型保留 Unicode category C 的完整权威检查。
                        "pattern": "^(?!\\s)(?!.*\\s$)[^\\x00-\\x1F\\x7F]+$",
                    },
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
                            "enum": [code.value for code in ConflictRiskCode]
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
                # JSON Schema 先筛除备品策略与备品 ID 的明显矛盾；后续 Pydantic 仍会
                # 执行相同语义和更严格的 Unicode 控制字符检查，不让模型输出走旁路。
                "allOf": [
                    {
                        "if": {
                            "properties": {
                                "product_strategy": {"const": "SWITCH_TO_BACKUP"}
                            }
                        },
                        "then": {
                            "properties": {
                                "backup_product_id": {"type": "string", "minLength": 1}
                            }
                        },
                        "else": {
                            "properties": {"backup_product_id": {"type": "null"}}
                        },
                    }
                ],
            },
        }
    },
    "required": ["options"],
}


def _build_profile(
    *,
    profile_id: str,
    task_kind: SpecialistTaskKind,
    prompt_prefix: str,
    result_schema: dict[str, object],
    max_total_tokens: int,
    max_case_cost_cny: Decimal,
) -> SpecialistProfile:
    """统一构造温度零、单次调用、零 Skill 和两秒 deadline 的精确 Profile。"""

    # Runner 先解析 AgentAction，再只对 FINAL 的 final_output 校验 result_schema；Prompt
    # 必须同时固定两层形状，否则模型即使遵守结果 Schema 也会被 Runner 判为 INVALID_ACTION。
    prompt_text = (
        prompt_prefix
        + " Return exactly one AgentAction FINAL envelope and no markdown or reasoning. "
        + 'FINAL envelope: {"kind":"FINAL","final_output":<RESULT>,"evidence_refs":[<EvidenceRef>]}. '
        + "The final_output must match this RESULT JSON Schema: "
        + json.dumps(result_schema, sort_keys=True, separators=(",", ":"))
    )
    return SpecialistProfile(
        profile_id=profile_id,
        profile_version=CONTROLLED_MULTI_AGENT_PROFILE_VERSION,
        task_kind=task_kind,
        model_id=FORMAL_MODEL_ID,
        endpoint_host=FORMAL_ENDPOINT_HOST,
        temperature=Decimal("0"),
        prompt_text=prompt_text,
        prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        result_schema_hash=canonical_json_sha256(result_schema),
        result_schema=result_schema,
        allowed_skill_ids=(),
        skill_versions={},
        max_model_calls=1,
        max_skill_calls=0,
        max_total_tokens=max_total_tokens,
        deadline_seconds=2,
        max_case_cost_cny=max_case_cost_cny,
    )


def build_evidence_analyst_profile() -> SpecialistProfile:
    """返回只读 ConflictAnalysis Profile，预算固定为 2 秒、1200 token、0.03 CNY。"""

    return _build_profile(
        profile_id=EVIDENCE_ANALYST_PROFILE_ID,
        task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
        prompt_prefix=(
            "You are EvidenceAnalystAgent for a live-commerce conflict. "
            "Do not rank products, propose actions, call Skills, or claim authority."
        ),
        result_schema=_CONFLICT_ANALYSIS_RESULT_SCHEMA,
        max_total_tokens=1200,
        max_case_cost_cny=Decimal("0.030000"),
    )


def build_decision_planner_profile() -> SpecialistProfile:
    """返回只读 Planner Profile，预算固定为 2 秒、2800 token、0.07 CNY。"""

    return _build_profile(
        profile_id=DECISION_PLANNER_PROFILE_ID,
        task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING,
        prompt_prefix=(
            "You are DecisionPlannerAgent for a human-operated live-commerce incident. "
            "Return options only; never call Skills, select a route, or execute a command."
        ),
        result_schema=_LIVE_DECISION_PLANNING_RESULT_SCHEMA,
        max_total_tokens=2800,
        max_case_cost_cny=Decimal("0.070000"),
    )


class _AnalystRunner(Protocol):
    """协调器唯一依赖的模型执行面；实际装配仍使用受限共享 Runner。"""

    async def run(self, task: AgentTask) -> AgentResult:
        """执行一个已冻结的 EvidenceAnalystAgent 单次任务。"""

    def resolve_profile(self, task: AgentTask) -> SpecialistProfile:
        """返回实际 Runner Registry 中与任务匹配的完整冻结 Profile。"""


class _EscalationStore(Protocol):
    """Task 5 使用的窄 Store 读写面，禁止把 SQL 或任意事实容器交给 Agent。"""

    def get_workspace(self, live_session_id: str) -> Any:
        """读取当前 Workspace 投影以确认 LIVE 边界。"""

    def get_evidence_bundle(self, fact_id: str) -> EvidenceBundle:
        """按稳定 ID 读取 Store 权威 Bundle，而非相信调用方对象。"""

    def list_escalations(self, live_session_id: str) -> tuple[EscalationRecord, ...]:
        """读取该 Workspace 的追加升级历史以支持幂等恢复。"""

    def list_conflict_analyses(self, live_session_id: str) -> tuple[ConflictAnalysis, ...]:
        """读取已持久化分析，避免响应丢失后再次发送模型请求。"""

    def list_multi_agent_outcomes(
        self, live_session_id: str
    ) -> tuple[MultiAgentOutcome, ...]:
        """读取唯一终态，失败重试只能返回原有降级事实。"""

    def append_escalation(
        self,
        fact: EscalationRecord,
        *,
        expected_workspace_version: int,
        operator_id: str | None = None,
        fencing_token: int | None = None,
    ) -> Any:
        """原子追加自动或人工升级，并返回递增后的 Workspace。"""

    def append_conflict_analysis(
        self, fact: ConflictAnalysis, *, expected_workspace_version: int
    ) -> Any:
        """原子追加完整 Analyst 事实，并返回递增后的 Workspace。"""

    def append_multi_agent_outcome(
        self, fact: MultiAgentOutcome, *, expected_workspace_version: int
    ) -> Any:
        """原子追加每次升级唯一的 `DEGRADED` 终态。"""

    def claim_analyst_dispatch(
        self,
        *,
        escalation_id: str,
        task_digest: str,
        now: datetime | None = None,
        lease_seconds: int = 2,
    ) -> tuple[Any, bool, bool]:
        """持久化外部发送前的单次 claim，返回事实、发送权及 Store 权威活跃状态。"""


class HighConflictCoordinationResult:
    """Task 5 的受限协调结果，不包含 Planner、Proposal、命令或执行授权。"""

    __slots__ = ("selected", "escalation", "analysis", "outcome")

    def __init__(
        self,
        *,
        selected: bool,
        escalation: EscalationRecord | None = None,
        analysis: ConflictAnalysis | None = None,
        outcome: MultiAgentOutcome | None = None,
    ) -> None:
        """固定成功、未选中和降级三种不可扩展结果形状。"""

        if not selected and any(item is not None for item in (escalation, analysis, outcome)):
            raise ValueError("unselected coordination cannot carry persisted facts")
        if analysis is not None and escalation is None:
            raise ValueError("analysis requires escalation")
        if outcome is not None and escalation is None:
            raise ValueError("outcome requires escalation")
        if outcome is not None and outcome.status is not MultiAgentOutcomeStatus.DEGRADED:
            raise ValueError("Task 5 only exposes DEGRADED outcomes")
        self.selected = selected
        self.escalation = escalation
        self.analysis = analysis
        self.outcome = outcome


class HighConflictEscalationCoordinator:
    """把高冲突 LIVE Bundle 受控接入单次 EvidenceAnalystAgent。

    本协调器只决定是否升级、持久化父事实、调用 Analyst 并落库其完整分析。它从不
    创建 Planner 输入、READY Proposal、经营命令或自动恢复；Task 6 才能消费成功
    的分析。所有运行错误在已经存在升级后转为唯一 `DEGRADED` Outcome，以便重启
    恢复不会重新发送模型请求。
    """

    __slots__ = ("_store", "_analyst_runner", "_profile", "_clock")

    def __init__(
        self,
        *,
        store: _EscalationStore,
        analyst_runner: _AnalystRunner,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """冻结精确 Analyst Profile 与可信 UTC 时钟，拒绝运行期替换边界。"""

        if clock is not None and not callable(clock):
            raise TypeError("coordinator clock must be callable")
        self._store = store
        self._analyst_runner = analyst_runner
        self._profile = build_evidence_analyst_profile()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __setattr__(self, name: str, value: Any) -> None:
        """启动冻结后禁止替换 Store、Runner、Profile 或时钟取得额外能力。"""

        if hasattr(self, name):
            raise TypeError("high conflict coordinator is startup-frozen")
        object.__setattr__(self, name, value)

    async def run_automatic(
        self,
        bundle: EvidenceBundle,
        *,
        expected_workspace_version: int,
    ) -> HighConflictCoordinationResult:
        """仅在同一权威 Bundle 满足三选二、fresh、eligible、LIVE 时自动升级。"""

        validated = self._load_authoritative_bundle(bundle)
        if validated is None:
            return HighConflictCoordinationResult(selected=False)
        existing = self._find_escalation(validated)
        if existing is not None:
            # 已持久化的事实优先于新请求时的 freshness/LIVE 投影：这里仅恢复
            # 已知结果或处理已有 dispatch claim，绝不因为当前状态改变重新路由模型。
            return await self._coordinate(
                bundle=validated,
                expected_workspace_version=expected_workspace_version,
                mode=existing.mode,
                trigger_codes=existing.trigger_codes,
                operator_id=None,
                fencing_token=None,
                may_dispatch=self._is_proposal_eligible_and_fresh(validated),
            )
        if self._store.get_workspace(validated.live_session_id).view is not WorkspaceView.LIVE:
            return HighConflictCoordinationResult(selected=False)
        trigger_codes = self._select_automatic_codes(validated)
        if not trigger_codes:
            return HighConflictCoordinationResult(selected=False)
        return await self._coordinate(
            bundle=validated,
            expected_workspace_version=expected_workspace_version,
            mode=EscalationMode.AUTOMATIC,
            trigger_codes=trigger_codes,
            operator_id=None,
            fencing_token=None,
            may_dispatch=True,
        )

    async def run_operator_requested(
        self,
        bundle: EvidenceBundle,
        *,
        expected_workspace_version: int,
        operator_id: str,
        fencing_token: int,
    ) -> HighConflictCoordinationResult:
        """在当前 lease 下执行人工显式升级；调用方不能提供触发码、Profile 或父作用域。"""

        validated = self._load_authoritative_bundle(bundle)
        if validated is None:
            return HighConflictCoordinationResult(selected=False)
        existing = self._find_escalation(validated)
        if existing is not None:
            return await self._coordinate(
                bundle=validated,
                expected_workspace_version=expected_workspace_version,
                mode=existing.mode,
                trigger_codes=existing.trigger_codes,
                operator_id=None,
                fencing_token=None,
                may_dispatch=self._is_proposal_eligible_and_fresh(validated),
            )
        if self._store.get_workspace(validated.live_session_id).view is not WorkspaceView.LIVE:
            return HighConflictCoordinationResult(selected=False)
        # 手动路径仍只接受 fresh、proposal-eligible Bundle；Store 在同一 CAS 写中会
        # 再次复核，避免调用前检查与提交之间的时间窗口形成信任旁路。
        if not self._is_proposal_eligible_and_fresh(validated):
            return HighConflictCoordinationResult(selected=False)
        # 人工拥有升级权而没有事实注入权：服务端仍从冻结 Bundle 重建全部真实信号。
        # 自动路径要求三选二；人工路径允许运营在至少一项已证实冲突出现时请求更深
        # 分析。零信号 Bundle 没有能被 Analyst 合法复述的 finding，因此必须停在模型前。
        trigger_codes = derive_automatic_escalation_codes(validated)
        if not trigger_codes:
            return HighConflictCoordinationResult(selected=False)
        return await self._coordinate(
            bundle=validated,
            expected_workspace_version=expected_workspace_version,
            mode=EscalationMode.OPERATOR_REQUESTED,
            trigger_codes=trigger_codes,
            operator_id=operator_id,
            fencing_token=fencing_token,
            may_dispatch=True,
        )

    def _load_authoritative_bundle(self, supplied: EvidenceBundle) -> EvidenceBundle | None:
        """重新校验调用对象并逐字段比对 Store 事实，拒绝同 ID 的进程内替换。"""

        try:
            validated = EvidenceBundle.model_validate(supplied.model_dump(mode="json"))
            stored = self._store.get_evidence_bundle(validated.evidence_bundle_id)
            authoritative = EvidenceBundle.model_validate(stored.model_dump(mode="json"))
            if authoritative.model_dump(mode="json") != validated.model_dump(mode="json"):
                return None
            return authoritative
        except Exception:
            return None

    def _select_automatic_codes(
        self, bundle: EvidenceBundle
    ) -> tuple[ConflictAnalysisCode, ...]:
        """以 Store 共用规则做三选二选择，全部输入均来自可重放 Bundle 快照。"""

        if not self._is_proposal_eligible_and_fresh(bundle):
            return ()
        codes = derive_automatic_escalation_codes(bundle)
        return codes if len(codes) >= 2 else ()

    def _is_proposal_eligible_and_fresh(self, bundle: EvidenceBundle) -> bool:
        """统一处理无时区时钟、失效快照与对账阻断，全部视为不可进入模型。"""

        try:
            instant = self._clock()
            if instant.tzinfo is None or instant.utcoffset() is None:
                return False
            snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
            return snapshot.proposal_eligible and instant.astimezone(timezone.utc) < snapshot.valid_until
        except Exception:
            return False

    async def _coordinate(
        self,
        *,
        bundle: EvidenceBundle,
        expected_workspace_version: int,
        mode: EscalationMode,
        trigger_codes: tuple[ConflictAnalysisCode, ...],
        operator_id: str | None,
        fencing_token: int | None,
        may_dispatch: bool,
    ) -> HighConflictCoordinationResult:
        """先写升级、再恢复已有终态或分析，最后才允许一次 Analyst 调用。"""

        existing = self._find_escalation(bundle)
        if existing is None:
            escalation = EscalationRecord(
                escalation_id=f"phase16-escalation:{mode.value.lower()}:{bundle.evidence_bundle_id}",
                live_session_id=bundle.live_session_id,
                incident_id=bundle.incident_id,
                evidence_bundle_id=bundle.evidence_bundle_id,
                evidence_bundle_digest=EvidenceBundleSnapshot.model_validate(bundle.snapshot).bundle_digest,
                idempotency_key=f"phase16-escalation:{mode.value.lower()}:{bundle.evidence_bundle_id}",
                mode=mode,
                trigger_codes=trigger_codes,
                operator_id=operator_id,
                created_at=self._utc_now(),
            )
            after_escalation = self._store.append_escalation(
                escalation,
                expected_workspace_version=expected_workspace_version,
                operator_id=operator_id,
                fencing_token=fencing_token,
            )
        else:
            escalation = existing
            after_escalation = self._store.get_workspace(bundle.live_session_id)

        recovered = self._recover_existing(escalation)
        if recovered is not None:
            return recovered

        # 已完成结果可以跨 LIVE 结束读取，但任何尚未 dispatch 的升级在进入 REVIEW
        # 后只能保留为未闭合审计事实。此处再次读取 Store 投影，防止初始 LIVE 检查
        # 与 claim 创建之间发生状态切换后仍把陈旧直播上下文发送给 Analyst。
        if self._store.get_workspace(bundle.live_session_id).view is not WorkspaceView.LIVE:
            return HighConflictCoordinationResult(selected=True, escalation=escalation)

        task = self._build_analyst_task(bundle, escalation)
        if not self._runner_profile_matches(task):
            return self._persist_degraded(
                escalation, MultiAgentFailureCode.ANALYST_INVALID_OUTPUT
            )
        try:
            claim, is_new_claim, is_active_claim = self._store.claim_analyst_dispatch(
                escalation_id=escalation.escalation_id,
                task_digest=task.task_digest,
                now=self._utc_now(),
                lease_seconds=self._profile.deadline_seconds,
            )
        except Exception:
            return self._persist_degraded(
                escalation, MultiAgentFailureCode.ANALYST_INVALID_OUTPUT
            )
        if claim.task_digest != task.task_digest:
            return self._persist_degraded(
                escalation, MultiAgentFailureCode.ANALYST_INVALID_OUTPUT
            )
        if not is_new_claim:
            # 活跃 claim 表示另一个 Coordinator 已获得一次发送权；返回 pending 给调用方
            # 轮询，不抢占、取消或制造竞争性 DEGRADED。过期后响应是否已离开进程未知，
            # 只能 fail-closed 落终态，禁止相同 task 的第二次模型调用。
            if is_active_claim:
                return HighConflictCoordinationResult(selected=True, escalation=escalation)
            return self._persist_degraded(
                escalation, MultiAgentFailureCode.COORDINATOR_TIMEOUT
            )
        if not may_dispatch:
            # Escalation 已提交但发送前证据已失效时，保留审计链并交给人工；不能把
            # 已选中的高冲突事件伪装成普通未选中，也不能对陈旧快照发送模型。
            return self._persist_degraded(
                escalation, MultiAgentFailureCode.COORDINATOR_TIMEOUT
            )
        try:
            # PostgreSQL claim 的到期边界属于数据库事务时钟，不能同 Worker 的业务墙钟
            # 混算。Store 在发送前返回权威剩余秒数；这段时间已包含建 claim 到此处的
            # 所有本地工作，窗口用尽时只追加降级审计，绝不在过期 lease 外继续等待。
            remaining_seconds = self._store.get_analyst_dispatch_remaining_seconds(
                claim.escalation_id
            )
            if not 0 < remaining_seconds <= self._profile.deadline_seconds:
                return self._persist_degraded(
                    escalation, MultiAgentFailureCode.COORDINATOR_TIMEOUT
                )
            result = await asyncio.wait_for(
                self._analyst_runner.run(task), timeout=remaining_seconds
            )
            analysis = self._analysis_from_result(bundle, escalation, task, result)
            self._store.append_conflict_analysis(
                analysis, expected_workspace_version=after_escalation.version
            )
            return HighConflictCoordinationResult(
                selected=True, escalation=escalation, analysis=analysis
            )
        except asyncio.TimeoutError:
            return self._persist_degraded(
                escalation, MultiAgentFailureCode.COORDINATOR_TIMEOUT
            )
        except _AnalystResultError as error:
            return self._persist_degraded(escalation, error.failure_code)
        except Exception:
            # Runner、输出反序列化与持久化异常都不得向上泄漏为“可重试模型调用”；
            # 唯一降级事实会令下一次请求只恢复，不重新发送相同冻结任务。
            recovered = self._recover_existing(escalation)
            if recovered is not None:
                return recovered
            return self._persist_degraded(
                escalation, MultiAgentFailureCode.ANALYST_MODEL_ERROR
            )

    def _runner_profile_matches(self, task: AgentTask) -> bool:
        """发送前比较实际 Runner Registry 的完整 Profile，阻断同名版本的错误装配。"""

        try:
            actual = self._analyst_runner.resolve_profile(task)
            normalized = SpecialistProfile.model_validate(actual.model_dump(mode="json"))
            return (
                type(actual) is SpecialistProfile
                and normalized.profile_digest == self._profile.profile_digest
                and actual.profile_digest == self._profile.profile_digest
                and actual.allowed_skill_ids == ()
                and actual.max_model_calls == 1
                and actual.max_skill_calls == 0
            )
        except Exception:
            return False

    def _find_escalation(self, bundle: EvidenceBundle) -> EscalationRecord | None:
        """同一 Bundle 只能对应一条升级；恢复时从 append-only 历史按 Bundle 查找。"""

        matches = [
            item
            for item in self._store.list_escalations(bundle.live_session_id)
            if item.evidence_bundle_id == bundle.evidence_bundle_id
        ]
        if len(matches) > 1:
            raise WorkspaceConflictError("bundle has multiple escalation facts")
        return matches[0] if matches else None

    def _recover_existing(
        self, escalation: EscalationRecord
    ) -> HighConflictCoordinationResult | None:
        """优先恢复唯一终态，其次恢复完整分析，保证网络重试绝不触发第二次模型发送。"""

        outcomes = [
            item
            for item in self._store.list_multi_agent_outcomes(escalation.live_session_id)
            if item.escalation_id == escalation.escalation_id
        ]
        if len(outcomes) > 1:
            raise WorkspaceConflictError("escalation has multiple outcomes")
        if outcomes:
            return HighConflictCoordinationResult(
                selected=True, escalation=escalation, outcome=outcomes[0]
            )
        analyses = [
            item
            for item in self._store.list_conflict_analyses(escalation.live_session_id)
            if item.escalation_id == escalation.escalation_id
        ]
        if len(analyses) > 1:
            raise WorkspaceConflictError("escalation has multiple analyses")
        if analyses:
            return HighConflictCoordinationResult(
                selected=True, escalation=escalation, analysis=analyses[0]
            )
        return None

    def _build_analyst_task(
        self, bundle: EvidenceBundle, escalation: EscalationRecord
    ) -> AgentTask:
        """只把 Bundle 快照、父身份与完整 Bundle 引用交给 Analyst，不传 Store 或命令面。"""

        snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
        references = tuple(component.reference for component in snapshot.components)
        return AgentTask(
            task_id=f"phase16-analyst:{escalation.escalation_id}",
            task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
            profile_id=self._profile.profile_id,
            profile_version=self._profile.profile_version,
            room_id=snapshot.scope.room_id,
            trace_id=snapshot.scope.trace_id,
            objective="Analyze only governed sold-out conflict evidence for operator review.",
            input_snapshot={
                "escalation_id": escalation.escalation_id,
                "escalation_digest": escalation.escalation_digest,
                # 触发码由确定性选择器和 Store 双重重建，Agent 只能解释这些既有
                # 事实而不得新增或删减。输出阶段会再次要求它精确回显该有序集合。
                "trigger_codes": [code.value for code in escalation.trigger_codes],
                "evidence_bundle": bundle.model_dump(mode="json")["snapshot"],
            },
            initial_evidence_refs=references,
        )

    def _analysis_from_result(
        self,
        bundle: EvidenceBundle,
        escalation: EscalationRecord,
        task: AgentTask,
        result: AgentResult,
    ) -> ConflictAnalysis:
        """重新验证 Runner 身份、封闭输出和完整证据集，再构造可追加的分析事实。"""

        if (
            result.task_id != task.task_id
            or result.profile_id != self._profile.profile_id
            or result.profile_version != self._profile.profile_version
        ):
            raise _AnalystResultError(MultiAgentFailureCode.ANALYST_INVALID_OUTPUT)
        if result.status is not AgentResultStatus.SUCCEEDED:
            raise _AnalystResultError(self._failure_code_for_result(result))
        try:
            output = result.output
            # AgentResult 为阻断调用方运行中篡改会把 JSON 递归冻结为 FrozenDict；
            # 协调器应接受该只读 Mapping，再复制到本地普通字典完成严格字段读取，
            # 不能把共享 Runner 已验证的结构化结果误判为无效输出。
            if not isinstance(output, Mapping):
                raise ValueError("analysis output must be object")
            output = dict(output)
            snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
            references = tuple(component.reference for component in snapshot.components)
            returned_refs = tuple(
                EvidenceRef.model_validate(item) for item in output["evidence_refs"]
            )
            finding_codes = tuple(ConflictAnalysisCode(item) for item in output["finding_codes"])
            if returned_refs != references or finding_codes != escalation.trigger_codes:
                raise ValueError("analysis does not exactly bind bundle evidence and triggers")
            return ConflictAnalysis(
                analysis_id=f"phase16-analysis:{escalation.escalation_id}",
                idempotency_key=f"phase16-analysis:{escalation.escalation_id}",
                escalation_id=escalation.escalation_id,
                live_session_id=escalation.live_session_id,
                incident_id=escalation.incident_id,
                evidence_bundle_id=escalation.evidence_bundle_id,
                evidence_bundle_digest=escalation.evidence_bundle_digest,
                analyst_profile_id=self._profile.profile_id,
                analyst_profile_version=self._profile.profile_version,
                analyst_profile_digest=self._profile.profile_digest,
                finding_codes=finding_codes,
                constraint_codes=tuple(output["constraint_codes"]),
                risk_codes=tuple(output["risk_codes"]),
                explanation=output["explanation"],
                evidence_refs=references,
                created_at=self._utc_now(),
            )
        except Exception as exc:
            raise _AnalystResultError(MultiAgentFailureCode.ANALYST_INVALID_OUTPUT) from exc

    def _persist_degraded(
        self,
        escalation: EscalationRecord,
        failure_code: MultiAgentFailureCode,
    ) -> HighConflictCoordinationResult:
        """以稳定身份追加唯一降级结果；已有终态优先返回，避免第二次写入或模型重试。"""

        recovered = self._recover_existing(escalation)
        if recovered is not None:
            return recovered
        outcome = MultiAgentOutcome(
            outcome_id=f"phase16-outcome:{escalation.escalation_id}",
            idempotency_key=f"phase16-outcome:{escalation.escalation_id}",
            escalation_id=escalation.escalation_id,
            live_session_id=escalation.live_session_id,
            incident_id=escalation.incident_id,
            escalation_digest=escalation.escalation_digest,
            evidence_bundle_id=escalation.evidence_bundle_id,
            evidence_bundle_digest=escalation.evidence_bundle_digest,
            status=MultiAgentOutcomeStatus.DEGRADED,
            failure_code=failure_code,
            fact_summary=(
                "Evidence analyst unavailable; deterministic protection remains active and "
                "operator review is required."
            ),
            created_at=self._utc_now(),
        )
        for attempt in range(2):
            workspace = self._store.get_workspace(escalation.live_session_id)
            try:
                self._store.append_multi_agent_outcome(
                    outcome, expected_workspace_version=workspace.version
                )
                return HighConflictCoordinationResult(
                    selected=True, escalation=escalation, outcome=outcome
                )
            except WorkspaceConflictError:
                # claim 到期后运营可进入 REVIEW。首次读版本与写入之间若恰好发生该
                # 迁移，只重新读取并重试同一条 append-only DEGRADED 事实一次；不重发
                # 模型、不改写 payload，也不把这条受限恢复扩大到 Analysis 或 Proposal。
                recovered = self._recover_existing(escalation)
                if recovered is not None:
                    return recovered
                if attempt == 0:
                    continue
                raise
            except Exception:
                recovered = self._recover_existing(escalation)
                if recovered is not None:
                    return recovered
                raise

    @staticmethod
    def _failure_code_for_result(result: AgentResult) -> MultiAgentFailureCode:
        """将共享 Runner 的开放失败状态压缩为 Phase 16 可审计的封闭代码。"""

        if result.status is AgentResultStatus.BUDGET_EXCEEDED:
            return MultiAgentFailureCode.ANALYST_BUDGET_EXCEEDED
        if result.status is AgentResultStatus.MODEL_ERROR:
            return MultiAgentFailureCode.ANALYST_MODEL_ERROR
        return MultiAgentFailureCode.ANALYST_INVALID_OUTPUT

    def _utc_now(self) -> datetime:
        """把可注入时钟规范为 UTC；无时区时间不能作为 append-only 事实时间。"""

        instant = self._clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("coordinator clock must be timezone-aware")
        return instant.astimezone(timezone.utc)


class _AnalystResultError(RuntimeError):
    """在不泄漏模型自由文本的前提下，把 Analyst 失败归一为一个稳定原因码。"""

    def __init__(self, failure_code: MultiAgentFailureCode) -> None:
        super().__init__(failure_code.value)
        self.failure_code = failure_code
