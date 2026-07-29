"""Phase 16 V5 受控双 Agent 真实模型 E2E 的独立运行协议。

V5 的职责是把既有 V2 的只读证据投影、共享 ``BoundedSpecialistRunner`` 与语义校验器
组合为一个新的、禁思考的审计 campaign。这个模块不修改 V1 至 V4 的 Profile、Manifest、
账本或运行器，也不注册到生产 LIVE 路由；它只为显式命令入口提供独立的离线预检和零重试
执行边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import ConfigDict, Field, model_validator

from src.decision_support.multi_agent import (
    _SMOKE_V2_CONFLICT_ANALYSIS_RESULT_SCHEMA,
    _SMOKE_V2_LIVE_DECISION_PLANNING_RESULT_SCHEMA,
    _build_profile,
    validate_v2_conflict_analysis_result,
    validate_v2_live_decision_planner_result,
)
from src.decision_support.multi_agent_evaluation import (
    Phase16EvaluationCase,
    Phase16EvaluationDataset,
    _assemble_bundle,
)
from src.decision_support.official_smoke_evidence_v2 import (
    FORMAL_INPUT_PRICE_CNY_PER_MILLION,
    FORMAL_OFFICIAL_SMOKE_MANIFEST_PATH,
    FORMAL_OUTPUT_PRICE_CNY_PER_MILLION,
    load_phase16_official_smoke_v2_evidence_manifest,
    load_phase16_official_smoke_v2_parent_dataset,
)
from src.decision_support.official_smoke_runner_v2 import (
    Phase16OfficialSmokeV2CaseProjection,
    _opaque_case_key,
    _projection_evidence_registry,
    _synthetic_live_parents,
    build_phase16_official_smoke_v2_case_projection,
)
from src.decision_support.evidence import EvidenceBundleSnapshot, ProductInventoryPayload
from src.decision_support.store import derive_automatic_escalation_codes
from src.decision_support.controlled_e2e_ledger_v5 import (
    Phase16V5CaseOutcomeStatus,
    Phase16V5DispatchStage,
    Phase16V5RunKind,
    Phase16V5RunStatus,
    Phase16V5ValidationVerdict,
)
from src.specialist_runtime.budget import BudgetInvariantError
from src.specialist_runtime.model_port import AgentModelPort, ModelFailure, ModelSuccess
from src.specialist_runtime.models import (
    AgentResult,
    AgentTask,
    SpecialistTaskKind,
    StrictFrozenModel,
    _plain_json,
    canonical_json_sha256,
)
from src.specialist_runtime.profiles import (
    DEEPSEEK_V4_PRO_MODEL_ID,
    FORMAL_ENDPOINT_HOST,
    FinalEvidenceBindingMode,
    SpecialistProfile,
)
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
from src.specialist_runtime.runner import BoundedSpecialistRunner
from src.decision_support.controlled_e2e_adapter_v5 import DeepSeekV5ThinkingMode


PHASE16_V5_CAMPAIGN_ID = "phase16-v5-controlled-e2e"
PHASE16_V5_CALIBRATION_RUN_ID = "phase16-v5-calibration-001"
PHASE16_V5_FORMAL_RUN_ID = "phase16-v5-formal-001"
PHASE16_V5_MANIFEST_ID = "phase16-v5-controlled-e2e-v1"
PHASE16_V5_MANIFEST_PATH = Path(
    "evaluation/manifests/phase16-v5-controlled-e2e-v1.json"
)
PHASE16_V5_CALIBRATION_INPUT_PATH = Path(
    "evaluation/manifests/phase16-v5-controlled-e2e-calibration-v1.json"
)
PHASE16_V5_ANALYST_PROFILE_ID = "phase16_v5_evidence_analyst"
PHASE16_V5_PLANNER_PROFILE_ID = "phase16_v5_decision_planner"
PHASE16_V5_PROFILE_VERSION = "5.0.0"
PHASE16_V5_TOTAL_BUDGET_CNY = Decimal("1.000000")
PHASE16_V5_STAGE_RESERVATION_CNY = Decimal("0.030000")
PHASE16_V5_FORMAL_CASE_COUNT = 10
PHASE16_V5_MAX_TOTAL_TOKENS = 6000
PHASE16_V5_MAX_OUTPUT_TOKENS = 2800
PHASE16_V5_DEADLINE_SECONDS = 60

# V5 清晰列出其新执行闭包。V2 模块和其 Manifest 只作为只读父证据，因此不会因为 V5
# 修订而被重签；V4 Adapter 的源码摘要则证明本次请求确实由禁思考装饰器发出。
PHASE16_V5_EXECUTION_IDENTITY_PATHS = (
    "docker/init_phase16_v5_controlled_e2e.sql",
    "scripts/run_phase16_v5_controlled_e2e.py",
    "src/decision_support/controlled_e2e_adapter_v5.py",
    "src/decision_support/controlled_e2e_ledger_v5.py",
    "src/decision_support/controlled_e2e_v5.py",
)


class Phase16V5Error(RuntimeError):
    """V5 的稳定边界错误，禁止携带数据库异常、模型正文或敏感配置。"""


class Phase16V5EvidenceConclusion(StrEnum):
    """区分外部调用尚无结论、已失败与严格 E2E 合格三种可披露状态。"""

    INCONCLUSIVE = "INCONCLUSIVE"
    FAILED = "FAILED"
    CONTROLLED_E2E_QUALIFIED = "CONTROLLED_E2E_QUALIFIED"


class Phase16V5ExecutionStatus(StrEnum):
    """命令和报告使用的运行状态，不表达生产上线或经营授权。"""

    DRY_RUN = "DRY_RUN"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    PASS = "PASS"


@dataclass(frozen=True)
class Phase16V5CaseExecution:
    """一个 V5 slot 的脱敏运行结论，完整 Provider ID 和模型正文均不在这里保存。"""

    case_id: str
    status: Phase16V5ExecutionStatus
    reason_code: str
    analyst_attempt_id: str | None
    planner_attempt_id: str | None


@dataclass(frozen=True)
class Phase16V5ExecutionReport:
    """V5 Runner 返回的进程内摘要，长期报告必须由账本事实重新渲染。"""

    campaign_id: str
    run_id: str
    run_kind: Phase16V5RunKind
    status: Phase16V5ExecutionStatus
    evidence_conclusion: Phase16V5EvidenceConclusion
    reason_codes: tuple[str, ...]
    case_executions: tuple[Phase16V5CaseExecution, ...]
    model_calls: int


class Phase16V5CalibrationInput(StrictFrozenModel):
    """独立于正式十例的合成校准输入及其可复验摘要。

    校准只验证真实 API、JSON 协议、双阶段顺序与账本链路，不得复用正式评分 slot 的
    任一 case 投影。这里保存的全部字段都是合成库存/节奏事实，不包含用户、订单或标签。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    calibration_case_id: str = Field(
        default="phase16-v5-synthetic-calibration-001",
        pattern=r"^phase16-v5-synthetic-calibration-[0-9]{3}$",
    )
    case: Phase16EvaluationCase
    # 空字符串仅允许出现在首次重建前的源码模板；加载后的验证器会立即以 canonical JSON
    # 填充并冻结两个摘要，因此运行时对象不可能携带空摘要进入 Manifest 或网络路径。
    case_digest: str = ""
    calibration_payload_digest: str = ""

    @model_validator(mode="after")
    def _bind_synthetic_identity(self) -> "Phase16V5CalibrationInput":
        """拒绝将正式 slot、标签或未签名的 case 替换为所谓 calibration。"""

        if self.case.case_id.startswith("phase16-v5-"):
            raise ValueError("V5 calibration model case must use the governed synthetic case shape")
        case_digest = canonical_json_sha256(self.case.model_dump(mode="json"))
        if self.case_digest and len(self.case_digest) != 64:
            raise ValueError("V5 calibration case digest is invalid")
        if self.case_digest and self.case_digest != case_digest:
            raise ValueError("V5 calibration case digest does not match synthetic case")
        payload = self.model_dump(
            mode="json",
            exclude={"case_digest", "calibration_payload_digest"},
        )
        payload_digest = canonical_json_sha256(payload)
        if self.calibration_payload_digest and len(self.calibration_payload_digest) != 64:
            raise ValueError("V5 calibration payload digest is invalid")
        if self.calibration_payload_digest and self.calibration_payload_digest != payload_digest:
            raise ValueError("V5 calibration payload digest does not match synthetic input")
        object.__setattr__(self, "case_digest", case_digest)
        object.__setattr__(self, "calibration_payload_digest", payload_digest)
        return self


class Phase16V5Manifest(StrictFrozenModel):
    """冻结 V5 campaign 的公开身份，不保存 Prompt、真实证据正文或环境凭据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = PHASE16_V5_MANIFEST_ID
    schema_version: str = "1.0.0"
    campaign_id: str = PHASE16_V5_CAMPAIGN_ID
    model_id: str = DEEPSEEK_V4_PRO_MODEL_ID
    endpoint_host: str = FORMAL_ENDPOINT_HOST
    thinking_mode: DeepSeekV5ThinkingMode = DeepSeekV5ThinkingMode.DISABLED
    parent_manifest_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    calibration_case_id: str = Field(..., min_length=1)
    calibration_case_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    calibration_payload_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    formal_case_ids: tuple[str, ...] = Field(..., min_length=PHASE16_V5_FORMAL_CASE_COUNT)
    formal_case_digests: dict[str, str]
    profile_digests: dict[str, str]
    input_cny_per_million: Decimal = FORMAL_INPUT_PRICE_CNY_PER_MILLION
    output_cny_per_million: Decimal = FORMAL_OUTPUT_PRICE_CNY_PER_MILLION
    stage_reservation_cny: Decimal = PHASE16_V5_STAGE_RESERVATION_CNY
    total_budget_cny: Decimal = PHASE16_V5_TOTAL_BUDGET_CNY
    source_file_digests: dict[str, str]
    manifest_digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _bind_manifest_identity(self) -> "Phase16V5Manifest":
        """把所有可公开的协议事实纳入摘要，并拒绝篡改过的 slot 或价格边界。"""

        if (
            self.manifest_id != PHASE16_V5_MANIFEST_ID
            or self.campaign_id != PHASE16_V5_CAMPAIGN_ID
            or self.model_id != DEEPSEEK_V4_PRO_MODEL_ID
            or self.endpoint_host != FORMAL_ENDPOINT_HOST
            or self.thinking_mode is not DeepSeekV5ThinkingMode.DISABLED
        ):
            raise ValueError("V5 manifest identity is frozen")
        if len(self.formal_case_ids) != PHASE16_V5_FORMAL_CASE_COUNT:
            raise ValueError("V5 formal run requires exactly ten case slots")
        if len(set(self.formal_case_ids)) != PHASE16_V5_FORMAL_CASE_COUNT:
            raise ValueError("V5 formal case slots must be unique")
        if set(self.formal_case_digests) != set(self.formal_case_ids):
            raise ValueError("V5 case digests must exactly cover formal case slots")
        if self.calibration_case_id in self.formal_case_ids:
            raise ValueError("V5 calibration slot must be independent from formal slots")
        if set(self.profile_digests) != {"analyst", "planner"}:
            raise ValueError("V5 manifest requires analyst and planner profile digests")
        if (
            self.input_cny_per_million != FORMAL_INPUT_PRICE_CNY_PER_MILLION
            or self.output_cny_per_million != FORMAL_OUTPUT_PRICE_CNY_PER_MILLION
            or self.stage_reservation_cny != PHASE16_V5_STAGE_RESERVATION_CNY
            or self.total_budget_cny != PHASE16_V5_TOTAL_BUDGET_CNY
        ):
            raise ValueError("V5 manifest budget or price facts are frozen")
        if set(self.source_file_digests) != set(PHASE16_V5_EXECUTION_IDENTITY_PATHS):
            raise ValueError("V5 source closure is incomplete")
        if any(
            len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
            for digest in (*self.formal_case_digests.values(), *self.profile_digests.values(), *self.source_file_digests.values())
        ):
            raise ValueError("V5 manifest digest field is invalid")
        payload = self.model_dump(mode="json", exclude={"manifest_digest"})
        calculated = canonical_json_sha256(payload)
        if self.manifest_digest and self.manifest_digest != calculated:
            raise ValueError("V5 manifest_digest does not match manifest facts")
        object.__setattr__(self, "manifest_digest", calculated)
        return self


def _v5_profile(
    *,
    profile_id: str,
    task_kind: SpecialistTaskKind,
    prompt_prefix: str,
    result_schema: dict[str, object],
) -> SpecialistProfile:
    """复用冻结的 Profile 构造器生成新身份，避免复制 JSON Schema 或公共协议逻辑。"""

    return _build_profile(
        profile_id=profile_id,
        profile_version=PHASE16_V5_PROFILE_VERSION,
        task_kind=task_kind,
        prompt_prefix=prompt_prefix,
        result_schema=result_schema,
        max_total_tokens=PHASE16_V5_MAX_TOTAL_TOKENS,
        max_output_tokens=PHASE16_V5_MAX_OUTPUT_TOKENS,
        max_case_cost_cny=PHASE16_V5_STAGE_RESERVATION_CNY,
        deadline_seconds=PHASE16_V5_DEADLINE_SECONDS,
        model_id=DEEPSEEK_V4_PRO_MODEL_ID,
        endpoint_host=FORMAL_ENDPOINT_HOST,
        final_envelope_instruction='FINAL envelope: {"kind":"FINAL","final_output":<RESULT>}. ',
        final_evidence_binding_mode=FinalEvidenceBindingMode.SYSTEM_MANAGED_IDS,
    )


def build_phase16_v5_analyst_profile() -> SpecialistProfile:
    """构造 V5 Analyst Profile，示例只展示无真实数据的信封形状。"""

    return _v5_profile(
        profile_id=PHASE16_V5_ANALYST_PROFILE_ID,
        task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
        prompt_prefix=(
            "You are EvidenceAnalystAgent for a controlled E2E qualification. "
            "你只能分析给定证据，不得提出经营动作、调用 Skill、选择路由或声明权限。 "
            "finding_codes 与完整 EvidenceRef 均由系统管理，禁止输出它们；evidence_ids "
            "只能选择输入证据包内可见的 ID。只输出一个 JSON 对象，不得输出 Markdown、"
            "代码块、前缀或推理过程。无真实数据的形状示例："
            '{"kind":"FINAL","final_output":{"constraint_codes":[],"risk_codes":[],"explanation":"brief evidence-grounded explanation","evidence_ids":["bundle-evidence-id"]}}. '
        ),
        result_schema=_SMOKE_V2_CONFLICT_ANALYSIS_RESULT_SCHEMA,
    )


def build_phase16_v5_planner_profile() -> SpecialistProfile:
    """构造 V5 Planner Profile，明确它只能产生待人工审阅的受限候选项。"""

    return _v5_profile(
        profile_id=PHASE16_V5_PLANNER_PROFILE_ID,
        task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING,
        prompt_prefix=(
            "You are DecisionPlannerAgent for a controlled E2E qualification. "
            "只返回一到三个供人工审阅的受限 option，不得调用 Skill、选择路由、执行命令或"
            "声称权限。每个 evidence_ids 只能选择已提供证据包内 ID。只输出一个 JSON 对象，"
            "不得输出 Markdown、代码块、前缀或推理过程。无真实数据的信封形状示例："
            '{"kind":"FINAL","final_output":{"options":[{"option_id":"hold-review","product_strategy":"HOLD_AND_ESCALATE","backup_product_id":null,"host_prompt":"review before action","timing":"AFTER_OPERATOR_CONFIRMATION","risk_flags":["HUMAN_CONFIRMATION_REQUIRED"],"evidence_ids":["bundle-evidence-id"]}]}}. '
        ),
        result_schema=_SMOKE_V2_LIVE_DECISION_PLANNING_RESULT_SCHEMA,
    )


def _source_digest(repository_root: Path, relative_path: str) -> str:
    """读取 V5 自身的普通 UTF-8 LF 文件并计算摘要，拒绝 symlink 和非规范字节。"""

    path = (repository_root / relative_path).resolve()
    if not path.is_file() or path.is_symlink() or repository_root.resolve() not in path.parents:
        raise ValueError("V5 source closure path is invalid")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise ValueError("V5 source closure requires UTF-8 LF without BOM")
    raw.decode("utf-8")
    return sha256(raw).hexdigest()


def load_phase16_v5_calibration_input(*, repository_root: Path) -> Phase16V5CalibrationInput:
    """读取独立冻结的合成校准输入，编码或摘要异常均阻断真实模型发送。"""

    path = repository_root / PHASE16_V5_CALIBRATION_INPUT_PATH
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
            raise ValueError("calibration input encoding is invalid")
        return Phase16V5CalibrationInput.model_validate(json.loads(raw.decode("utf-8")))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise Phase16V5Error("V5 synthetic calibration input is unreadable") from error


def build_phase16_v5_manifest(
    *, repository_root: Path, dataset: Phase16EvaluationDataset
) -> Phase16V5Manifest:
    """从只读 V2 父数据和当前 V5 新模块重建可复验的 campaign Manifest。"""

    parent_manifest = load_phase16_official_smoke_v2_evidence_manifest(
        repository_root=repository_root
    )
    case_ids = dataset.manifest.smoke_eligible_case_ids
    if tuple(case_ids) != tuple(parent_manifest.case_ids):
        raise ValueError("V5 parent dataset no longer matches the frozen V2 case set")
    if len(case_ids) != PHASE16_V5_FORMAL_CASE_COUNT:
        raise ValueError("V5 parent dataset must expose exactly ten formal cases")
    calibration = load_phase16_v5_calibration_input(repository_root=repository_root)
    if calibration.case.case_id in case_ids:
        raise ValueError("V5 synthetic calibration must not reuse a formal case")
    analyst = build_phase16_v5_analyst_profile()
    planner = build_phase16_v5_planner_profile()
    return Phase16V5Manifest(
        parent_manifest_digest=parent_manifest.manifest_digest,
        # 校准拥有单独冻结的合成输入和摘要，既不占用正式 slot，也不能通过改变正式数据集
        # 来重签。它只证明 API 集成与协议闭环，不能为十例正式结果提供训练或预演机会。
        calibration_case_id=calibration.calibration_case_id,
        calibration_case_digest=calibration.case_digest,
        calibration_payload_digest=calibration.calibration_payload_digest,
        formal_case_ids=tuple(case_ids),
        formal_case_digests={
            case_id: dataset.manifest.case_digests[case_id] for case_id in case_ids
        },
        profile_digests={"analyst": analyst.profile_digest, "planner": planner.profile_digest},
        source_file_digests={
            path: _source_digest(repository_root, path)
            for path in PHASE16_V5_EXECUTION_IDENTITY_PATHS
        },
    )


def load_phase16_v5_manifest(*, repository_root: Path) -> Phase16V5Manifest:
    """加载版本化 V5 Manifest，文件错误只能阻断外部发送，不能回退到内存配置。"""

    path = repository_root / PHASE16_V5_MANIFEST_PATH
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
            raise ValueError("manifest encoding is invalid")
        return Phase16V5Manifest.model_validate(json.loads(raw.decode("utf-8")))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise Phase16V5Error("V5 manifest is unreadable") from error


def load_phase16_v5_parent_dataset(*, repository_root: Path) -> Phase16EvaluationDataset:
    """只通过 V2 的冻结加载器读取父评估数据，禁止 V5 偷换数据集解释。"""

    return load_phase16_official_smoke_v2_parent_dataset(
        repository_root / "evaluation" / "phase16_controlled_multi_agent"
    )


def preflight_phase16_v5(*, repository_root: Path) -> tuple[Phase16V5Manifest | None, tuple[str, ...]]:
    """完成纯本地身份重建；本函数不读 .env、不建立数据库连接也不触发网络。"""

    reasons: list[str] = []
    try:
        dataset = load_phase16_v5_parent_dataset(repository_root=repository_root)
        rebuilt = build_phase16_v5_manifest(repository_root=repository_root, dataset=dataset)
    except Exception:
        rebuilt = None
        reasons.append("MANIFEST_REBUILD_FAILED")
    try:
        stored = load_phase16_v5_manifest(repository_root=repository_root)
    except Phase16V5Error:
        stored = None
        reasons.append("MANIFEST_UNREADABLE")
    if stored is not None and rebuilt is not None and stored.manifest_digest != rebuilt.manifest_digest:
        reasons.append("MANIFEST_IDENTITY_MISMATCH")
    return stored if not reasons else None, tuple(sorted(set(reasons)))


def build_phase16_v5_calibration_projection(
    *,
    repository_root: Path,
    now: datetime,
) -> Phase16OfficialSmokeV2CaseProjection:
    """从独立合成输入重建校准证据 Bundle，不读取或投影正式十例的 case。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("V5 calibration projection requires timezone-aware clock")
    calibration = load_phase16_v5_calibration_input(repository_root=repository_root)
    workspace, incident = _synthetic_live_parents(
        # 外层校准 slot 是审计账本用于隔离正式十例的稳定身份；但六角色证据组件、事件和
        # Incident 的绑定关系必须全部由合成受治理 case 的身份派生。若把 slot ID 传入这里，
        # Assembler 会检测到事件 source_ref 与 ``calibration.case.case_id`` 不一致并 fail-closed。
        # 因而仅内部父事实使用 case ID，模型可见和账本可见的 slot 身份仍保持独立。
        case_id=calibration.case.case_id,
        now=now,
    )
    # 继续复用 Phase 16 正式六角色 Assembler。V5 仅提供独立的合成 case，不复制或
    # 绕过 EvidenceBundle、freshness、trigger derivation 与只读 Resolver 的治理逻辑。
    bundle = _assemble_bundle(
        workspace=workspace,
        incident=incident,
        case=calibration.case,
        now=now,
    )
    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    references = tuple(component.reference for component in snapshot.components)
    trigger_codes = derive_automatic_escalation_codes(bundle)
    if len(trigger_codes) < 2:
        raise ValueError("V5 synthetic calibration does not contain a high-conflict trigger set")
    inventory = next(
        component.payload
        for component in snapshot.components
        if component.role.value == "PRODUCT_INVENTORY_SNAPSHOT"
    )
    if not isinstance(inventory, ProductInventoryPayload):
        raise ValueError("V5 synthetic calibration inventory evidence is invalid")
    key = _opaque_case_key(calibration.calibration_case_id)
    analyst_profile = build_phase16_v5_analyst_profile()
    planner_profile = build_phase16_v5_planner_profile()
    analyst_task = AgentTask(
        task_id=f"v5-calibration-analyst-{key}",
        task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
        profile_id=analyst_profile.profile_id,
        profile_version=analyst_profile.profile_version,
        room_id=snapshot.scope.room_id,
        trace_id=snapshot.scope.trace_id,
        objective="Analyze only governed synthetic sold-out conflict evidence for controlled E2E calibration.",
        input_snapshot={
            "trigger_codes": [code.value for code in trigger_codes],
            "evidence_bundle_digest": snapshot.bundle_digest,
        },
        initial_evidence_refs=references,
    )
    return Phase16OfficialSmokeV2CaseProjection(
        case_id=calibration.calibration_case_id,
        case_digest=calibration.case_digest,
        analyst_task=analyst_task,
        planner_profile_id=planner_profile.profile_id,
        planner_profile_version=planner_profile.profile_version,
        evidence_refs=references,
        evidence_registry=_projection_evidence_registry(snapshot),
        trusted_anchor_id=snapshot.scope.anchor_id,
        trigger_codes=tuple(trigger_codes),
        available_backup_product_ids=frozenset(
            product.product_id
            for product in inventory.backup_products
            if product.is_active and product.inventory > 0
        ),
        proposal_eligible=snapshot.proposal_eligible,
        valid_until=snapshot.valid_until,
        evidence_bundle_digest=snapshot.bundle_digest,
    )


class _NoSkillPort:
    """V5 Profile 为零 Skill；任何意外 Skill 路径必须立即失败而非静默降级。"""

    async def invoke(self, **_kwargs: Any) -> dict[str, Any]:
        """阻断共享 Runner 外的所有工具执行能力。"""

        raise RuntimeError("V5 controlled E2E does not permit Skills")


class _CapturingModelPort:
    """逐次捕获最小调用结果，用于账本保存摘要和判断是否已向 Provider 发送。"""

    def __init__(self, delegate: AgentModelPort) -> None:
        self._delegate = delegate
        self.request: Any | None = None
        self.outcome: ModelSuccess | ModelFailure | None = None

    async def complete(self, request: Any) -> ModelSuccess | ModelFailure:
        """透传唯一模型调用；V5 不在该端口增加 fallback、修补或隐藏重试。"""

        self.request = request
        self.outcome = await self._delegate.complete(request)
        return self.outcome


@dataclass(frozen=True)
class _BudgetClaim:
    """满足共享 Runner 最小预算接口的不可变结果，真实 intent 已在 V5 账本追加。"""

    created: bool


class _V5PricingPolicy:
    """以 V5 Manifest 冻结的 Pro 价格计算保守预约与 Provider usage 实际成本。"""

    policy_digest = canonical_json_sha256(
        {
            "input": str(FORMAL_INPUT_PRICE_CNY_PER_MILLION),
            "output": str(FORMAL_OUTPUT_PRICE_CNY_PER_MILLION),
            "model": DEEPSEEK_V4_PRO_MODEL_ID,
        }
    )

    def count_input_tokens(self, request: Any) -> int:
        """采用确定性字节估算，发送后仍以 Provider usage 作为唯一实际结算依据。"""

        payload = json.dumps(
            request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return max((len(payload) + 3) // 4, 1)

    def worst_case_cost(self, request: Any, profile: SpecialistProfile) -> Decimal:
        """估算值超过冻结阶段预约时在网络前阻断，不能削顶后继续发送。"""

        cost = self._cost(self.count_input_tokens(request), request.max_output_tokens)
        if cost > profile.max_case_cost_cny:
            raise BudgetInvariantError("V5 request exceeds frozen stage reservation")
        return cost

    def actual_cost(self, usage: Any, _profile: SpecialistProfile) -> Decimal:
        """使用供应商返回的 token usage 计算实际费用，拒绝本地猜测。"""

        return self._cost(usage.input_tokens, usage.output_tokens)

    @staticmethod
    def _cost(input_tokens: int, output_tokens: int) -> Decimal:
        raw = (
            Decimal(input_tokens) * FORMAL_INPUT_PRICE_CNY_PER_MILLION
            + Decimal(output_tokens) * FORMAL_OUTPUT_PRICE_CNY_PER_MILLION
        ) / Decimal("1000000")
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


class _V5BudgetAdapter:
    """把共享 Runner 的 reserve/settle 适配为 V5 append-only attempt，而不借用 V2 私有适配器。"""

    def __init__(
        self,
        *,
        ledger: Any,
        run_id: str,
        claim_id: str,
        stage: Phase16V5DispatchStage,
        profile: SpecialistProfile,
    ) -> None:
        self._ledger = ledger
        self._run_id = run_id
        self._claim_id = claim_id
        self._stage = stage
        self._profile = profile
        self.attempt: Any | None = None
        self._request_id: str | None = None

    def reserve(self, request_id: str, candidate: object, amount_cny: Decimal) -> _BudgetClaim:
        """在 HTTP 调用前原子写入唯一 intent，并拒绝动态候选或超出 V5 预约。"""

        if candidate != self._stage.value or self.attempt is not None:
            raise BudgetInvariantError("V5 dispatch candidate is invalid")
        if amount_cny > PHASE16_V5_STAGE_RESERVATION_CNY:
            raise BudgetInvariantError("V5 dispatch reservation exceeds stage cap")
        self.attempt = self._ledger.begin_dispatch(
            run_id=self._run_id,
            claim_id=self._claim_id,
            stage=self._stage,
            profile_digest=self._profile.profile_digest,
            internal_request_id=request_id,
            reservation_cny=amount_cny,
        )
        self._request_id = request_id
        return _BudgetClaim(created=True)

    def settle(self, request_id: str, actual_cost_cny: Decimal | None) -> _BudgetClaim:
        """共享 Runner 的本地结算不拥有费用权威；账本随后依据 receipt 的 usage 再结算。"""

        # 必须保留与共享 BoundedSpecialistRunner 完全一致的 ``actual_cost_cny`` 参数名。
        # 共享运行器以关键字调用该方法；V5 不在这里结算费用，只在 receipt 落库后按 provider
        # usage 计算不可变实际成本，因此此处有意不消费该局部值。
        _ = actual_cost_cny

        if self.attempt is None or request_id != self._request_id:
            raise BudgetInvariantError("V5 settlement has no matching attempt")
        return _BudgetClaim(created=False)

    def release(self, request_id: str) -> _BudgetClaim:
        """deadline 前未发送只释放 Runner 侧状态，append-only intent 仍需由 BLOCKED validation 收口。"""

        if self.attempt is None or request_id != self._request_id:
            raise BudgetInvariantError("V5 release has no matching attempt")
        return _BudgetClaim(created=False)


class _V5Ledger(Protocol):
    """Runner 所需的最小账本协定，便于单元测试使用不联网的严格 Fake。"""

    def ensure_campaign(self, manifest: Phase16V5Manifest) -> None: ...
    def begin_run(self, *, run_id: str, run_kind: Phase16V5RunKind, manifest: Phase16V5Manifest) -> None: ...
    def recover_open_attempts(self) -> tuple[Any, ...]: ...
    def recover_incomplete_cases(self) -> tuple[Any, ...]: ...
    def calibration_passed(self) -> bool: ...
    def claim_case(self, *, run_id: str, case_id: str, case_digest: str): ...
    def begin_dispatch(self, **kwargs: Any): ...
    def append_receipt(self, **kwargs: Any) -> bool: ...
    def append_validation(self, **kwargs: Any) -> None: ...
    def close_case(self, **kwargs: Any) -> None: ...
    def close_run(self, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class _StageExecution:
    """仅在本次进程内传递 Analyst 验证载荷，不能成为长期审计替代品。"""

    passed: bool
    network_sent: bool
    reason_code: str
    attempt_id: str | None
    analysis: Any | None = None


class Phase16V5ControlledE2ERunner:
    """串行执行校准或正式 10-case V5 run，并在第一条失败后永久停止发送。"""

    def __init__(
        self,
        *,
        dataset: Phase16EvaluationDataset,
        manifest: Phase16V5Manifest,
        ledger: _V5Ledger,
        model_port: AgentModelPort,
        clock: Any | None = None,
    ) -> None:
        self._dataset = dataset
        self._manifest = manifest
        self._ledger = ledger
        self._model_port = model_port
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._pricing_policy = _V5PricingPolicy()

    def dry_run(self, *, run_kind: Phase16V5RunKind) -> Phase16V5ExecutionReport:
        """离线确认 V5 身份和校准门，不创建账本行、不装配模型端口也不联网。"""

        reasons = self._static_reasons(run_kind=run_kind)
        return Phase16V5ExecutionReport(
            campaign_id=PHASE16_V5_CAMPAIGN_ID,
            run_id=self._run_id(run_kind),
            run_kind=run_kind,
            status=Phase16V5ExecutionStatus.BLOCKED if reasons else Phase16V5ExecutionStatus.DRY_RUN,
            evidence_conclusion=Phase16V5EvidenceConclusion.INCONCLUSIVE,
            reason_codes=reasons,
            case_executions=(),
            model_calls=0,
        )

    async def execute(self, *, run_kind: Phase16V5RunKind) -> Phase16V5ExecutionReport:
        """执行一轮已授权 V5 run；任何已发送失败立即关闭并绝不继续下一个 case。"""

        reasons = self._static_reasons(run_kind=run_kind)
        run_id = self._run_id(run_kind)
        if reasons:
            return self._report(run_id, run_kind, Phase16V5ExecutionStatus.BLOCKED, reasons, (), 0)
        if getattr(self._model_port, "thinking_mode", None) is not DeepSeekV5ThinkingMode.DISABLED:
            return self._report(run_id, run_kind, Phase16V5ExecutionStatus.BLOCKED, ("THINKING_MODE_MISMATCH",), (), 0)
        try:
            self._ledger.ensure_campaign(self._manifest)
            # 在开始新的 claim 前先处理上次进程留下的网络前 intent。该恢复不会发送模型，
            # 只把未知外部状态封口为 FAILED；当前 run 因而也绝不能继续联网。
            recovered_attempts = self._ledger.recover_open_attempts()
            recovered_cases = self._ledger.recover_incomplete_cases()
            recovered = (*recovered_attempts, *recovered_cases)
            same_run_recovery = next((item for item in recovered if item.run_id == run_id), None)
            if same_run_recovery is not None:
                return self._report(
                    run_id,
                    run_kind,
                    (
                        Phase16V5ExecutionStatus.BLOCKED
                        if same_run_recovery.status is Phase16V5CaseOutcomeStatus.BLOCKED
                        else Phase16V5ExecutionStatus.FAILED
                    ),
                    (same_run_recovery.reason_code,),
                    (),
                    0,
                )
            # 恢复必须先于正式 run 的校准检查：若校准进程曾在写终态前崩溃，本次命令应先把
            # 那条历史事实收口为不可重试结论，而不是只返回一个无法定位的校准门阻断。
            if run_kind is Phase16V5RunKind.FORMAL and not self._ledger.calibration_passed():
                return self._report(run_id, run_kind, Phase16V5ExecutionStatus.BLOCKED, ("CALIBRATION_PASS_REQUIRED",), (), 0)
            projections = self._projections(run_kind=run_kind)
            self._ledger.begin_run(run_id=run_id, run_kind=run_kind, manifest=self._manifest)
        except Exception:
            return self._report(run_id, run_kind, Phase16V5ExecutionStatus.BLOCKED, ("V5_LEDGER_OR_PROJECTION_BLOCKED",), (), 0)

        executions: list[Phase16V5CaseExecution] = []
        model_calls = 0
        for case_id, case_digest, projection in projections:
            try:
                claim = self._ledger.claim_case(run_id=run_id, case_id=case_id, case_digest=case_digest)
            except Exception:
                return self._close_and_report(
                    run_id, run_kind, Phase16V5ExecutionStatus.BLOCKED, "CASE_CLAIM_BLOCKED", executions, model_calls
                )
            analyst = await self._execute_stage(
                run_id=run_id,
                claim_id=claim.claim_id,
                projection=projection,
                stage=Phase16V5DispatchStage.ANALYST,
                task=self._analyst_task(projection),
            )
            model_calls += int(analyst.network_sent)
            if not analyst.passed or analyst.analysis is None:
                status = Phase16V5ExecutionStatus.FAILED if analyst.network_sent else Phase16V5ExecutionStatus.BLOCKED
                self._ledger.close_case(
                    claim_id=claim.claim_id,
                    status=(Phase16V5CaseOutcomeStatus.FAILED if analyst.network_sent else Phase16V5CaseOutcomeStatus.BLOCKED),
                    reason_code=analyst.reason_code,
                )
                executions.append(Phase16V5CaseExecution(case_id, status, analyst.reason_code, analyst.attempt_id, None))
                return self._close_and_report(run_id, run_kind, status, analyst.reason_code, executions, model_calls)
            planner = await self._execute_stage(
                run_id=run_id,
                claim_id=claim.claim_id,
                projection=projection,
                stage=Phase16V5DispatchStage.PLANNER,
                task=self._planner_task(projection, analyst.analysis),
                analysis=analyst.analysis,
            )
            model_calls += int(planner.network_sent)
            if not planner.passed:
                status = Phase16V5ExecutionStatus.FAILED if planner.network_sent else Phase16V5ExecutionStatus.BLOCKED
                self._ledger.close_case(
                    claim_id=claim.claim_id,
                    status=(Phase16V5CaseOutcomeStatus.FAILED if planner.network_sent else Phase16V5CaseOutcomeStatus.BLOCKED),
                    reason_code=planner.reason_code,
                )
                executions.append(Phase16V5CaseExecution(case_id, status, planner.reason_code, analyst.attempt_id, planner.attempt_id))
                return self._close_and_report(run_id, run_kind, status, planner.reason_code, executions, model_calls)
            self._ledger.close_case(
                claim_id=claim.claim_id,
                status=Phase16V5CaseOutcomeStatus.PASS,
                reason_code="MULTI_AGENT_READY",
            )
            executions.append(Phase16V5CaseExecution(case_id, Phase16V5ExecutionStatus.PASS, "MULTI_AGENT_READY", analyst.attempt_id, planner.attempt_id))
        return self._close_and_report(run_id, run_kind, Phase16V5ExecutionStatus.PASS, "CONTROLLED_E2E_QUALIFIED", executions, model_calls)

    def _static_reasons(self, *, run_kind: Phase16V5RunKind) -> tuple[str, ...]:
        """检查调用方无法覆盖的 Manifest/Profile/slot 身份，避免把配置错误带到网络层。"""

        expected_ids = self._dataset.manifest.smoke_eligible_case_ids
        if tuple(self._manifest.formal_case_ids) != tuple(expected_ids):
            return ("FORMAL_SLOT_IDENTITY_MISMATCH",)
        if run_kind not in {Phase16V5RunKind.CALIBRATION, Phase16V5RunKind.FORMAL}:
            return ("RUN_KIND_INVALID",)
        return ()

    def _projections(self, *, run_kind: Phase16V5RunKind) -> tuple[tuple[str, str, Phase16OfficialSmokeV2CaseProjection], ...]:
        """所有 V2 父数据投影都在领取 slot 前重建，防止标签或真实生产状态泄露给模型。"""

        instant = self._clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("V5 clock must be timezone-aware")
        if run_kind is Phase16V5RunKind.CALIBRATION:
            return ((
                self._manifest.calibration_case_id,
                self._manifest.calibration_case_digest,
                build_phase16_v5_calibration_projection(
                    repository_root=Path(__file__).resolve().parents[2],
                    now=instant,
                ),
            ),)
        return tuple(
            (
                case_id,
                self._manifest.formal_case_digests[case_id],
                build_phase16_official_smoke_v2_case_projection(dataset=self._dataset, case_id=case_id, now=instant),
            )
            for case_id in self._manifest.formal_case_ids
        )

    def _analyst_task(self, projection: Phase16OfficialSmokeV2CaseProjection) -> AgentTask:
        """仅替换 V2 投影的 Profile/任务身份，保留其已验证的受控输入与完整 evidence 引用。"""

        profile = build_phase16_v5_analyst_profile()
        task = projection.analyst_task
        return AgentTask(
            task_id=f"v5-analyst-{sha256(task.task_id.encode('utf-8')).hexdigest()[:24]}",
            task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            room_id=task.room_id,
            trace_id=task.trace_id,
            objective=task.objective,
            input_snapshot=_plain_json(task.input_snapshot),
            initial_evidence_refs=projection.evidence_refs,
        )

    def _planner_task(self, projection: Phase16OfficialSmokeV2CaseProjection, analysis: Any) -> AgentTask:
        """只把已验证 Analyst 载荷传给 V5 Planner；完整 Bundle 仍由受限 Resolver 提供。"""

        profile = build_phase16_v5_planner_profile()
        task = projection.analyst_task
        key = sha256(projection.case_id.encode("utf-8")).hexdigest()[:24]
        return AgentTask(
            task_id=f"v5-planner-{key}",
            task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING,
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            room_id=task.room_id,
            trace_id=task.trace_id,
            objective="Generate bounded options for controlled human-review E2E qualification.",
            input_snapshot={
                "analysis": analysis.as_model_input(),
                "evidence_bundle_digest": projection.evidence_bundle_digest,
            },
            initial_evidence_refs=projection.evidence_refs,
        )

    async def _execute_stage(
        self,
        *,
        run_id: str,
        claim_id: str,
        projection: Phase16OfficialSmokeV2CaseProjection,
        stage: Phase16V5DispatchStage,
        task: AgentTask,
        analysis: Any | None = None,
    ) -> _StageExecution:
        """用共享 Runner 完成一段，再按 V5 的 receipt、语义和零重试规则持久化结论。"""

        profile = build_phase16_v5_analyst_profile() if stage is Phase16V5DispatchStage.ANALYST else build_phase16_v5_planner_profile()
        capture = _CapturingModelPort(self._model_port)
        budget = _V5BudgetAdapter(
            ledger=self._ledger, run_id=run_id, claim_id=claim_id, stage=stage, profile=profile
        )
        bounded = BoundedSpecialistRunner(
            orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
            model_port=capture,
            budget_store=budget,
            evidence_registry=projection.evidence_registry,
            skill_port=_NoSkillPort(),
            skill_catalog=(),
            trusted_anchor_resolver=lambda _task: projection.trusted_anchor_id,
            pricing_policy=self._pricing_policy,
            budget_candidate_resolver=lambda _task: stage.value,
            request_id_factory=lambda _task, _execution_id, _index: str(
                uuid5(NAMESPACE_URL, f"{self._manifest.manifest_digest}:{run_id}:{projection.case_id}:{stage.value}")
            ),
            clock=self._clock,
        )
        try:
            result = await bounded.run(task)
        except Exception:
            result = None
        attempt = budget.attempt
        if attempt is None:
            return _StageExecution(False, False, "RUNNER_PRE_SEND_BLOCKED", None)
        if capture.request is None or (isinstance(capture.outcome, ModelFailure) and not capture.outcome.request_sent):
            self._append_validation(attempt.attempt_id, stage, Phase16V5ValidationVerdict.BLOCKED, "MODEL_REQUEST_NOT_SENT", None)
            return _StageExecution(False, False, "MODEL_REQUEST_NOT_SENT", attempt.attempt_id)
        if not isinstance(capture.outcome, ModelSuccess):
            self._append_validation(attempt.attempt_id, stage, Phase16V5ValidationVerdict.FAILED, "MODEL_OUTCOME_UNAVAILABLE", None)
            return _StageExecution(False, True, "MODEL_OUTCOME_UNAVAILABLE", attempt.attempt_id)
        receipt_complete = self._ledger.append_receipt(
            attempt_id=attempt.attempt_id,
            success=capture.outcome,
        )
        if not receipt_complete:
            self._append_validation(attempt.attempt_id, stage, Phase16V5ValidationVerdict.FAILED, "PROVIDER_RECEIPT_INVALID", None)
            return _StageExecution(False, True, "PROVIDER_RECEIPT_INVALID", attempt.attempt_id)
        try:
            if not isinstance(result, AgentResult):
                raise ValueError("shared runner did not return AgentResult")
            if stage is Phase16V5DispatchStage.ANALYST:
                validated = validate_v2_conflict_analysis_result(
                    task=task,
                    result=result,
                    expected_profile=profile,
                    expected_evidence_refs=projection.evidence_refs,
                    expected_finding_codes=projection.trigger_codes,
                )
                self._append_validation(attempt.attempt_id, stage, Phase16V5ValidationVerdict.PASS, "ANALYST_VALIDATION_PASS", result)
                return _StageExecution(True, True, "ANALYST_VALIDATION_PASS", attempt.attempt_id, validated)
            if analysis is None:
                raise ValueError("planner requires validated analyst analysis")
            validate_v2_live_decision_planner_result(
                task=task,
                result=result,
                expected_profile=profile,
                expected_evidence_refs=projection.evidence_refs,
                required_risk_codes=frozenset(item.value for item in analysis.risk_codes),
                available_backup_product_ids=projection.available_backup_product_ids,
                proposal_eligible_and_fresh=projection.proposal_eligible,
            )
            self._append_validation(attempt.attempt_id, stage, Phase16V5ValidationVerdict.PASS, "PLANNER_VALIDATION_PASS", result)
            return _StageExecution(True, True, "PLANNER_VALIDATION_PASS", attempt.attempt_id)
        except Exception:
            reason = "ANALYST_VALIDATION_FAILED" if stage is Phase16V5DispatchStage.ANALYST else "PLANNER_VALIDATION_FAILED"
            self._append_validation(attempt.attempt_id, stage, Phase16V5ValidationVerdict.FAILED, reason, None)
            return _StageExecution(False, True, reason, attempt.attempt_id)

    def _append_validation(self, attempt_id: str, stage: Phase16V5DispatchStage, verdict: Phase16V5ValidationVerdict, reason_code: str, result: AgentResult | None) -> None:
        """只写 Task/输出摘要和枚举结论，任何 Prompt、模型正文或业务建议都不会进入账本。"""

        digest_payload: dict[str, Any] = {"attempt_id": attempt_id, "stage": stage.value, "verdict": verdict.value, "reason_code": reason_code}
        if result is not None:
            digest_payload.update({
                "task_id": result.task_id,
                "profile_id": result.profile_id,
                "output_digest": canonical_json_sha256(_plain_json(result.output)),
                "evidence_ids": sorted(item.evidence_id for item in result.evidence_refs),
            })
        self._ledger.append_validation(
            attempt_id=attempt_id,
            verdict=verdict,
            reason_code=reason_code,
            validation_digest=canonical_json_sha256(digest_payload),
        )

    def _close_and_report(self, run_id: str, run_kind: Phase16V5RunKind, status: Phase16V5ExecutionStatus, reason_code: str, executions: list[Phase16V5CaseExecution], model_calls: int) -> Phase16V5ExecutionReport:
        """先追加 run 终态，再返回内存报告；账本拒绝任意第二次执行或终态改写。"""

        ledger_status = {
            Phase16V5ExecutionStatus.PASS: Phase16V5RunStatus.PASS,
            Phase16V5ExecutionStatus.FAILED: Phase16V5RunStatus.FAILED,
            Phase16V5ExecutionStatus.BLOCKED: Phase16V5RunStatus.BLOCKED,
        }[status]
        self._ledger.close_run(run_id=run_id, status=ledger_status, reason_code=reason_code)
        return self._report(run_id, run_kind, status, (() if status is Phase16V5ExecutionStatus.PASS else (reason_code,)), tuple(executions), model_calls)

    @staticmethod
    def _run_id(run_kind: Phase16V5RunKind) -> str:
        """将 run 类型映射到唯一冻结 ID，命令行没有自由 run ID 或重试参数。"""

        return PHASE16_V5_CALIBRATION_RUN_ID if run_kind is Phase16V5RunKind.CALIBRATION else PHASE16_V5_FORMAL_RUN_ID

    @staticmethod
    def _report(run_id: str, run_kind: Phase16V5RunKind, status: Phase16V5ExecutionStatus, reasons: tuple[str, ...], executions: tuple[Phase16V5CaseExecution, ...] | list[Phase16V5CaseExecution], model_calls: int) -> Phase16V5ExecutionReport:
        """将状态映射为严格证据措辞，单次校准 PASS 永远不能提升为 E2E 合格。"""

        conclusion = (
            Phase16V5EvidenceConclusion.CONTROLLED_E2E_QUALIFIED
            if status is Phase16V5ExecutionStatus.PASS and run_kind is Phase16V5RunKind.FORMAL
            else Phase16V5EvidenceConclusion.FAILED
            if status is Phase16V5ExecutionStatus.FAILED
            else Phase16V5EvidenceConclusion.INCONCLUSIVE
        )
        return Phase16V5ExecutionReport(
            campaign_id=PHASE16_V5_CAMPAIGN_ID,
            run_id=run_id,
            run_kind=run_kind,
            status=status,
            evidence_conclusion=conclusion,
            reason_codes=tuple(reasons),
            case_executions=tuple(executions),
            model_calls=model_calls,
        )
