"""Phase 16 受控双 Agent 的离线配对评估资产与 ScriptedModel 重放。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema.validators import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.decision_support.evidence import (
    AnchorRhythmPayload,
    DanmakuAggregatePayload,
    DanmakuNoiseLevel,
    DanmakuTopicEvidence,
    EvidenceAssemblyRequest,
    EvidenceBundleAssembler,
    EvidenceBundleSnapshot,
    EvidenceFreshnessPolicy,
    EvidenceRole,
    EvidenceScope,
    GovernedEvidenceComponent,
    GovernedEvidenceContextResolver,
    GovernedReadOnlyEvidenceResolver,
    LiveEvidenceResolverRegistry,
    PlanEvidencePayload,
    ProductInventoryPayload,
    ProductSnapshotEvidence,
    RhythmSignalKind,
    RoleEvidenceReference,
    VerifiedEventPayload,
    governed_evidence_digest,
)
from src.decision_support.models import (
    Incident,
    LiveSessionWorkspace,
    MultiAgentOutcomeStatus,
    WorkspaceView,
)
from src.decision_support.multi_agent import (
    HighConflictEscalationCoordinator,
    build_decision_planner_profile,
    build_evidence_analyst_profile,
)
from src.decision_support.store import InMemoryDecisionSupportStore
from src.plan_engine.event_state_machine import EventApplicationState, EventInboxState
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.models import PlanRunKind, PlanRunState
from src.skill_runtime.models import SideEffectState
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelMessage,
    ModelRequest,
    ModelSuccess,
    ModelUsage,
)
from src.specialist_runtime.models import (
    AgentAction,
    AgentActionKind,
    AgentFailure,
    AgentResult,
    AgentResultStatus,
    AgentTask,
    EvidenceKind,
    EvidenceRef,
    canonical_json_sha256,
)
from src.specialist_runtime.profiles import SpecialistProfile
from src.specialist_runtime.scripted_model import ScriptedAgentModel
from src.specialist_runtime.live_ops import PriorityLiveOpsPolicy


PHASE16_DATASET_ID = "phase16-controlled-multi-agent-v1"
PHASE16_SEED = 20260718
SPLIT_COUNTS: dict[str, int] = {"development": 12, "validation": 24, "holdout": 12}
CASE_KIND_COUNTS: dict[str, int] = {
    "NORMAL_SINGLE_COPILOT": 12,
    "HIGH_CONFLICT_PAIRED": 24,
    "ADVERSARIAL_DEGRADED": 12,
}
# Manifest 的代码身份必须覆盖每个会改变 Coordinator 评估结果、Store 持久化/恢复或
# Proposal lineage 的生产模块。它是相对仓库根的公开常量，测试可以防止未来补模块时
# 静默遗漏，从而让旧数据集错误复用到新运行时。
PHASE16_SOURCE_CLOSURE_PATHS: tuple[str, ...] = (
    "src/decision_support/multi_agent.py",
    "src/decision_support/multi_agent_evaluation.py",
    "src/decision_support/evidence.py",
    "src/decision_support/models.py",
    "src/decision_support/proposal.py",
    "src/decision_support/store.py",
    "src/specialist_runtime/model_port.py",
    "src/specialist_runtime/models.py",
    "src/specialist_runtime/profiles.py",
    "src/specialist_runtime/live_ops.py",
    "src/specialist_runtime/scripted_model.py",
)
_EVALUATION_NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
_HASH_PATTERN = r"^[0-9a-f]{64}$"


class Phase16CaseKind(StrEnum):
    """冻结数据集中的三类业务路径，不能由运行时动态补充。"""

    NORMAL_SINGLE_COPILOT = "NORMAL_SINGLE_COPILOT"
    HIGH_CONFLICT_PAIRED = "HIGH_CONFLICT_PAIRED"
    ADVERSARIAL_DEGRADED = "ADVERSARIAL_DEGRADED"


class Phase16ExpectedRoute(StrEnum):
    """标签文件中的受控预期；该字段绝不进入 AgentTask 输入。"""

    SINGLE_COPILOT = "SINGLE_COPILOT"
    MULTI_AGENT_READY = "MULTI_AGENT_READY"
    NO_SEND = "NO_SEND"
    DEGRADED = "DEGRADED"


class Phase16EvaluationCase(BaseModel):
    """模型不可见标签之外的冻结 case，只有事实场景和稳定身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(..., pattern=r"^phase16-[a-z0-9-]+-(development|validation|holdout)-[0-9]{3}$")
    split: str = Field(..., pattern=r"^(development|validation|holdout)$")
    kind: Phase16CaseKind
    logical_case_id: str = Field(..., min_length=1)
    input: dict[str, Any]

    @field_validator("input", mode="after")
    @classmethod
    def _reject_labels_from_model_visible_input(cls, value: dict[str, Any]) -> dict[str, Any]:
        """把评分预期与 smoke 标记限制在旁路标签文件，不能泄漏给模型任务。"""

        forbidden = {"label", "expected", "smoke_eligible", "expected_route", "score"}
        if forbidden.intersection(value):
            raise ValueError("evaluation labels cannot be part of case input")
        return value

    @model_validator(mode="after")
    def _bind_case_id_to_split(self) -> "Phase16EvaluationCase":
        """case ID 的分片后缀必须和结构字段一致，防止错误解封 holdout。"""

        if f"-{self.split}-" not in self.case_id:
            raise ValueError("case_id split does not match case split")
        return self


class Phase16EvaluationLabel(BaseModel):
    """与模型输入分离的确定性期望、配对和 smoke 资格。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(..., min_length=1)
    split: str = Field(..., pattern=r"^(development|validation|holdout)$")
    expected_route: Phase16ExpectedRoute
    paired_baseline_required: bool
    smoke_eligible: bool


class Phase16Script(BaseModel):
    """ScriptedModel 的最小执行脚本，独立于 case 与评分标签。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(..., min_length=1)
    analyst_mode: str = Field(..., pattern=r"^(NONE|VALID|FORGED|MODEL_FAILURE|TIMEOUT)$")
    planner_mode: str = Field(..., pattern=r"^(NONE|VALID|INVALID)$")


class Phase16Manifest(BaseModel):
    """绑定数据、脚本、实现闭包与每个不可变产物摘要的 Manifest。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str = PHASE16_DATASET_ID
    dataset_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    seed: int = Field(..., ge=0, strict=True)
    split_counts: dict[str, int]
    case_kind_counts: dict[str, int]
    case_ids: dict[str, tuple[str, ...]]
    smoke_eligible_case_ids: tuple[str, ...]
    case_digests: dict[str, str]
    dataset_digest: str = Field(..., pattern=_HASH_PATTERN)
    artifact_digests: dict[str, str]
    profile_digests: dict[str, str]
    generator_digest: str = Field(..., pattern=_HASH_PATTERN)
    source_code_digest: str = Field(..., pattern=_HASH_PATTERN)
    manifest_digest: str = Field(default="", pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def _validate_frozen_shape(self) -> "Phase16Manifest":
        """加载时重新确认固定 48 例形状、10 个 smoke 和 Manifest 自摘要。"""

        if self.split_counts != SPLIT_COUNTS:
            raise ValueError("Phase 16 split counts must be 12/24/12")
        if self.case_kind_counts != CASE_KIND_COUNTS:
            raise ValueError("Phase 16 case kind counts are frozen")
        if set(self.case_ids) != set(SPLIT_COUNTS):
            raise ValueError("manifest must contain all frozen splits")
        if any(len(self.case_ids[name]) != count for name, count in SPLIT_COUNTS.items()):
            raise ValueError("manifest case IDs do not match split counts")
        all_ids = tuple(case_id for split in SPLIT_COUNTS for case_id in self.case_ids[split])
        if len(all_ids) != 48 or len(set(all_ids)) != 48:
            raise ValueError("manifest must contain exactly 48 unique cases")
        if len(self.smoke_eligible_case_ids) != 10:
            raise ValueError("manifest must contain exactly ten smoke eligible cases")
        if not set(self.smoke_eligible_case_ids).issubset(set(all_ids)):
            raise ValueError("smoke eligible cases must belong to the dataset")
        expected_profiles = {
            "evidence_analyst": build_evidence_analyst_profile().profile_digest,
            "decision_planner": build_decision_planner_profile().profile_digest,
        }
        if self.profile_digests != expected_profiles:
            raise ValueError("manifest profile digests do not match frozen Phase 16 profiles")
        if self.manifest_digest:
            expected = _sha256(_canonical_bytes(self.model_dump(mode="json", exclude={"manifest_digest"})))
            if self.manifest_digest != expected:
                raise ValueError("manifest digest does not match manifest facts")
        return self


@dataclass(frozen=True)
class Phase16EvaluationDataset:
    """运行时只读聚合；所有身份都已由加载器验证。"""

    cases: tuple[Phase16EvaluationCase, ...]
    labels: Mapping[str, Phase16EvaluationLabel]
    scripts: Mapping[str, Phase16Script]
    manifest: Phase16Manifest


@dataclass(frozen=True)
class Phase16EvaluationCaseResult:
    """每个 case 的可复核协调器证据，不包含模型自由推理文本。"""

    case_id: str
    expected_route: Phase16ExpectedRoute
    actual_route: Phase16ExpectedRoute
    call_sequence: tuple[str, ...]
    analyst_calls: int
    planner_calls: int
    ready_outcomes: int
    degraded_outcomes: int
    no_send: bool
    paired_identity_correct: bool
    paired_baseline_executed: bool
    lineage_identity_correct: bool
    model_input_bound: bool
    model_metadata_safe: bool
    profile_contract_verified: bool
    failure_semantics_correct: bool
    replay_identity_correct: bool


@dataclass(frozen=True)
class Phase16DeterministicBaselineResult:
    """与双 Agent 路径共享同一 Bundle 的确定性单 Copilot 基线观察。"""

    logical_case_id: str
    evidence_bundle_id: str
    evidence_bundle_digest: str
    route: Phase16ExpectedRoute
    action: str
    model_calls: int


@dataclass(frozen=True)
class Phase16EvaluationReport:
    """Phase 16 Scripted 重放的汇总证据，刻意区分模型调用数和真实费用。"""

    dataset_id: str
    total_cases: int
    normal_single_copilot_cases: int
    high_conflict_paired_cases: int
    adversarial_degraded_cases: int
    route_correct_cases: int
    paired_identity_correct_cases: int
    paired_baseline_executed_cases: int
    analyst_calls: int
    planner_calls: int
    ready_outcomes: int
    degraded_outcomes: int
    no_send_cases: int
    lineage_identity_correct_cases: int
    model_input_bound_cases: int
    model_metadata_safe_cases: int
    profile_contract_verified_cases: int
    replay_identity_correct_cases: int
    scripted_reserved_cost_cny: Decimal
    real_model_calls: int
    case_results: tuple[Phase16EvaluationCaseResult, ...]


def _canonical_bytes(value: Any) -> bytes:
    """所有评估文件使用排序 JSON 与 LF，避免操作系统换行影响摘要。"""

    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256(value: bytes) -> str:
    """返回可写入 Manifest 的稳定 SHA-256 十六进制摘要。"""

    return hashlib.sha256(value).hexdigest()


def _file_digest(path: Path) -> str:
    """以二进制读取产物，不能让文本层自动换行改变验证结果。"""

    return _sha256(path.read_bytes())


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """一次性写入规范 JSONL；生成器不使用平台默认编码或行尾。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(_canonical_bytes(record) for record in records))


def _split_for_index(index: int, total: int) -> str:
    """按 1:2:1 比例为每个固定类别分配 development/validation/holdout。"""

    development = total // 4
    holdout = total // 4
    return "development" if index <= development else "validation" if index <= total - holdout else "holdout"


def _source_closure_digest(source_root: Path) -> str:
    """绑定实际 Coordinator、证据装配和 ScriptedModel 源码，不能只冻结数据文件。"""

    payload = bytearray()
    for relative_path in PHASE16_SOURCE_CLOSURE_PATHS:
        path = source_root / relative_path
        relative = path.relative_to(source_root).as_posix().encode("utf-8")
        normalized = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        payload.extend(relative + b"\0" + normalized + b"\0")
    return _sha256(bytes(payload))


def _runtime_key(case: Phase16EvaluationCase) -> str:
    """从公开 case ID 单向派生不可逆的运行时身份，禁止 split/kind 进入模型边界。"""

    return _sha256(case.case_id.encode("utf-8"))[:24]


def _validate_dataset_for_run(dataset: Phase16EvaluationDataset) -> None:
    """每次执行前重算冻结资产与源码身份，抵御同进程嵌套 dict 篡改。"""

    repository_root = Path(__file__).resolve().parents[2]
    generator_path = (
        repository_root
        / "evaluation"
        / "generators"
        / "generate_phase16_controlled_multi_agent.py"
    )
    if _file_digest(generator_path) != dataset.manifest.generator_digest:
        raise ValueError("generator digest does not match frozen manifest")
    if _source_closure_digest(repository_root) != dataset.manifest.source_code_digest:
        raise ValueError("source code digest does not match frozen manifest")
    cases = dataset.cases
    current_digests = {
        case.case_id: _sha256(_canonical_bytes(case.model_dump(mode="json")))
        for case in cases
    }
    if current_digests != dataset.manifest.case_digests:
        raise ValueError("case digests do not match frozen manifest")
    if _sha256(
        b"".join(_canonical_bytes(case.model_dump(mode="json")) for case in cases)
    ) != dataset.manifest.dataset_digest:
        raise ValueError("dataset digest does not match frozen manifest")
    if set(dataset.labels) != set(current_digests) or set(dataset.scripts) != set(current_digests):
        raise ValueError("labels and scripts do not match frozen case identities")


def _case_record(
    *,
    kind: Phase16CaseKind,
    index: int,
    total: int,
    scenario: str,
    include_availability_noise: bool,
    pause_required: bool,
    valid_backup_count: int,
    reconciliation_required: bool = False,
    side_effect_unknown: bool = False,
    stale: bool = False,
) -> dict[str, Any]:
    """生成不含评分真值的 case；所有策略期望另存 labels.jsonl。"""

    split = _split_for_index(index, total)
    prefix = kind.value.lower().replace("_", "-")
    case_id = f"phase16-{prefix}-{split}-{index:03d}"
    return {
        "case_id": case_id,
        "split": split,
        "kind": kind.value,
        # 高冲突 case 的 logical identity 由同一记录同时绑定 baseline 与 controlled 路径；
        # 不复制成两个能被分别删改的 case，配对比较只能从这个稳定 ID 恢复。
        "logical_case_id": f"phase16-logical-{prefix}-{index:03d}",
        "input": {
            "scenario": scenario,
            "include_availability_noise": include_availability_noise,
            "pause_required": pause_required,
            "valid_backup_count": valid_backup_count,
            # 这些业务值进入受治理 Inventory/Rhythm payload，而不是只放在 case 元数据；
            # 每个 split 因而拥有不同的真实证据组合，不能用同一个行为样本重复计分。
            "backup_inventory": 10 + index,
            "pace_score": 60 + index,
            "reconciliation_required": reconciliation_required,
            "side_effect_unknown": side_effect_unknown,
            "stale": stale,
        },
    }


def _generate_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """按冻结数量生成 case、旁路标签和 ScriptedModel 脚本。"""

    cases: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    scripts: list[dict[str, Any]] = []
    for index in range(1, 13):
        case = _case_record(
            kind=Phase16CaseKind.NORMAL_SINGLE_COPILOT,
            index=index,
            total=12,
            scenario="normal_single_copilot",
            include_availability_noise=False,
            pause_required=False,
            valid_backup_count=1,
        )
        cases.append(case)
        labels.append({"case_id": case["case_id"], "split": case["split"], "expected_route": "SINGLE_COPILOT", "paired_baseline_required": False, "smoke_eligible": False})
        scripts.append({"case_id": case["case_id"], "analyst_mode": "NONE", "planner_mode": "NONE"})
    for index in range(1, 25):
        case = _case_record(
            kind=Phase16CaseKind.HIGH_CONFLICT_PAIRED,
            index=index,
            total=24,
            scenario="high_conflict_sold_out",
            include_availability_noise=True,
            pause_required=True,
            valid_backup_count=2,
        )
        cases.append(case)
        labels.append({"case_id": case["case_id"], "split": case["split"], "expected_route": "MULTI_AGENT_READY", "paired_baseline_required": True, "smoke_eligible": index <= 10})
        scripts.append({"case_id": case["case_id"], "analyst_mode": "VALID", "planner_mode": "VALID"})
    adversarial = (
        ("stale_evidence", "NONE", "NONE", {"stale": True}),
        ("stale_evidence", "NONE", "NONE", {"stale": True}),
        ("reconciliation_required", "NONE", "NONE", {"reconciliation_required": True}),
        ("reconciliation_required", "NONE", "NONE", {"reconciliation_required": True}),
        ("unknown_side_effect", "NONE", "NONE", {"side_effect_unknown": True}),
        ("unknown_side_effect", "NONE", "NONE", {"side_effect_unknown": True}),
        ("forged_analyst_output", "FORGED", "NONE", {}),
        ("forged_analyst_output", "FORGED", "NONE", {}),
        ("analyst_model_failure", "MODEL_FAILURE", "NONE", {}),
        ("analyst_timeout", "TIMEOUT", "NONE", {}),
        ("invalid_planner_proposal", "VALID", "INVALID", {}),
        ("invalid_planner_proposal", "VALID", "INVALID", {}),
    )
    for index, (scenario, analyst_mode, planner_mode, flags) in enumerate(adversarial, start=1):
        case = _case_record(
            kind=Phase16CaseKind.ADVERSARIAL_DEGRADED,
            index=index,
            total=12,
            scenario=scenario,
            include_availability_noise=True,
            pause_required=True,
            valid_backup_count=2,
            **flags,
        )
        cases.append(case)
        expected_route = "NO_SEND" if analyst_mode == "NONE" else "DEGRADED"
        labels.append({"case_id": case["case_id"], "split": case["split"], "expected_route": expected_route, "paired_baseline_required": False, "smoke_eligible": False})
        scripts.append({"case_id": case["case_id"], "analyst_mode": analyst_mode, "planner_mode": planner_mode})
    return cases, labels, scripts


def generate_phase16_controlled_multi_agent_dataset(
    output_root: Path,
    *,
    source_root: Path | None = None,
) -> Phase16Manifest:
    """写出完全独立的 Phase 16 case、label、script 与 Manifest 资产。"""

    root = Path(output_root)
    repository_root = Path(source_root) if source_root else Path(__file__).resolve().parents[2]
    case_records, label_records, script_records = _generate_records()
    cases = tuple(Phase16EvaluationCase.model_validate(record) for record in case_records)
    labels = tuple(Phase16EvaluationLabel.model_validate(record) for record in label_records)
    scripts = tuple(Phase16Script.model_validate(record) for record in script_records)
    case_ids = {split: tuple(case.case_id for case in cases if case.split == split) for split in SPLIT_COUNTS}
    case_digests = {case.case_id: _sha256(_canonical_bytes(case.model_dump(mode="json"))) for case in cases}
    paths = {"cases.jsonl": root / "cases.jsonl", "labels.jsonl": root / "labels.jsonl", "scripts.jsonl": root / "scripts.jsonl"}
    _write_jsonl(paths["cases.jsonl"], [case.model_dump(mode="json") for case in cases])
    _write_jsonl(paths["labels.jsonl"], [label.model_dump(mode="json") for label in labels])
    _write_jsonl(paths["scripts.jsonl"], [script.model_dump(mode="json") for script in scripts])
    artifact_digests = {name: _file_digest(path) for name, path in paths.items()}
    generator_path = repository_root / "evaluation" / "generators" / "generate_phase16_controlled_multi_agent.py"
    manifest = Phase16Manifest(
        seed=PHASE16_SEED,
        split_counts=dict(SPLIT_COUNTS),
        case_kind_counts=dict(CASE_KIND_COUNTS),
        case_ids=case_ids,
        smoke_eligible_case_ids=tuple(label.case_id for label in labels if label.smoke_eligible),
        case_digests=dict(sorted(case_digests.items())),
        dataset_digest=_sha256(b"".join(_canonical_bytes(case.model_dump(mode="json")) for case in cases)),
        artifact_digests=dict(sorted(artifact_digests.items())),
        profile_digests={
            "evidence_analyst": build_evidence_analyst_profile().profile_digest,
            "decision_planner": build_decision_planner_profile().profile_digest,
        },
        generator_digest=_file_digest(generator_path),
        source_code_digest=_source_closure_digest(repository_root),
    )
    manifest = manifest.model_copy(update={"manifest_digest": _sha256(_canonical_bytes(manifest.model_dump(mode="json", exclude={"manifest_digest"})))})
    # Pydantic 的通用 model_copy 对此数据模型安全；它只为写入已经重算过的派生摘要，
    # 不会绕过任何 case、label、script 或 Manifest 形状校验。
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(_canonical_bytes(manifest.model_dump(mode="json")))
    return manifest


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """严格读取 UTF-8/LF JSONL；空行意味着资产并非生成器输出。"""

    raw = path.read_bytes()
    if b"\r" in raw or raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("evaluation asset must be UTF-8 without BOM and LF only")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("evaluation asset is not valid UTF-8") from exc
    if not text or not text.endswith("\n") or "\n\n" in text:
        raise ValueError("evaluation JSONL must contain one LF-terminated record per line")
    return [json.loads(line) for line in text.splitlines()]


def load_phase16_controlled_multi_agent_dataset(output_root: Path) -> Phase16EvaluationDataset:
    """按 Manifest 重新校验所有文件、case 身份、split、标签与脚本闭包。"""

    root = Path(output_root)
    manifest_path = root / "manifest.json"
    manifest = Phase16Manifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    paths = {"cases.jsonl": root / "cases.jsonl", "labels.jsonl": root / "labels.jsonl", "scripts.jsonl": root / "scripts.jsonl"}
    for name, path in paths.items():
        if _file_digest(path) != manifest.artifact_digests.get(name):
            raise ValueError(f"evaluation artifact digest mismatch: {name}")
    cases = tuple(Phase16EvaluationCase.model_validate(record) for record in _load_jsonl(paths["cases.jsonl"]))
    labels = {label.case_id: label for label in (Phase16EvaluationLabel.model_validate(record) for record in _load_jsonl(paths["labels.jsonl"]))}
    scripts = {script.case_id: script for script in (Phase16Script.model_validate(record) for record in _load_jsonl(paths["scripts.jsonl"]))}
    if len(cases) != 48 or len(labels) != 48 or len(scripts) != 48:
        raise ValueError("Phase 16 dataset must contain exactly 48 case, label and script records")
    case_ids = tuple(case.case_id for case in cases)
    if len(set(case_ids)) != 48 or set(case_ids) != set(labels) or set(case_ids) != set(scripts):
        raise ValueError("case, label and script identities must match exactly")
    expected_ids = {split: tuple(case.case_id for case in cases if case.split == split) for split in SPLIT_COUNTS}
    if expected_ids != manifest.case_ids:
        raise ValueError("dataset split IDs do not match manifest")
    expected_digests = {case.case_id: _sha256(_canonical_bytes(case.model_dump(mode="json"))) for case in cases}
    if expected_digests != manifest.case_digests:
        raise ValueError("dataset case digests do not match manifest")
    if _sha256(b"".join(_canonical_bytes(case.model_dump(mode="json")) for case in cases)) != manifest.dataset_digest:
        raise ValueError("dataset digest does not match manifest")
    kinds = {kind.value: sum(case.kind is kind for case in cases) for kind in Phase16CaseKind}
    if kinds != CASE_KIND_COUNTS:
        raise ValueError("dataset case kinds do not match manifest")
    dataset = Phase16EvaluationDataset(
        cases=cases,
        # 顶层映射也转为只读，阻止调用方增删/替换标签或脚本身份；嵌套 case
        # input 的极端同进程篡改由执行前的 digest 重算再次 fail-closed。
        labels=MappingProxyType(labels),
        scripts=MappingProxyType(scripts),
        manifest=manifest,
    )
    _validate_dataset_for_run(dataset)
    return dataset


def _product(product_id: str, price: str, version: int, inventory: int, is_active: bool) -> ProductSnapshotEvidence:
    """构造受 Pydantic 校验的产品快照，而不是向 Bundle 注入自由 JSON。"""

    return ProductSnapshotEvidence(product_id=product_id, name=product_id, price=price, inventory=inventory, version=version, is_active=is_active)


def _component(
    *,
    role: EvidenceRole,
    scope: EvidenceScope,
    evidence_id: str,
    kind: EvidenceKind,
    source_version: str,
    observed_version: int,
    observed_at: datetime,
    received_at: datetime,
    payload: object,
) -> GovernedEvidenceComponent:
    """从角色、scope 和结构化 payload 重建不可伪造的 EvidenceRef 摘要。"""

    digest = governed_evidence_digest(role=role, scope=scope, evidence_id=evidence_id, source_version=source_version, observed_version=observed_version, observed_at=observed_at, received_at=received_at, payload=payload)
    return GovernedEvidenceComponent(
        role=role,
        reference=EvidenceRef(kind=kind, evidence_id=evidence_id, source_version=source_version, digest=digest, room_id=scope.room_id, anchor_id=scope.anchor_id),
        scope=scope,
        observed_version=observed_version,
        observed_at=observed_at,
        received_at=received_at,
        payload=payload,
    )


def _assemble_bundle(
    *,
    workspace: LiveSessionWorkspace,
    incident: Incident,
    case: Phase16EvaluationCase,
    now: datetime,
):
    """通过正式六角色 Assembler 构造每例证据，评估不导入任何测试 factory。"""

    facts = case.input
    key = _runtime_key(case)
    # 陈旧 case 必须先以真实新鲜证据成功入库，再把 Coordinator 时钟推进到 TTL 之后；
    # 直接装配陈旧组件只是在 Assembler 层失败，无法证明升级选择器会在模型发送前拒绝。
    evidence_time = now
    scope = EvidenceScope(
        live_session_id=workspace.live_session_id,
        incident_id=incident.incident_id,
        room_id=workspace.room_id,
        trace_id=workspace.trace_id,
        anchor_id=workspace.anchor_id,
        root_plan_run_id=workspace.root_plan_run_id,
    )
    event_id = f"event-{key}"
    event = InventoryFactEvent.create_sold_out(event_id=event_id, room_id=workspace.room_id, product_id="p001", observed_version=2, occurred_at=evidence_time - timedelta(seconds=8), source="taobao.inventory")
    provenance = VerifiedIngressProvenance(provenance_id=f"provenance-{key}", profile_id="taobao-inventory-v1", transport="KAFKA", topic="inventory-events", source=event.source, received_at=evidence_time - timedelta(seconds=7), payload_digest=event.payload_digest)
    components = (
        _component(role=EvidenceRole.VERIFIED_EVENT, scope=scope, evidence_id=event_id, kind=EvidenceKind.EVENT, source_version="2.0.0", observed_version=2, observed_at=event.occurred_at, received_at=provenance.received_at, payload=VerifiedEventPayload(event=event, provenance=provenance, inbox_state=EventInboxState.APPLIED, application_state=EventApplicationState.APPLIED, emergency_plan_run_id=f"emergency-{key}", applied_plan_version=2, side_effect_state=SideEffectState.CONFIRMED)),
        _component(role=EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT, scope=scope, evidence_id=f"inventory-{key}", kind=EvidenceKind.SKILL_ATTEMPT, source_version="2.0.0", observed_version=2, observed_at=evidence_time - timedelta(seconds=5), received_at=evidence_time - timedelta(seconds=4), payload=ProductInventoryPayload(captured_at=evidence_time - timedelta(seconds=5), sold_out_product_id="p001", expected_version=2, planned_product=_product("p001", "39.90", 1, 10, True), current_product=_product("p001", "39.90", 2, 0, False), backup_products=tuple(_product(f"p{index + 2:03d}", "35.90", 4, facts["backup_inventory"], True) for index in range(facts["valid_backup_count"])))),
        _component(role=EvidenceRole.ROOT_PLAN_SNAPSHOT, scope=scope, evidence_id=workspace.root_plan_run_id, kind=EvidenceKind.PLAN, source_version="2.0.0", observed_version=2, observed_at=evidence_time - timedelta(seconds=5), received_at=evidence_time - timedelta(seconds=4), payload=PlanEvidencePayload(captured_at=evidence_time - timedelta(seconds=5), plan_run_id=workspace.root_plan_run_id, root_plan_run_id=workspace.root_plan_run_id, plan_kind=PlanRunKind.CARD_BATCH, plan_state=PlanRunState.FROZEN, plan_version=2, reconciliation_required=facts["reconciliation_required"], side_effect_unknown=facts["side_effect_unknown"])),
        _component(role=EvidenceRole.EMERGENCY_PLAN_SNAPSHOT, scope=scope, evidence_id=f"emergency-{key}", kind=EvidenceKind.PLAN, source_version="1.0.0", observed_version=1, observed_at=evidence_time - timedelta(seconds=5), received_at=evidence_time - timedelta(seconds=4), payload=PlanEvidencePayload(captured_at=evidence_time - timedelta(seconds=5), plan_run_id=f"emergency-{key}", root_plan_run_id=workspace.root_plan_run_id, parent_plan_run_id=workspace.root_plan_run_id, trigger_event_id=event_id, plan_kind=PlanRunKind.EMERGENCY_SOLD_OUT, plan_state=PlanRunState.SUCCEEDED, plan_version=1, reconciliation_required=facts["reconciliation_required"], side_effect_unknown=facts["side_effect_unknown"])),
        _component(role=EvidenceRole.DANMAKU_AGGREGATE, scope=scope, evidence_id=f"danmaku-{key}", kind=EvidenceKind.AUDIT, source_version="3.0.0", observed_version=3, observed_at=evidence_time - timedelta(seconds=2), received_at=evidence_time - timedelta(seconds=1), payload=DanmakuAggregatePayload(aggregate_id=f"danmaku-{key}", window_start=evidence_time - timedelta(seconds=10), window_end=evidence_time - timedelta(seconds=2), noise_level=DanmakuNoiseLevel.HIGH if facts["include_availability_noise"] else DanmakuNoiseLevel.LOW, topics=(DanmakuTopicEvidence(category="PRODUCT_AVAILABILITY", summary="用户集中询问主商品是否还有库存", count=1),))),
        _component(role=EvidenceRole.RHYTHM_SIGNAL, scope=scope, evidence_id=f"rhythm-{key}", kind=EvidenceKind.AUDIT, source_version="5.0.0", observed_version=5, observed_at=evidence_time - timedelta(seconds=1), received_at=evidence_time, payload=AnchorRhythmPayload(signal_id=f"rhythm-{key}", window_start=evidence_time - timedelta(seconds=9), window_end=evidence_time - timedelta(seconds=1), signal_kind=RhythmSignalKind.PAUSE_REQUIRED if facts["pause_required"] else RhythmSignalKind.STEADY, pace_score=facts["pace_score"])),
    )
    registry = LiveEvidenceResolverRegistry({component.role: GovernedReadOnlyEvidenceResolver(resolver_id=f"phase16-eval-{component.role.value.lower()}", resolver_version="1.0.0", role=component.role, loader=lambda _evidence_id, item=component: item) for component in components})
    request = EvidenceAssemblyRequest(evidence_bundle_id=f"bundle-{key}", idempotency_key=f"bundle-{key}", live_session_id=workspace.live_session_id, incident_id=incident.incident_id, references=tuple(RoleEvidenceReference(role=item.role, reference=item.reference) for item in components))
    return EvidenceBundleAssembler(context_resolver=GovernedEvidenceContextResolver(workspace_loader=lambda _live_session_id: workspace, incident_loader=lambda _incident_id: incident), registry=registry, freshness_policy=EvidenceFreshnessPolicy.default(), clock=lambda: now).assemble(request)


def _profile_for(task: AgentTask) -> SpecialistProfile:
    """严格返回 Phase 16 的一个冻结 Profile；其它 task kind 不能借评估路径运行。"""

    from src.decision_support.multi_agent import build_decision_planner_profile, build_evidence_analyst_profile

    if task.task_kind.value == "CONFLICT_ANALYSIS":
        return build_evidence_analyst_profile()
    if task.task_kind.value == "LIVE_DECISION_PLANNING":
        return build_decision_planner_profile()
    raise ValueError("Phase 16 evaluation runner only accepts controlled multi-agent tasks")


class _EvaluationScriptedRunner:
    """仅供离线评估使用的显式 ScriptedModel 组合，不触碰共享生产 Runner 或正式账本。"""

    def __init__(self, *, case: Phase16EvaluationCase, script: Phase16Script, now: datetime) -> None:
        self._case = case
        self._script = script
        self._case_key = _runtime_key(case)
        self._now = now
        self.calls: list[AgentTask] = []
        self.reserved_cost_cny = Decimal("0")
        self._input_bound: list[bool] = []
        self._metadata_safe: list[bool] = []
        self._profile_contract: list[bool] = []

    @property
    def model_input_bound(self) -> bool:
        """所有实际发送都已校验为 Coordinator 构造的完整受治理输入。"""

        return all(self._input_bound)

    @property
    def model_metadata_safe(self) -> bool:
        """模型正文不含可推断评估标签的 case ID、split 或 kind。"""

        return all(self._metadata_safe)

    @property
    def profile_contract_verified(self) -> bool:
        """每次发送及返回均匹配冻结 Profile 的 prompt、schema 和 model identity。"""

        return all(self._profile_contract)

    def resolve_profile(self, task: AgentTask) -> SpecialistProfile:
        """Coordinator 在 dispatch 前以此复核完整 Profile identity，不放开 Registry。"""

        return _profile_for(task)

    async def run(self, task: AgentTask) -> AgentResult:
        """把一次 ScriptedModel 请求映射为严格 AgentResult，且绝不重试或 fallback。"""

        profile = _profile_for(task)
        input_snapshot = task.model_dump(mode="json")["input_snapshot"]
        input_bound = _is_governed_task_input(task, input_snapshot)
        self._input_bound.append(input_bound)
        if not input_bound:
            return AgentResult(
                task_id=task.task_id,
                profile_id=task.profile_id,
                profile_version=task.profile_version,
                status=AgentResultStatus.POLICY_DENIED,
                failure=AgentFailure(code="EVALUATION_TASK_INPUT_DENIED", details={}),
                summary="EVALUATION_TASK_INPUT_DENIED",
            )
        request_id = f"phase16-eval:{self._case_key}:{task.task_kind.value}"
        request_payload = {
            "task_id": task.task_id,
            "task_kind": task.task_kind.value,
            "input_snapshot": input_snapshot,
            "evidence_refs": [
                reference.model_dump(mode="json")
                for reference in task.initial_evidence_refs
            ],
        }
        request_content = json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        forbidden_metadata = (
            self._case.case_id,
            self._case.split,
            self._case.kind.value,
        )
        self._metadata_safe.append(
            all(item not in request_content for item in forbidden_metadata)
        )
        request = ModelRequest(
            request_id=request_id,
            endpoint_host=profile.endpoint_host,
            model_id=profile.model_id,
            temperature=profile.temperature,
            prompt_hash=profile.prompt_hash,
            result_schema_hash=profile.result_schema_hash,
            # ScriptedModel 仍必须消费 Coordinator 实际创建的完整任务正文：Analyst 读取
            # Bundle + trigger，Planner 读取 Bundle + 已验证 Analysis。不能只传 task ID 后
            # 由评估替身从外部 Bundle 预制答案，否则错误装配也可能误报 READY。
            messages=(
                ModelMessage(role="system", content=profile.prompt_text),
                ModelMessage(role="user", content=request_content),
            ),
            max_output_tokens=profile.max_total_tokens,
            deadline_at=self._now + timedelta(seconds=profile.deadline_seconds),
        )
        self._profile_contract.append(
            task.profile_id == profile.profile_id
            and task.profile_version == profile.profile_version
            and request.messages[0].content == profile.prompt_text
            and request.prompt_hash == profile.prompt_hash
            and request.result_schema_hash == profile.result_schema_hash
        )
        self.calls.append(task)
        # 外部发送在拿到响应前已经消耗合同预约；即使 ModelFailure 表明 request_sent，
        # 也必须保守计入本例的离线预算证据，不能把失败调用误记为零成本。
        self.reserved_cost_cny += profile.max_case_cost_cny
        scripted_model = ScriptedAgentModel(
            outcomes={
                request_id: (
                    _scripted_outcome_for_task(
                        request=request,
                        profile=profile,
                        task=task,
                        script=self._script,
                    ),
                )
            }
        )
        outcome = await scripted_model.complete(request)
        if isinstance(outcome, ModelFailure):
            return AgentResult(task_id=task.task_id, profile_id=task.profile_id, profile_version=task.profile_version, status=AgentResultStatus.MODEL_ERROR, failure=AgentFailure(code=f"SCRIPTED_{outcome.category.value}", details={}), summary="SCRIPTED_MODEL_FAILURE", model_calls=1)
        if not isinstance(outcome, ModelSuccess) or outcome.usage is None:
            return AgentResult(task_id=task.task_id, profile_id=task.profile_id, profile_version=task.profile_version, status=AgentResultStatus.MODEL_ERROR, failure=AgentFailure(code="SCRIPTED_OUTCOME_INVALID", details={}), summary="SCRIPTED_OUTCOME_INVALID", model_calls=1)
        if outcome.request_id != request.request_id or outcome.model_id != profile.model_id:
            self._profile_contract.append(False)
            return AgentResult(task_id=task.task_id, profile_id=task.profile_id, profile_version=task.profile_version, status=AgentResultStatus.MODEL_ERROR, failure=AgentFailure(code="SCRIPTED_MODEL_IDENTITY_MISMATCH", details={}), summary="SCRIPTED_MODEL_IDENTITY_MISMATCH", model_calls=1)
        # ModelSuccess 为防止调用方篡改会冻结 output；AgentResult 的严格 JSON 协议要求
        # 普通 dict/list，因此通过 Pydantic 的 JSON 序列化边界还原一次，不能直接把
        # FrozenDict 传入并误归类为模型错误。
        envelope = outcome.model_dump(mode="json")["output"]
        try:
            action = AgentAction.model_validate(envelope)
            if (
                action.kind is not AgentActionKind.FINAL
                or tuple(action.evidence_refs) != task.initial_evidence_refs
            ):
                raise ValueError("scripted action does not bind the Coordinator task")
            final_output = action.model_dump(mode="json")["final_output"]
            Draft202012Validator(profile.result_schema).validate(final_output)
        except Exception:
            # INVALID script 是刻意覆盖的 Planner schema 拒绝；这证明冻结 schema 已被
            # 实际执行，而不是 Profile/prompt/model 身份接线失败。请求侧身份事实已在发送
            # 前写入 _profile_contract，坏输出仍会以 INVALID_OUTPUT 进入 Coordinator 降级。
            return AgentResult(task_id=task.task_id, profile_id=task.profile_id, profile_version=task.profile_version, status=AgentResultStatus.INVALID_OUTPUT, failure=AgentFailure(code="SCRIPTED_SCHEMA_REJECTED", details={}), summary="SCRIPTED_SCHEMA_REJECTED", model_calls=1)
        return AgentResult(task_id=task.task_id, profile_id=task.profile_id, profile_version=task.profile_version, status=AgentResultStatus.SUCCEEDED, output=final_output, actions=(action,), evidence_refs=task.initial_evidence_refs, summary="SCRIPTED_MODEL_SUCCEEDED", model_calls=1, input_tokens=outcome.usage.input_tokens, output_tokens=outcome.usage.output_tokens, total_tokens=outcome.usage.total_tokens, latency_ms=outcome.latency_ms, cost_cny=profile.max_case_cost_cny)


def _success(request_id: str, profile: SpecialistProfile, output: dict[str, Any]) -> ModelSuccess:
    """创建带 usage 的离线成功结果，模型 ID 与请求身份仍必须精确匹配。"""

    return ModelSuccess(request_id=request_id, model_id=profile.model_id, output=output, usage=ModelUsage(input_tokens=20, output_tokens=20, total_tokens=40), response_digest=_sha256(_canonical_bytes(output)), latency_ms=Decimal("1"))


def _failure(request_id: str, category: ModelFailureCategory) -> ModelFailure:
    """构造不包含异常正文的 ScriptedModel 失败结果，用于退化路径审计。"""

    return ModelFailure(request_id=request_id, category=category, request_sent=True, response_digest=None, latency_ms=Decimal("1"))


def _is_governed_task_input(task: AgentTask, input_snapshot: Any) -> bool:
    """在 ScriptedModel 发送前核对两种 Agent 的最小、精确父事实输入。"""

    if not isinstance(input_snapshot, dict):
        return False
    try:
        if task.task_kind.value == "CONFLICT_ANALYSIS":
            if set(input_snapshot) != {
                "escalation_id",
                "escalation_digest",
                "trigger_codes",
                "evidence_bundle",
            }:
                return False
            trigger_codes = input_snapshot["trigger_codes"]
            if not isinstance(trigger_codes, list) or len(trigger_codes) < 2:
                return False
            snapshot = EvidenceBundleSnapshot.model_validate(input_snapshot["evidence_bundle"])
            return tuple(component.reference for component in snapshot.components) == task.initial_evidence_refs
        if task.task_kind.value == "LIVE_DECISION_PLANNING":
            if set(input_snapshot) != {"analysis", "evidence_bundle"}:
                return False
            analysis = input_snapshot["analysis"]
            if not isinstance(analysis, dict) or not {
                "analysis_id",
                "analysis_digest",
                "risk_codes",
                "evidence_refs",
            }.issubset(analysis):
                return False
            snapshot = EvidenceBundleSnapshot.model_validate(input_snapshot["evidence_bundle"])
            refs = tuple(component.reference for component in snapshot.components)
            return (
                refs == task.initial_evidence_refs
                and analysis["evidence_refs"]
                == [reference.model_dump(mode="json") for reference in task.initial_evidence_refs]
            )
    except Exception:
        return False
    return False


def _render_scripted_output(task: AgentTask, template: Any) -> dict[str, Any]:
    """只从 Coordinator 已绑定的任务生成演练输出，脚本不能从外部 Bundle 取证据。"""

    if not isinstance(template, dict) or set(template) != {"template"}:
        raise ValueError("scripted model output must contain exactly one template")
    input_snapshot = task.model_dump(mode="json")["input_snapshot"]
    references = [reference.model_dump(mode="json") for reference in task.initial_evidence_refs]
    if template["template"] == "ANALYST_VALID":
        return {
            "finding_codes": input_snapshot["trigger_codes"],
            "constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED"],
            "risk_codes": ["INVENTORY_CONFLICT_REQUIRES_REVIEW"],
            "explanation": "Multiple governed conflict signals require operator review.",
            "evidence_refs": references,
        }
    if template["template"] == "ANALYST_FORGED":
        return {
            # 故意回显不完整 finding；Coordinator 必须按完整 trigger 集拒绝，不能按
            # 评估脚本帮它补齐缺失事实。
            "finding_codes": input_snapshot["trigger_codes"][:1],
            "constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED"],
            "risk_codes": ["INVENTORY_CONFLICT_REQUIRES_REVIEW"],
            "explanation": "Forged incomplete analysis must be rejected.",
            "evidence_refs": references,
        }
    if template["template"] == "PLANNER_VALID":
        analysis = input_snapshot["analysis"]
        risk_flags = [
            "BACKUP_PRODUCT_REQUIRES_CONFIRMATION",
            "HUMAN_CONFIRMATION_REQUIRED",
            *analysis["risk_codes"],
        ]
        return {
            "options": [
                {
                    "option_id": "switch-backup",
                    "product_strategy": "SWITCH_TO_BACKUP",
                    "backup_product_id": "p002",
                    "host_prompt": "Await operator confirmation before backup switch.",
                    "timing": "AFTER_OPERATOR_CONFIRMATION",
                    "risk_flags": risk_flags,
                    "evidence_refs": references,
                }
            ]
        }
    if template["template"] == "PLANNER_INVALID":
        return {"options": []}
    raise ValueError("unknown scripted output template")


def _scripted_outcome_for_task(
    *,
    request: ModelRequest,
    profile: SpecialistProfile,
    task: AgentTask,
    script: Phase16Script,
) -> ModelSuccess | ModelFailure:
    """由冻结 script 选择失败/正常模式，再以当前 Coordinator task 构造实际模型结构化输出。"""

    if task.task_kind.value == "CONFLICT_ANALYSIS":
        if script.analyst_mode == "MODEL_FAILURE":
            return _failure(request.request_id, ModelFailureCategory.HTTP_ERROR)
        if script.analyst_mode == "TIMEOUT":
            return _failure(request.request_id, ModelFailureCategory.DEADLINE_EXCEEDED)
        template = (
            "ANALYST_VALID"
            if script.analyst_mode == "VALID"
            else "ANALYST_FORGED"
        )
        return _success(request.request_id, profile, _final_action_envelope(task, template))
    if task.task_kind.value == "LIVE_DECISION_PLANNING":
        template = "PLANNER_VALID" if script.planner_mode == "VALID" else "PLANNER_INVALID"
        return _success(request.request_id, profile, _final_action_envelope(task, template))
    raise ValueError("scripted evaluation received unsupported task kind")


def _final_action_envelope(task: AgentTask, template: str) -> dict[str, Any]:
    """按冻结 Profile 的 AgentAction FINAL 协议封装内层结果。"""

    return {
        "kind": "FINAL",
        "final_output": _render_scripted_output(task, {"template": template}),
        "evidence_refs": [
            reference.model_dump(mode="json") for reference in task.initial_evidence_refs
        ],
        "reason_summary": "SCRIPTED_PHASE16_EVALUATION",
    }


def _seed_case(
    case: Phase16EvaluationCase,
    now: datetime,
    *,
    store_factory: Callable[[], Any] | None = None,
) -> tuple[Any, LiveSessionWorkspace, Any]:
    """每例通过公开 Store API 创建父事实，杜绝直接写 snapshot/SQL 的评估旁路。"""

    # 默认使用内存 Store 保持 unit 评估快速；集成测试可注入已经初始化的真实
    # PostgreSQL Store，从而复用完全相同的 Coordinator、证据和重放实现，而不是复制一套路径。
    store = store_factory() if store_factory is not None else InMemoryDecisionSupportStore(clock=lambda: now)
    key = _runtime_key(case)
    workspace = store.create_workspace(LiveSessionWorkspace(live_session_id=f"session-{key}", run_key=f"run-{key}", room_id=f"room-{key}", trace_id=f"trace-{key}", anchor_id="anchor-phase16-evaluation", root_plan_run_id=f"root-{key}", event_inbox_scope_id=f"inbox-{key}", decision_trace_scope_id=f"trace-scope-{key}", replay_scope_id=f"replay-{key}", evaluation_scope_id=f"evaluation-{key}"))
    incident = Incident(incident_id=f"incident-{key}", live_session_id=workspace.live_session_id, idempotency_key=f"incident-{key}", incident_type="SOLD_OUT_COMPOSITE", source_ref_ids=(f"event-{key}",), snapshot={"product_id": "p001", "expected_version": 2}, created_at=now)
    workspace = store.append_incident(incident, expected_workspace_version=workspace.version)
    # PostgreSQL 的 lease/视图迁移必须以事务时钟为权威；内存 Store 已在构造时注入同一
    # evaluation 时钟，因此这里不能把测试专用 now 参数扩散为生产 PostgreSQL 接口。
    lease = store.acquire_operator_lock(workspace.live_session_id, "phase16-evaluation-operator", 60)
    workspace = store.advance_view(workspace.live_session_id, target_view=WorkspaceView.LIVE, expected_version=workspace.version, operator_id=lease.operator_id, fencing_token=lease.fencing_token)
    assembled = _assemble_bundle(workspace=workspace, incident=incident, case=case, now=now)
    workspace = store.append_evidence_bundle(assembled, expected_workspace_version=workspace.version)
    return store, workspace, assembled.bundle


def _actual_route(result: Any) -> Phase16ExpectedRoute:
    """把协调器真实事实归一为仅用于评估比较的闭合路径代码。"""

    if not result.selected:
        return Phase16ExpectedRoute.SINGLE_COPILOT
    if result.outcome is not None and result.outcome.status is MultiAgentOutcomeStatus.READY:
        return Phase16ExpectedRoute.MULTI_AGENT_READY
    if result.outcome is not None and result.outcome.status is MultiAgentOutcomeStatus.DEGRADED:
        return Phase16ExpectedRoute.DEGRADED
    return Phase16ExpectedRoute.NO_SEND


def _run_deterministic_single_copilot_baseline(
    case: Phase16EvaluationCase,
    bundle: Any,
) -> Phase16DeterministicBaselineResult:
    """对高冲突 case 执行同输入的确定性基线，不发送任意 Phase 16 Agent。"""

    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    if not snapshot.proposal_eligible:
        raise ValueError("paired baseline requires proposal-eligible governed evidence")
    inventory = next(
        component.payload
        for component in snapshot.components
        if component.role is EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT
    )
    danmaku = next(
        component.payload
        for component in snapshot.components
        if component.role is EvidenceRole.DANMAKU_AGGREGATE
    )
    if not isinstance(inventory, ProductInventoryPayload) or not isinstance(
        danmaku, DanmakuAggregatePayload
    ):
        raise ValueError("paired baseline requires governed inventory and danmaku payloads")
    # 复用既有 PriorityLiveOpsPolicy 执行同一 Bundle 的确定性单 Copilot 基线；它只接收
    # 投影后的冻结 evidence，不查询 Store、不写 Proposal，也不调用模型或 Task 10 账本。
    suggestion = PriorityLiveOpsPolicy().decide(
        {
            "inventory_alert": {
                "risk_open": inventory.current_product.inventory <= 0,
                "backup_available": any(
                    product.is_active and product.inventory > 0
                    for product in inventory.backup_products
                ),
            },
            "danmaku": {"question_count": sum(topic.count for topic in danmaku.topics)},
            "evidence_refs": [
                component.reference.model_dump(mode="json")
                for component in snapshot.components
            ],
        }
    )
    return Phase16DeterministicBaselineResult(
        logical_case_id=case.logical_case_id,
        evidence_bundle_id=bundle.evidence_bundle_id,
        evidence_bundle_digest=snapshot.bundle_digest,
        route=Phase16ExpectedRoute.SINGLE_COPILOT,
        action=suggestion.action.value,
        model_calls=0,
    )


def _lineage_identity_correct(result: Any, bundle: Any) -> bool:
    """逐字段验证升级、分析、方案和终态都绑定同一 Bundle 的精确 ID 与摘要。"""

    if not result.selected:
        return all(
            item is None
            for item in (result.escalation, result.analysis, result.proposal, result.outcome)
        )
    escalation = result.escalation
    outcome = result.outcome
    if escalation is None or outcome is None:
        return False
    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    if (
        escalation.live_session_id != bundle.live_session_id
        or escalation.incident_id != bundle.incident_id
        or escalation.evidence_bundle_id != bundle.evidence_bundle_id
        or escalation.evidence_bundle_digest != snapshot.bundle_digest
        or outcome.escalation_id != escalation.escalation_id
        or outcome.escalation_digest != escalation.escalation_digest
        or outcome.evidence_bundle_id != bundle.evidence_bundle_id
        or outcome.evidence_bundle_digest != snapshot.bundle_digest
    ):
        return False
    analysis = result.analysis
    if analysis is not None and (
        analysis.escalation_id != escalation.escalation_id
        or analysis.evidence_bundle_id != bundle.evidence_bundle_id
        or analysis.evidence_bundle_digest != snapshot.bundle_digest
        or tuple(analysis.evidence_refs)
        != tuple(component.reference for component in snapshot.components)
    ):
        return False
    proposal = result.proposal
    if proposal is None:
        return outcome.status is MultiAgentOutcomeStatus.DEGRADED
    lineage = proposal.multi_agent_lineage
    return bool(
        analysis is not None
        and lineage is not None
        and proposal.evidence_bundle_id == bundle.evidence_bundle_id
        and proposal.evidence_bundle_digest == snapshot.bundle_digest
        and lineage.escalation_id == escalation.escalation_id
        and lineage.escalation_digest == escalation.escalation_digest
        and lineage.analysis_id == analysis.analysis_id
        and lineage.analysis_digest == analysis.analysis_digest
        and lineage.evidence_bundle_id == bundle.evidence_bundle_id
        and lineage.evidence_bundle_digest == snapshot.bundle_digest
        and outcome.status is MultiAgentOutcomeStatus.READY
        and outcome.analysis_id == analysis.analysis_id
        and outcome.analysis_digest == analysis.analysis_digest
        and outcome.proposal_id == proposal.proposal_id
        and outcome.proposal_digest
        == canonical_json_sha256(proposal.model_dump(mode="json"))
    )


def run_phase16_scripted_evaluation(
    dataset: Phase16EvaluationDataset,
    *,
    store_factory: Callable[[], Any] | None = None,
    restart_store_factory: Callable[[], Any] | None = None,
) -> Phase16EvaluationReport:
    """顺序重放 48 例，报告配对身份、路由、调用、预算和失败语义而不访问网络。"""

    _validate_dataset_for_run(dataset)
    results: list[Phase16EvaluationCaseResult] = []
    total_cost = Decimal("0")
    for case in dataset.cases:
        label = dataset.labels[case.case_id]
        script = dataset.scripts[case.case_id]
        # Store 的 escalation trigger 在事务边界使用数据库/进程当前时间校验 freshness，
        # 因此运行时基线必须是本次评估的真实 UTC，而不是数据集生成时的历史常量。
        # case 身份、脚本、Manifest 和请求 ID 均不含此瞬时值，字节稳定性不受影响。
        runtime_now = datetime.now(timezone.utc)
        coordinator_now = runtime_now + timedelta(seconds=30) if case.input["stale"] else runtime_now
        store, workspace, bundle = _seed_case(case, runtime_now, store_factory=store_factory)
        baseline = (
            _run_deterministic_single_copilot_baseline(case, bundle)
            if label.paired_baseline_required
            else None
        )
        runner = _EvaluationScriptedRunner(case=case, script=script, now=coordinator_now)
        result = asyncio.run(HighConflictEscalationCoordinator(store=store, analyst_runner=runner, planner_runner=runner, clock=lambda instant=coordinator_now: instant).run_automatic(bundle, expected_workspace_version=workspace.version))
        actual = _actual_route(result)
        if label.expected_route is Phase16ExpectedRoute.SINGLE_COPILOT:
            route_correct = actual is Phase16ExpectedRoute.SINGLE_COPILOT
        elif label.expected_route is Phase16ExpectedRoute.NO_SEND:
            route_correct = actual in {Phase16ExpectedRoute.SINGLE_COPILOT, Phase16ExpectedRoute.NO_SEND}
        else:
            route_correct = actual is label.expected_route
        analyst_calls = sum(task.task_kind.value == "CONFLICT_ANALYSIS" for task in runner.calls)
        planner_calls = sum(task.task_kind.value == "LIVE_DECISION_PLANNING" for task in runner.calls)
        ready = int(result.outcome is not None and result.outcome.status is MultiAgentOutcomeStatus.READY)
        degraded = int(result.outcome is not None and result.outcome.status is MultiAgentOutcomeStatus.DEGRADED)
        no_send = analyst_calls == 0 and planner_calls == 0
        lineage_identity_correct = _lineage_identity_correct(result, bundle)
        paired_baseline_executed = baseline is not None
        paired_identity_correct = not label.paired_baseline_required or bool(
            baseline is not None
            and baseline.logical_case_id == case.logical_case_id
            and baseline.evidence_bundle_id == bundle.evidence_bundle_id
            and baseline.evidence_bundle_digest
            == EvidenceBundleSnapshot.model_validate(bundle.snapshot).bundle_digest
            and baseline.route is Phase16ExpectedRoute.SINGLE_COPILOT
            and baseline.model_calls == 0
            and lineage_identity_correct
        )
        failure_correct = (label.expected_route is Phase16ExpectedRoute.NO_SEND and no_send) or (label.expected_route is Phase16ExpectedRoute.DEGRADED and degraded == 1) or (label.expected_route in {Phase16ExpectedRoute.SINGLE_COPILOT, Phase16ExpectedRoute.MULTI_AGENT_READY})
        # 对同一结果按原 expected Workspace 版本再次调用，证明已持久化的 escalation/analysis/
        # proposal/outcome 是恢复事实，不会让 ScriptedModel 产生第二次发送。
        replay_store = restart_store_factory() if restart_store_factory is not None else store
        replay_runner = _EvaluationScriptedRunner(
            case=case,
            script=script,
            now=coordinator_now,
        )
        replay = asyncio.run(HighConflictEscalationCoordinator(store=replay_store, analyst_runner=replay_runner, planner_runner=replay_runner, clock=lambda instant=coordinator_now: instant).run_automatic(bundle, expected_workspace_version=workspace.version))
        replay_identity_correct = (
            _actual_route(replay) is actual
            and _lineage_identity_correct(replay, bundle)
            and (result.escalation, result.analysis, result.proposal, result.outcome)
            == (replay.escalation, replay.analysis, replay.proposal, replay.outcome)
            and replay_runner.calls == []
        )
        total_cost += runner.reserved_cost_cny
        results.append(Phase16EvaluationCaseResult(case_id=case.case_id, expected_route=label.expected_route, actual_route=actual, call_sequence=tuple(task.task_kind.value for task in runner.calls), analyst_calls=analyst_calls, planner_calls=planner_calls, ready_outcomes=ready, degraded_outcomes=degraded, no_send=no_send, paired_identity_correct=paired_identity_correct, paired_baseline_executed=paired_baseline_executed, lineage_identity_correct=lineage_identity_correct, model_input_bound=runner.model_input_bound, model_metadata_safe=runner.model_metadata_safe, profile_contract_verified=runner.profile_contract_verified, failure_semantics_correct=failure_correct, replay_identity_correct=replay_identity_correct))
    return Phase16EvaluationReport(dataset_id=dataset.manifest.dataset_id, total_cases=len(results), normal_single_copilot_cases=sum(case.kind is Phase16CaseKind.NORMAL_SINGLE_COPILOT for case in dataset.cases), high_conflict_paired_cases=sum(case.kind is Phase16CaseKind.HIGH_CONFLICT_PAIRED for case in dataset.cases), adversarial_degraded_cases=sum(case.kind is Phase16CaseKind.ADVERSARIAL_DEGRADED for case in dataset.cases), route_correct_cases=sum(item.actual_route == item.expected_route or (item.expected_route is Phase16ExpectedRoute.NO_SEND and item.actual_route in {Phase16ExpectedRoute.SINGLE_COPILOT, Phase16ExpectedRoute.NO_SEND}) for item in results), paired_identity_correct_cases=sum(item.paired_identity_correct and dataset.labels[item.case_id].paired_baseline_required for item in results), paired_baseline_executed_cases=sum(item.paired_baseline_executed for item in results), analyst_calls=sum(item.analyst_calls for item in results), planner_calls=sum(item.planner_calls for item in results), ready_outcomes=sum(item.ready_outcomes for item in results), degraded_outcomes=sum(item.degraded_outcomes for item in results), no_send_cases=sum(item.no_send for item in results), lineage_identity_correct_cases=sum(item.lineage_identity_correct for item in results), model_input_bound_cases=sum(item.model_input_bound for item in results), model_metadata_safe_cases=sum(item.model_metadata_safe for item in results), profile_contract_verified_cases=sum(item.profile_contract_verified for item in results), replay_identity_correct_cases=sum(item.replay_identity_correct for item in results), scripted_reserved_cost_cny=total_cost, real_model_calls=0, case_results=tuple(results))
