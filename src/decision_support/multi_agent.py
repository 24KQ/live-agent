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
from time import monotonic
from typing import Any, Callable, Protocol

from src.decision_support.evidence import (
    EvidenceBundleSnapshot,
    EvidenceRole,
    ProductInventoryPayload,
)
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
    MultiAgentProposalLineage,
    Proposal,
    WorkspaceView,
)
from src.decision_support.proposal import (
    DecisionOption,
    LiveDecisionProposal,
    ProductStrategy,
    ProposalOrigin,
    ProposalStatus,
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
    _plain_json,
)
from src.specialist_runtime.profiles import (
    FORMAL_ENDPOINT_HOST,
    FORMAL_MODEL_ID,
    SpecialistProfile,
)


EVIDENCE_ANALYST_PROFILE_ID = "evidence_analyst"
DECISION_PLANNER_PROFILE_ID = "decision_planner"
CONTROLLED_MULTI_AGENT_PROFILE_VERSION = "1.0.0"
COORDINATOR_DEADLINE_SECONDS = 5
COORDINATOR_MAX_TOTAL_TOKENS = 4000
COORDINATOR_MAX_CASE_COST_CNY = Decimal("0.100000")


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


class _PlannerRunner(Protocol):
    """Planner 唯一依赖的单次模型端口；不暴露 Store、Skill 或执行能力。"""

    async def run(self, task: AgentTask) -> AgentResult:
        """执行已冻结的 LIVE_DECISION_PLANNING 任务。"""

    def resolve_profile(self, task: AgentTask) -> SpecialistProfile:
        """返回实际 Runner Registry 中与 Planner 任务对应的完整 Profile。"""


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

    def list_proposals(self, live_session_id: str) -> tuple[Proposal, ...]:
        """读取既有 Proposal，处理 Proposal 已提交但 READY Outcome 尚未落库的恢复窗口。"""

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
        """原子追加每次升级唯一的 READY 或 DEGRADED 终态。"""

    def append_proposal(
        self, fact: Proposal, *, expected_workspace_version: int
    ) -> Any:
        """追加已由确定性 Validator 完整验证的 Proposal 快照。"""

    def append_multi_agent_proposal(
        self, fact: Proposal, *, expected_workspace_version: int
    ) -> Any:
        """仅允许受控 Coordinator 写入已经完成 Planner 全量验证的多 Agent Proposal。"""

    def get_proposal(self, fact_id: str) -> Proposal:
        """按稳定身份恢复 Proposal，用于 READY Outcome 的重启重放。"""

    def claim_analyst_dispatch(
        self,
        *,
        escalation_id: str,
        task_digest: str,
        now: datetime | None = None,
        lease_seconds: int = 2,
    ) -> tuple[Any, bool, bool]:
        """持久化外部发送前的单次 claim，返回事实、发送权及 Store 权威活跃状态。"""

    def get_analyst_dispatch_remaining_seconds(self, escalation_id: str) -> float:
        """返回 Analyst claim 的 Store 权威剩余时间，禁止 Coordinator 自行使用墙钟重算。"""

    def claim_planner_dispatch(
        self,
        *,
        escalation_id: str,
        analysis_id: str,
        analysis_digest: str,
        task_digest: str,
        now: datetime | None = None,
        lease_seconds: int = 2,
    ) -> tuple[Any, bool, bool]:
        """在 Planner 外部发送前原子绑定既有 Analysis，重复调用只能观察同一 claim。"""

    def get_planner_dispatch_remaining_seconds(self, escalation_id: str) -> float:
        """返回 Planner claim 的 Store 权威剩余时间，不能由 Worker 时钟放大租约。"""

    def get_planner_dispatch_claim(self, escalation_id: str) -> Any:
        """读取 Planner 已发送事实，以便 REVIEW 恢复只闭合已离开进程的第二段请求。"""


class HighConflictCoordinationResult:
    """受控双 Agent 协调结果；Proposal 仍只供后续人工 OperatorDecision 使用。"""

    __slots__ = ("selected", "escalation", "analysis", "proposal", "outcome")

    def __init__(
        self,
        *,
        selected: bool,
        escalation: EscalationRecord | None = None,
        analysis: ConflictAnalysis | None = None,
        proposal: LiveDecisionProposal | None = None,
        outcome: MultiAgentOutcome | None = None,
    ) -> None:
        """固定成功、未选中和降级三种不可扩展结果形状。"""

        if not selected and any(
            item is not None for item in (escalation, analysis, proposal, outcome)
        ):
            raise ValueError("unselected coordination cannot carry persisted facts")
        if analysis is not None and escalation is None:
            raise ValueError("analysis requires escalation")
        if proposal is not None and analysis is None:
            raise ValueError("proposal requires analysis")
        if outcome is not None and escalation is None:
            raise ValueError("outcome requires escalation")
        if outcome is not None and outcome.status is MultiAgentOutcomeStatus.READY:
            if proposal is None or analysis is None:
                raise ValueError("READY outcome requires analysis and proposal")
        if outcome is not None and outcome.status is MultiAgentOutcomeStatus.DEGRADED:
            if proposal is not None:
                raise ValueError("DEGRADED outcome cannot carry proposal")
        self.selected = selected
        self.escalation = escalation
        self.analysis = analysis
        self.proposal = proposal
        self.outcome = outcome


class HighConflictEscalationCoordinator:
    """把高冲突 LIVE Bundle 受控接入顺序双 Agent，并保留确定性安全与人工权限。

    Coordinator 只负责升级选择、持久化单次 Analyst/Planner dispatch claim、完整 Analysis
    与 Proposal 父链、READY/DEGRADED Outcome 以及重启恢复。两个 Agent 只能读取冻结
    Evidence，不能调用 Skill、写 Store 或执行经营恢复；OperatorDecision、Compiler 与受控
    ExecutionCommand 继续是唯一的人工授权路径。任何未知响应只能恢复或降级，绝不重发模型。
    """

    __slots__ = (
        "_store",
        "_analyst_runner",
        "_analyst_profile",
        "_planner_runner",
        "_planner_profile",
        "_clock",
        "_monotonic_clock",
    )

    def __init__(
        self,
        *,
        store: _EscalationStore,
        analyst_runner: _AnalystRunner,
        planner_runner: _PlannerRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        """冻结精确 Agent Profile 与可信 UTC 时钟，缺失 Planner 时保持 Task 5 只分析语义。"""

        if clock is not None and not callable(clock):
            raise TypeError("coordinator clock must be callable")
        if monotonic_clock is not None and not callable(monotonic_clock):
            raise TypeError("coordinator monotonic_clock must be callable")
        self._store = store
        self._analyst_runner = analyst_runner
        self._analyst_profile = build_evidence_analyst_profile()
        self._planner_runner = planner_runner
        self._planner_profile = build_decision_planner_profile()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock or monotonic
        # 两个启动冻结 Profile 的 token 和成本相加必须正好落在总 Coordinator 上限内。
        # 这个断言不替代 Task 10 的真实计费账本，只防止后续改动静默扩大本 Task 的设计预算。
        if (
            self._analyst_profile.max_total_tokens + self._planner_profile.max_total_tokens
            != COORDINATOR_MAX_TOTAL_TOKENS
            or self._analyst_profile.max_case_cost_cny
            + self._planner_profile.max_case_cost_cny
            != COORDINATOR_MAX_CASE_COST_CNY
        ):
            raise ValueError("controlled multi-agent profile budget does not match coordinator ceiling")

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

        # 总预算从公共入口开始。权威 Bundle 重载、Store 读取、选择器判断和 CAS 前置均会
        # 消耗直播现场时间，不能完成这些工作后再获得一整段新的五秒模型预算。
        coordinator_deadline = (
            self._monotonic_clock() + COORDINATOR_DEADLINE_SECONDS
        )
        validated = self._load_authoritative_bundle(bundle)
        if validated is None:
            return HighConflictCoordinationResult(selected=False)
        existing = self._find_escalation(validated)
        if existing is not None:
            if existing.mode is EscalationMode.OPERATOR_REQUESTED:
                # 自动触发器没有人工租约，也不知道首次人工授权是否仍然有效。人工路径可用
                # 单项信号，而自动路径要求三选二；若此处继续进入 _coordinate，会让自动轮询
                # 在租约过期后替人工请求发送 Analyst/Planner。故自动入口对人工事实只读取
                # 已落库的 Analysis/Proposal/Outcome；pending 时返回事实身份，不写 claim、
                # 不追加终态、更不触发任何 Runner。后续推进仍必须由持有当前 lease 的人工
                # 请求路径完成，确保模型预算和经营建议始终能追溯到有效操作员授权。
                return self._recover_existing(existing) or HighConflictCoordinationResult(
                    selected=True,
                    escalation=existing,
                )
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
                coordinator_deadline=coordinator_deadline,
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
            coordinator_deadline=coordinator_deadline,
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

        # 人工显式升级同样不能绕过端到端延迟上限；运营授权不授予额外模型等待时间。
        coordinator_deadline = (
            self._monotonic_clock() + COORDINATOR_DEADLINE_SECONDS
        )
        validated = self._load_authoritative_bundle(bundle)
        if validated is None:
            return HighConflictCoordinationResult(selected=False)
        existing = self._find_escalation(validated)
        if existing is not None:
            # D-156：Service 的预读与本次最终 Store 观察之间可能已有自动升级提交。
            # 人工请求绝不能把自动事实偷换为自己的 replay；必须在任何恢复、终态写入
            # 或 Runner 调用之前拒绝，只有同模式的既有人工事实才可按 D-155 恢复。
            if existing.mode is not EscalationMode.OPERATOR_REQUESTED:
                raise WorkspaceConflictError("bundle already has automatic escalation")
            return await self._coordinate(
                bundle=validated,
                expected_workspace_version=expected_workspace_version,
                mode=existing.mode,
                trigger_codes=existing.trigger_codes,
                operator_id=None,
                fencing_token=None,
                may_dispatch=self._is_proposal_eligible_and_fresh(validated),
                coordinator_deadline=coordinator_deadline,
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
            coordinator_deadline=coordinator_deadline,
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
        coordinator_deadline: float,
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
            if (
                recovered.analysis is not None
                and recovered.proposal is not None
                and recovered.outcome is None
            ):
                # Proposal 已是不可变父事实时，唯一合法恢复动作是补写 READY Outcome；
                # 绝不能重新进入 Planner 模型调用，哪怕前次响应恰好在 Outcome 写入前中断。
                workspace = self._store.get_workspace(bundle.live_session_id)
                if workspace.view is WorkspaceView.LIVE:
                    return self._append_ready_outcome(
                        escalation=escalation,
                        analysis=recovered.analysis,
                        proposal=recovered.proposal,
                        expected_workspace_version=workspace.version,
                        coordinator_deadline=coordinator_deadline,
                    )
                # Planner 已经发送且 Proposal 也已存在，但 READY 未在 LIVE 内完成。播后
                # 不能补 Proposal/READY；仅能利用既有 claim 追加不含父链的降级审计闭合。
                return self._persist_degraded(
                    escalation,
                    MultiAgentFailureCode.COORDINATOR_TIMEOUT,
                    analysis=recovered.analysis,
                    allow_review_terminalization=True,
                )
            if (
                recovered.analysis is not None
                and recovered.proposal is None
                and recovered.outcome is None
                and self._planner_runner is not None
                and self._store.get_workspace(bundle.live_session_id).view
                is WorkspaceView.LIVE
            ):
                # Task 5 可能已经安全落下 Analysis，而本次启动才显式装配 Planner。
                # 该分支只消费现有不可变中间事实，不再创建 Analyst claim 或重发 Analyst。
                return await self._plan_after_analysis(
                    bundle=bundle,
                    escalation=escalation,
                    analysis=recovered.analysis,
                    expected_workspace_version=self._store.get_workspace(
                        bundle.live_session_id
                    ).version,
                    coordinator_deadline=coordinator_deadline,
                )
            if (
                recovered.analysis is not None
                and recovered.proposal is None
                and recovered.outcome is None
                and self._store.get_workspace(bundle.live_session_id).view
                is WorkspaceView.REVIEW
                and self._store.get_planner_dispatch_claim(escalation.escalation_id)
                is not None
            ):
                # 只有 Planner claim 已经离开进程时才允许播后闭合。未装配 Planner 的
                # Task 5 历史 Analysis 不能被伪造成模型失败或自动写入新的终态。
                return self._persist_degraded(
                    escalation,
                    MultiAgentFailureCode.COORDINATOR_TIMEOUT,
                    analysis=recovered.analysis,
                    allow_review_terminalization=True,
                )
            return recovered

        # 已完成结果可以跨 LIVE 结束读取，但任何尚未 dispatch 的升级在进入 REVIEW
        # 后只能保留为未闭合审计事实。此处再次读取 Store 投影，防止初始 LIVE 检查
        # 与 claim 创建之间发生状态切换后仍把陈旧直播上下文发送给 Analyst。
        if self._store.get_workspace(bundle.live_session_id).view is not WorkspaceView.LIVE:
            return HighConflictCoordinationResult(selected=True, escalation=escalation)

        # D-148 的五秒上限必须覆盖 Analyst 之前的升级落库、Profile 校验和 claim 创建。
        # 若这些确定性步骤已经耗尽总窗口，发送模型只会制造迟到且不可安全使用的响应。
        coordinator_remaining = coordinator_deadline - self._monotonic_clock()
        if not 0 < coordinator_remaining <= COORDINATOR_DEADLINE_SECONDS:
            return self._persist_degraded(
                escalation, MultiAgentFailureCode.COORDINATOR_TIMEOUT
            )

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
                lease_seconds=self._analyst_profile.deadline_seconds,
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
            claim_remaining = self._store.get_analyst_dispatch_remaining_seconds(
                claim.escalation_id
            )
            coordinator_remaining = coordinator_deadline - self._monotonic_clock()
            remaining_seconds = min(
                claim_remaining,
                coordinator_remaining,
                float(self._analyst_profile.deadline_seconds),
            )
            if not 0 < remaining_seconds <= self._analyst_profile.deadline_seconds:
                return self._persist_degraded(
                    escalation, MultiAgentFailureCode.COORDINATOR_TIMEOUT
                )
            result = await asyncio.wait_for(
                self._analyst_runner.run(task), timeout=remaining_seconds
            )
            # 外部 Analyst 返回后，响应本身与后续结构化校验都仍受同一五秒窗口限制。
            # 先检查可阻断已过期的错误响应被解析为其他失败码，从而保持审计事实准确。
            if not self._coordinator_budget_available(coordinator_deadline):
                return self._persist_degraded(
                    escalation, MultiAgentFailureCode.COORDINATOR_TIMEOUT
                )
            analysis = self._analysis_from_result(bundle, escalation, task, result)
            # Analysis 的 Pydantic、证据与触发码验证属于模型派生事实的写入前工作；
            # 即使输出结构完全合法，只要这一步耗尽总预算就不得 append 新的 Analysis。
            if not self._coordinator_budget_available(coordinator_deadline):
                return self._persist_degraded(
                    escalation, MultiAgentFailureCode.COORDINATOR_TIMEOUT
                )
            after_analysis = self._store.append_conflict_analysis(
                analysis, expected_workspace_version=after_escalation.version
            )
            return await self._plan_after_analysis(
                bundle=bundle,
                escalation=escalation,
                analysis=analysis,
                expected_workspace_version=after_analysis.version,
                coordinator_deadline=coordinator_deadline,
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
                and normalized.profile_digest == self._analyst_profile.profile_digest
                and actual.profile_digest == self._analyst_profile.profile_digest
                and actual.allowed_skill_ids == ()
                and actual.max_model_calls == 1
                and actual.max_skill_calls == 0
            )
        except Exception:
            return False

    async def _plan_after_analysis(
        self,
        *,
        bundle: EvidenceBundle,
        escalation: EscalationRecord,
        analysis: ConflictAnalysis,
        expected_workspace_version: int,
        coordinator_deadline: float,
    ) -> HighConflictCoordinationResult:
        """在已落库 Analysis 后可选地调用一次 Planner，整份 Proposal 通过才允许 READY。"""

        # 没有显式冻结 Planner 装配时维持 Task 5 的分析止点，避免旧调用路径因为新增
        # 能力而默默获得第二次模型调用或 READY 语义；Phase 16 的默认路由仍不启用它。
        if self._planner_runner is None:
            return HighConflictCoordinationResult(
                selected=True, escalation=escalation, analysis=analysis
            )
        if self._store.get_workspace(bundle.live_session_id).view is not WorkspaceView.LIVE:
            return HighConflictCoordinationResult(
                selected=True, escalation=escalation, analysis=analysis
            )
        task = self._build_planner_task(bundle, escalation, analysis)
        if not self._planner_profile_matches(task):
            return self._persist_degraded(
                escalation, MultiAgentFailureCode.PLANNER_INVALID_OUTPUT, analysis=analysis
            )
        try:
            coordinator_remaining = coordinator_deadline - self._monotonic_clock()
            if not 0 < coordinator_remaining <= COORDINATOR_DEADLINE_SECONDS:
                return self._persist_degraded(
                    escalation,
                    MultiAgentFailureCode.COORDINATOR_TIMEOUT,
                    analysis=analysis,
                )
            claim, is_new_claim, is_active_claim = self._store.claim_planner_dispatch(
                escalation_id=escalation.escalation_id,
                analysis_id=analysis.analysis_id,
                analysis_digest=analysis.analysis_digest,
                task_digest=task.task_digest,
                now=self._utc_now(),
                lease_seconds=self._planner_profile.deadline_seconds,
            )
            if claim.task_digest != task.task_digest:
                return self._persist_degraded(
                    escalation, MultiAgentFailureCode.PLANNER_INVALID_OUTPUT, analysis=analysis
                )
            if not is_new_claim:
                if is_active_claim:
                    return HighConflictCoordinationResult(
                        selected=True, escalation=escalation, analysis=analysis
                    )
                return self._persist_degraded(
                    escalation,
                    MultiAgentFailureCode.COORDINATOR_TIMEOUT,
                    analysis=analysis,
                    # 已存在的 Planner claim 证明第二段请求已经离开进程。若此后第一
                    # 次终态 CAS 与运营的 LIVE->REVIEW 切换竞争，D-150 只允许同次
                    # 重建无父链的超时闭合，绝不能返回半成品 Analysis 或重发 Planner。
                    allow_review_terminalization=True,
                )
            claim_remaining = self._store.get_planner_dispatch_remaining_seconds(
                claim.escalation_id
            )
            remaining_seconds = min(
                claim_remaining,
                coordinator_deadline - self._monotonic_clock(),
                float(self._planner_profile.deadline_seconds),
            )
            if not 0 < remaining_seconds <= self._planner_profile.deadline_seconds:
                return self._persist_degraded(
                    escalation, MultiAgentFailureCode.COORDINATOR_TIMEOUT, analysis=analysis
                )
            # Planner Profile 是固定两秒、2800 token、0.07 CNY；claim、端到端窗口与
            # Profile deadline 三者取最小值，阻断错误适配器放大外部等待时间。
            result = await asyncio.wait_for(
                self._planner_runner.run(task),
                timeout=remaining_seconds,
            )
            # 外部 Planner 虽然在发送时受剩余预算约束，但响应返回后仍可能已经越过
            # 协调器总时限。先阻断 Validator，避免 CPU 校验和后续落库把迟到结果变成
            # 可供运营使用的经营建议。
            if not self._coordinator_budget_available(coordinator_deadline):
                return self._persist_degraded(
                    escalation,
                    MultiAgentFailureCode.COORDINATOR_TIMEOUT,
                    analysis=analysis,
                )
            proposal = self._proposal_from_result(bundle, escalation, analysis, task, result)
            # 整份 Proposal 的 Schema、证据、备品和风险校验也计入同一个端到端窗口。
            # 在持久化 Proposal 前再次检查，确保超时的验证结果不会形成新的父事实。
            if not self._coordinator_budget_available(coordinator_deadline):
                return self._persist_degraded(
                    escalation,
                    MultiAgentFailureCode.COORDINATOR_TIMEOUT,
                    analysis=analysis,
                )
            proposal_fact = self._proposal_fact(proposal, escalation)
            after_proposal = self._store.append_multi_agent_proposal(
                proposal_fact, expected_workspace_version=expected_workspace_version
            )
            return self._append_ready_outcome(
                escalation=escalation,
                analysis=analysis,
                proposal=proposal,
                expected_workspace_version=after_proposal.version,
                coordinator_deadline=coordinator_deadline,
            )
        except asyncio.TimeoutError:
            # wait_for 既可能由 Planner 自身两秒 Profile/claim 窗口触发，也可能由
            # Coordinator 五秒总预算截断。后者表示跨段延迟耗尽而非模型质量错误，必须
            # 保留 COORDINATOR_TIMEOUT，才能按 D-150/D-152 在 REVIEW 审计未知响应。
            failure_code = (
                MultiAgentFailureCode.COORDINATOR_TIMEOUT
                if not self._coordinator_budget_available(coordinator_deadline)
                else MultiAgentFailureCode.PLANNER_MODEL_ERROR
            )
            return self._persist_degraded(
                escalation,
                failure_code,
                analysis=analysis,
                allow_review_terminalization=(
                    failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT
                ),
            )
        except _PlannerResultError as error:
            return self._persist_degraded(
                escalation, error.failure_code, analysis=analysis
            )
        except Exception:
            # Proposal/Outcome 的持久化冲突同样不能变成重试模型调用；已写入的 Proposal
            # 或 READY Outcome 优先由恢复逻辑返回，否则只把现有 Analyst 事实降级闭合。
            recovered = self._recover_existing(escalation)
            if recovered is not None:
                return recovered
            return self._persist_degraded(
                escalation, MultiAgentFailureCode.VALIDATOR_REJECTED, analysis=analysis
            )

    def _planner_profile_matches(self, task: AgentTask) -> bool:
        """发送前核对 Planner 的完整冻结 Profile，拒绝同名同版本的错误装配。"""

        assert self._planner_runner is not None
        try:
            actual = self._planner_runner.resolve_profile(task)
            normalized = SpecialistProfile.model_validate(actual.model_dump(mode="json"))
            return (
                type(actual) is SpecialistProfile
                and normalized.profile_digest == self._planner_profile.profile_digest
                and actual.profile_digest == self._planner_profile.profile_digest
                and actual.allowed_skill_ids == ()
                and actual.max_model_calls == 1
                and actual.max_skill_calls == 0
                and actual.deadline_seconds == 2
                and actual.max_total_tokens == 2800
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

        analyses = [
            item
            for item in self._store.list_conflict_analyses(escalation.live_session_id)
            if item.escalation_id == escalation.escalation_id
        ]
        if len(analyses) > 1:
            raise WorkspaceConflictError("escalation has multiple analyses")
        analysis = analyses[0] if analyses else None

        proposals: list[LiveDecisionProposal] = []
        for proposal_fact in self._store.list_proposals(escalation.live_session_id):
            try:
                proposal = LiveDecisionProposal.model_validate(
                    _plain_json(proposal_fact.snapshot)
                )
            except Exception:
                # 历史单 Copilot 的通用 Proposal 不一定属于 LiveDecisionProposal 协议；
                # 它们不是本次升级的恢复候选，不能被错误地视为存储损坏。
                continue
            lineage = proposal.multi_agent_lineage
            if (
                proposal.proposal_origin is ProposalOrigin.MULTI_AGENT
                and lineage is not None
                and lineage.escalation_id == escalation.escalation_id
            ):
                proposals.append(proposal)
        if len(proposals) > 1:
            raise WorkspaceConflictError("escalation has multiple multi-agent proposals")
        proposal = proposals[0] if proposals else None

        outcomes = [
            item
            for item in self._store.list_multi_agent_outcomes(escalation.live_session_id)
            if item.escalation_id == escalation.escalation_id
        ]
        if len(outcomes) > 1:
            raise WorkspaceConflictError("escalation has multiple outcomes")
        if outcomes:
            outcome = outcomes[0]
            if outcome.status is MultiAgentOutcomeStatus.READY:
                if analysis is None or proposal is None or outcome.proposal_id is None:
                    raise WorkspaceConflictError("READY outcome parent is incomplete")
                if proposal.proposal_id != outcome.proposal_id:
                    raise WorkspaceConflictError("READY outcome proposal is invalid")
            return HighConflictCoordinationResult(
                selected=True,
                escalation=escalation,
                analysis=analysis,
                proposal=proposal,
                outcome=outcome,
            )
        if proposal is not None and analysis is None:
            raise WorkspaceConflictError("multi-agent proposal parent analysis is incomplete")
        if analysis is not None:
            return HighConflictCoordinationResult(
                selected=True,
                escalation=escalation,
                analysis=analysis,
                proposal=proposal,
            )
        return None

    def _append_ready_outcome(
        self,
        *,
        escalation: EscalationRecord,
        analysis: ConflictAnalysis,
        proposal: LiveDecisionProposal,
        expected_workspace_version: int,
        coordinator_deadline: float,
    ) -> HighConflictCoordinationResult:
        """在已持久化 Proposal 后补写唯一 READY Outcome，重启恢复绝不重新调用 Planner。"""

        # 五秒协调窗口覆盖模型返回后的全部确定性校验和事实写入。Proposal 已经是
        # append-only 父事实时，窗口耗尽只能以降级 Outcome 闭合，绝不能再把迟到的
        # 经营建议标记为 READY 或让恢复路径重新调用 Planner。
        if not self._coordinator_budget_available(coordinator_deadline):
            return self._persist_degraded(
                escalation,
                MultiAgentFailureCode.COORDINATOR_TIMEOUT,
                analysis=analysis,
            )

        outcome = MultiAgentOutcome(
            outcome_id=f"phase16-outcome:{escalation.escalation_id}",
            idempotency_key=f"phase16-outcome:{escalation.escalation_id}",
            escalation_id=escalation.escalation_id,
            live_session_id=escalation.live_session_id,
            incident_id=escalation.incident_id,
            escalation_digest=escalation.escalation_digest,
            evidence_bundle_id=escalation.evidence_bundle_id,
            evidence_bundle_digest=escalation.evidence_bundle_digest,
            status=MultiAgentOutcomeStatus.READY,
            analysis_id=analysis.analysis_id,
            analysis_digest=analysis.analysis_digest,
            proposal_id=proposal.proposal_id,
            proposal_digest=canonical_json_sha256(proposal.model_dump(mode="json")),
            fact_summary=(
                "Evidence analysis and deterministic proposal validation completed; "
                "operator decision is required."
            ),
            created_at=self._utc_now(),
        )
        try:
            self._store.append_multi_agent_outcome(
                outcome, expected_workspace_version=expected_workspace_version
            )
        except Exception:
            # 事实可能已由并发调用方成功提交；只有 Store 返回已存在终态才能恢复，不能
            # 因 Outcome 失败回到 Planner 发送路径。
            recovered = self._recover_existing(escalation)
            if recovered is not None and recovered.outcome is not None:
                return recovered
            raise
        return HighConflictCoordinationResult(
            selected=True,
            escalation=escalation,
            analysis=analysis,
            proposal=proposal,
            outcome=outcome,
        )

    def _build_analyst_task(
        self, bundle: EvidenceBundle, escalation: EscalationRecord
    ) -> AgentTask:
        """只把 Bundle 快照、父身份与完整 Bundle 引用交给 Analyst，不传 Store 或命令面。"""

        snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
        references = tuple(component.reference for component in snapshot.components)
        return AgentTask(
            task_id=f"phase16-analyst:{escalation.escalation_id}",
            task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
            profile_id=self._analyst_profile.profile_id,
            profile_version=self._analyst_profile.profile_version,
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

    def _build_planner_task(
        self,
        bundle: EvidenceBundle,
        escalation: EscalationRecord,
        analysis: ConflictAnalysis,
    ) -> AgentTask:
        """只把同一 Bundle 与已验证 Analysis 交给 Planner，不传 Store、Skill 或命令对象。"""

        snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
        references = tuple(component.reference for component in snapshot.components)
        return AgentTask(
            task_id=f"phase16-planner:{escalation.escalation_id}:{analysis.analysis_id}",
            task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING,
            profile_id=self._planner_profile.profile_id,
            profile_version=self._planner_profile.profile_version,
            room_id=snapshot.scope.room_id,
            trace_id=snapshot.scope.trace_id,
            objective="Generate one to three bounded options for human operator review.",
            input_snapshot={
                # Planner 的稳定 task_id 已绑定 escalation_id；模型正文只读取完整
                # EvidenceBundle 与已经确定性验证的 Analysis，不能接触 operator、模式或
                # 幂等控制字段，避免把控制面事实带入建议生成和可回显输出。
                "analysis": analysis.model_dump(mode="json"),
                "evidence_bundle": _plain_json(bundle.snapshot),
            },
            initial_evidence_refs=references,
        )

    def _proposal_from_result(
        self,
        bundle: EvidenceBundle,
        escalation: EscalationRecord,
        analysis: ConflictAnalysis,
        task: AgentTask,
        result: AgentResult,
    ) -> LiveDecisionProposal:
        """把 Planner 的封闭 options 重建为完整 Proposal，并在落库前做整份确定性验证。"""

        if (
            result.task_id != task.task_id
            or result.profile_id != self._planner_profile.profile_id
            or result.profile_version != self._planner_profile.profile_version
        ):
            raise _PlannerResultError(MultiAgentFailureCode.PLANNER_INVALID_OUTPUT)
        if result.status is not AgentResultStatus.SUCCEEDED:
            raise _PlannerResultError(self._planner_failure_code_for_result(result))
        try:
            output = _plain_json(result.output)
            if not isinstance(output, dict) or set(output) != {"options"}:
                raise ValueError("planner output must contain only options")
            options = tuple(DecisionOption.model_validate(item) for item in output["options"])
            if not 1 <= len(options) <= 3 or len({item.option_id for item in options}) != len(options):
                raise ValueError("planner options must be one to three unique values")
            snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
            references = tuple(component.reference for component in snapshot.components)
            if tuple(result.evidence_refs) != references:
                raise ValueError("planner result evidence refs do not match bundle")
            inventory_component = next(
                component
                for component in snapshot.components
                if component.role is EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT
            )
            if not isinstance(inventory_component.payload, ProductInventoryPayload):
                raise ValueError("planner inventory evidence is invalid")
            available_backups = {
                item.product_id
                for item in inventory_component.payload.backup_products
                if item.is_active and item.inventory > 0
            }
            required_analysis_risks = {item.value for item in analysis.risk_codes}
            for option in options:
                if tuple(option.evidence_refs) != references:
                    raise ValueError("planner option evidence refs do not match bundle")
                risk_flags = set(option.risk_flags)
                if "HUMAN_CONFIRMATION_REQUIRED" not in risk_flags:
                    raise ValueError("planner option omits human confirmation risk")
                if not required_analysis_risks.issubset(risk_flags):
                    raise ValueError("planner option omits analysis risk")
                if option.product_strategy is ProductStrategy.SWITCH_TO_BACKUP:
                    if option.backup_product_id not in available_backups:
                        raise ValueError("planner option backup is unavailable")
                    if "BACKUP_PRODUCT_REQUIRES_CONFIRMATION" not in risk_flags:
                        raise ValueError("planner backup omits confirmation risk")
            if not snapshot.proposal_eligible or snapshot.valid_until <= self._utc_now():
                raise ValueError("planner evidence is no longer eligible")
            lineage = MultiAgentProposalLineage(
                escalation_id=escalation.escalation_id,
                escalation_digest=escalation.escalation_digest,
                analysis_id=analysis.analysis_id,
                analysis_digest=analysis.analysis_digest,
                evidence_bundle_id=bundle.evidence_bundle_id,
                evidence_bundle_digest=snapshot.bundle_digest,
                evidence_refs=references,
                planner_profile_id=self._planner_profile.profile_id,
                planner_profile_version=self._planner_profile.profile_version,
                planner_profile_digest=self._planner_profile.profile_digest,
            )
            return LiveDecisionProposal(
                proposal_id=f"phase16-proposal:{escalation.escalation_id}",
                live_session_id=escalation.live_session_id,
                incident_id=escalation.incident_id,
                trace_id=snapshot.scope.trace_id,
                evidence_bundle_id=bundle.evidence_bundle_id,
                evidence_bundle_digest=snapshot.bundle_digest,
                proposal_origin=ProposalOrigin.MULTI_AGENT,
                status=ProposalStatus.READY,
                options=options,
                evidence_refs=references,
                multi_agent_lineage=lineage,
            )
        except _PlannerResultError:
            raise
        except Exception as exc:
            raise _PlannerResultError(MultiAgentFailureCode.VALIDATOR_REJECTED) from exc

    def _proposal_fact(
        self, proposal: LiveDecisionProposal, escalation: EscalationRecord
    ) -> Proposal:
        """把已验证的领域 Proposal 封装为 append-only Store 事实，调用方不能自报版本或 Profile。"""

        return Proposal(
            proposal_id=proposal.proposal_id,
            live_session_id=proposal.live_session_id,
            incident_id=proposal.incident_id,
            evidence_bundle_id=proposal.evidence_bundle_id,
            proposal_key=f"phase16-proposal:{escalation.escalation_id}",
            proposal_version=1,
            profile_id=self._planner_profile.profile_id,
            profile_version=self._planner_profile.profile_version,
            idempotency_key=f"phase16-proposal:{escalation.escalation_id}",
            snapshot=proposal.model_dump(mode="json"),
            created_at=self._utc_now(),
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
            or result.profile_id != self._analyst_profile.profile_id
            or result.profile_version != self._analyst_profile.profile_version
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
                analyst_profile_id=self._analyst_profile.profile_id,
                analyst_profile_version=self._analyst_profile.profile_version,
                analyst_profile_digest=self._analyst_profile.profile_digest,
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
        *,
        analysis: ConflictAnalysis | None = None,
        allow_review_terminalization: bool = False,
    ) -> HighConflictCoordinationResult:
        """以稳定身份追加唯一降级结果；已有终态优先返回，避免第二次写入或模型重试。"""

        recovered = self._recover_existing(escalation)
        # Analysis 是可继续交给 Planner 的中间事实，不是失败闭合。只有已经存在 Outcome
        # 才能阻止本次降级写入；否则 Planner 的无效输出会留下孤立 Analysis 且无法审计。
        if recovered is not None and recovered.outcome is not None:
            return recovered
        for attempt in range(2):
            workspace = self._store.get_workspace(escalation.live_session_id)
            if (
                workspace.view is WorkspaceView.REVIEW
                and analysis is not None
                and not allow_review_terminalization
            ):
                # 带 Analysis 的局部校验失败不能借 REVIEW 新增终态；保留现有
                # append-only 事实并 fail-closed，等待人工而不伪造模型超时审计。反之，
                # 已离开进程但尚未生成 Analysis 的 Analyst claim 可由数据库 trigger
                # 验证后补写无父链降级审计，避免超时事实在跨视图竞争中永久悬空。
                return recovered or HighConflictCoordinationResult(
                    selected=True, escalation=escalation
                )
            # LIVE -> REVIEW 可在读取 Workspace 与 CAS 写入之间发生。每次重试均从
            # Store 的最新投影重建 Outcome：REVIEW 的降级终态绝不携带已失效的
            # Analysis/Proposal 父链，避免把直播期模型事实错误投影到播后审计。
            outcome_analysis = (
                None if workspace.view is WorkspaceView.REVIEW else analysis
            )
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
                analysis_id=(
                    None if outcome_analysis is None else outcome_analysis.analysis_id
                ),
                analysis_digest=(
                    None if outcome_analysis is None else outcome_analysis.analysis_digest
                ),
                failure_code=failure_code,
                fact_summary=(
                    "Evidence analyst unavailable; deterministic protection remains active and "
                    "operator review is required."
                ),
                created_at=self._utc_now(),
            )
            try:
                self._store.append_multi_agent_outcome(
                    outcome, expected_workspace_version=workspace.version
                )
                return HighConflictCoordinationResult(
                    selected=True,
                    escalation=escalation,
                    analysis=outcome_analysis,
                    outcome=outcome,
                )
            except WorkspaceConflictError:
                # claim 到期后运营可进入 REVIEW。首次读版本与写入之间若恰好发生该
                # 迁移，只重新读取并重试同一条 append-only DEGRADED 事实一次；不重发
                # 模型、不改写 payload，也不把这条受限恢复扩大到 Analysis 或 Proposal。
                recovered = self._recover_existing(escalation)
                if recovered is not None and recovered.outcome is not None:
                    return recovered
                if attempt == 0:
                    continue
                raise
            except Exception:
                recovered = self._recover_existing(escalation)
                if recovered is not None:
                    return recovered
                raise

    def _coordinator_budget_available(self, coordinator_deadline: float) -> bool:
        """统一判定协调器总窗口是否仍可安全产生新的模型派生事实。"""

        return (
            0
            < coordinator_deadline - self._monotonic_clock()
            <= COORDINATOR_DEADLINE_SECONDS
        )

    @staticmethod
    def _failure_code_for_result(result: AgentResult) -> MultiAgentFailureCode:
        """将共享 Runner 的开放失败状态压缩为 Phase 16 可审计的封闭代码。"""

        if result.status is AgentResultStatus.BUDGET_EXCEEDED:
            return MultiAgentFailureCode.ANALYST_BUDGET_EXCEEDED
        if result.status is AgentResultStatus.MODEL_ERROR:
            return MultiAgentFailureCode.ANALYST_MODEL_ERROR
        return MultiAgentFailureCode.ANALYST_INVALID_OUTPUT

    @staticmethod
    def _planner_failure_code_for_result(result: AgentResult) -> MultiAgentFailureCode:
        """把共享 Runner 的 Planner 失败状态收束为封闭审计代码，禁止泄漏自由错误详情。"""

        if result.status is AgentResultStatus.BUDGET_EXCEEDED:
            return MultiAgentFailureCode.PLANNER_BUDGET_EXCEEDED
        if result.status is AgentResultStatus.MODEL_ERROR:
            return MultiAgentFailureCode.PLANNER_MODEL_ERROR
        return MultiAgentFailureCode.PLANNER_INVALID_OUTPUT

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


class _PlannerResultError(RuntimeError):
    """把 Planner 身份、Schema 和整份确定性校验失败传递为安全的固定失败码。"""

    def __init__(self, failure_code: MultiAgentFailureCode) -> None:
        super().__init__(failure_code.value)
        self.failure_code = failure_code
