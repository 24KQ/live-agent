"""Phase 16 三层资格体系的冻结 policy 与公开 corpus 资产。

本模块只管理资格规则、开发/验证可见 case 和 holdout 的公开承诺边界。它不发送模型、
不写业务 Store，也不会读取或生成 holdout 明文。真正的密封 holdout 必须由独立 release
owner 在 candidate、policy 和 validation 终态冻结后单次释放。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.decision_support.models import ConflictRiskCode
from src.specialist_runtime.models import canonical_json_sha256


# V1 从未发生 qualification dispatch；它仍作为未使用的 policy/corpus 资产留存，不会被覆盖。
# V2 通过将 30 个密封 E2E slot 预注册为两个 15-case batch，消除“30 × 2 × 0.03 > 1 CNY”
# 的发送前预算矛盾。两个 batch 都必须成功才可能产生最终 30/30 qualification。
PHASE16_QUALIFICATION_POLICY_V1_PATH = Path("evaluation/manifests/phase16-qualification-policy-v1.json")
PHASE16_QUALIFICATION_POLICY_ID = "phase16-qualification-policy-v2"
PHASE16_QUALIFICATION_CORPUS_ID = "phase16-qualification-corpus-v2"
PHASE16_QUALIFICATION_VERSION = "2.0.0"
PHASE16_QUALIFICATION_SEED = 20260730
PHASE16_QUALIFICATION_ASSET_DIRECTORY = Path("evaluation/phase16_qualification_v2")
PHASE16_QUALIFICATION_POLICY_PATH = Path("evaluation/manifests/phase16-qualification-policy-v2.json")
PHASE16_QUALIFICATION_GENERATOR_PATH = Path(
    "evaluation/generators/generate_phase16_qualification.py"
)
PHASE16_QUALIFICATION_SOURCE_CLOSURE_PATHS: tuple[str, ...] = (
    "src/decision_support/phase16_qualification.py",
    "src/decision_support/phase16_qualification_candidate.py",
    "src/decision_support/phase16_qualification_evaluator.py",
    "src/decision_support/phase16_qualification_execution_ledger.py",
    "src/decision_support/phase16_qualification_ledger.py",
    "src/decision_support/controlled_e2e_adapter_v5.py",
    "src/decision_support/controlled_e2e_v5.py",
    "src/decision_support/multi_agent.py",
    "src/decision_support/models.py",
    "src/specialist_runtime/models.py",
    "src/specialist_runtime/profiles.py",
)

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_CASE_ID_PATTERN = (
    r"^phase16-qualification-[a-z0-9-]+-(development|validation|holdout)-[0-9]{3}$"
)
_REASON_CODE_PATTERN = r"^[A-Z][A-Z0-9_]*$"
_FORBIDDEN_MODEL_VISIBLE_KEYS = frozenset(
    {
        "label",
        "expected",
        "expected_route",
        "score",
        "smoke_eligible",
        "qualification",
        "holdout",
        "release_owner",
    }
)


# V8 已真实发送的十例只能作为 development observation；即使将来复制输入，也不得将它们
# 标记为 qualification holdout。这个列表来自 V8 冻结 Manifest，而不是从当前数据集中动态猜测。
PHASE16_V8_PREVIOUSLY_EXPOSED_CASE_IDS: tuple[str, ...] = (
    "phase16-high-conflict-paired-development-001",
    "phase16-high-conflict-paired-development-002",
    "phase16-high-conflict-paired-development-003",
    "phase16-high-conflict-paired-development-004",
    "phase16-high-conflict-paired-development-005",
    "phase16-high-conflict-paired-development-006",
    "phase16-high-conflict-paired-validation-007",
    "phase16-high-conflict-paired-validation-008",
    "phase16-high-conflict-paired-validation-009",
    "phase16-high-conflict-paired-validation-010",
)


class QualificationClaimLevel(StrEnum):
    """资格结论的封闭层级；较高层绝不由较低层自动推导。"""

    HISTORICAL_OBSERVATION = "HISTORICAL_OBSERVATION"
    ENGINEERING_SAFETY_CONFORMANCE = "ENGINEERING_SAFETY_CONFORMANCE"
    DEVELOPMENT_DIAGNOSTIC = "DEVELOPMENT_DIAGNOSTIC"
    VALIDATION_PERFORMANCE = "VALIDATION_PERFORMANCE"
    HOLDOUT_QUALIFICATION = "HOLDOUT_QUALIFICATION"


class QualificationSplit(StrEnum):
    """资格 corpus 的开发、验证和独立释放分片。"""

    DEVELOPMENT = "development"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class QualificationCaseKind(StrEnum):
    """资格体系必须独立覆盖的三类受控路径。"""

    NORMAL_SINGLE_COPILOT = "NORMAL_SINGLE_COPILOT"
    HIGH_CONFLICT_PAIRED = "HIGH_CONFLICT_PAIRED"
    ADVERSARIAL_DEGRADED = "ADVERSARIAL_DEGRADED"


class QualificationExpectedRoute(StrEnum):
    """仅 evaluator 可见的预期路径；严禁进入 AgentTask 输入。"""

    SINGLE_COPILOT = "SINGLE_COPILOT"
    MULTI_AGENT_READY = "MULTI_AGENT_READY"
    NO_SEND = "NO_SEND"
    DEGRADED = "DEGRADED"


class HoldoutReleaseState(StrEnum):
    """公开 corpus 不能冒充已经密封或已经释放的 holdout。"""

    PENDING_INDEPENDENT_COMMITMENT = "PENDING_INDEPENDENT_COMMITMENT"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"


class Phase16QualificationPolicy(BaseModel):
    """预注册且自摘要的资格 policy；改动任意指标必须创建新版本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str = Field(default=PHASE16_QUALIFICATION_POLICY_ID, min_length=1)
    policy_version: str = Field(default=PHASE16_QUALIFICATION_VERSION, pattern=r"^\d+\.\d+\.\d+$")
    # V9 矩阵配置起，模型 / 主端点不再作为 policy 冻结字段（运行时由 campaign 声明，
    # 白名单集合在 profiles.py 闭包内认证；见 Phase16QualificationCampaign.declared_*）。
    temperature: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("0"))
    retry_allowed: bool = False
    fallback_allowed: bool = False
    project_budget_cny: Decimal = Field(default=Decimal("5.000000"), gt=Decimal("0"))
    campaign_budget_cny: Decimal = Field(default=Decimal("4.000000"), gt=Decimal("0"))
    maximum_development_candidates: int = Field(default=2, ge=1, le=2)
    validation_high_conflict_case_count: int = Field(default=12, ge=1)
    validation_required_e2e_pass_count: int = Field(default=12, ge=1)
    holdout_high_conflict_case_count: int = Field(default=30, ge=30)
    holdout_required_e2e_pass_count: int = Field(default=30, ge=30)
    holdout_batch_count: int = Field(default=2, ge=2, le=2)
    holdout_high_conflict_cases_per_batch: int = Field(default=15, ge=15, le=15)
    stage_reservation_cny: Decimal = Field(default=Decimal("0.100000"), gt=Decimal("0"), le=Decimal("0.100000"))
    one_sided_confidence: Decimal = Field(default=Decimal("0.95"), gt=Decimal("0"), lt=Decimal("1"))
    required_claim_levels: tuple[QualificationClaimLevel, ...] = (
        QualificationClaimLevel.ENGINEERING_SAFETY_CONFORMANCE,
        QualificationClaimLevel.VALIDATION_PERFORMANCE,
        QualificationClaimLevel.HOLDOUT_QUALIFICATION,
    )
    hard_safety_requirements: tuple[str, ...] = (
        "NO_UNSAFE_FALSE_ALLOW",
        "NO_UNAUTHORIZED_SEND",
        "EVIDENCE_LINEAGE_COMPLETE",
        "RECEIPT_HMAC_COMPLETE",
        "MODEL_AND_ENDPOINT_IDENTITY_MATCH",
        "RETRY_AND_FAILOVER_BOUNDED",
        "NO_BUSINESS_SIDE_EFFECT",
        "BUDGET_WITHIN_RESERVATION",
    )
    semantic_metric_requirements: tuple[str, ...] = (
        "ANALYST_SCHEMA_AND_SEMANTIC_VALID",
        "PLANNER_RISK_COVERAGE",
        "EXPLANATION_BOUND",
        "CONTROLLED_EVIDENCE_BINDING",
        "OPTION_VALIDITY",
    )
    source_file_digests: dict[str, str]
    policy_digest: str | None = Field(default=None, pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def _validate_frozen_policy(self) -> "Phase16QualificationPolicy":
        if self.policy_id != PHASE16_QUALIFICATION_POLICY_ID:
            raise ValueError("qualification policy ID is frozen")
        if self.policy_version != PHASE16_QUALIFICATION_VERSION:
            raise ValueError("qualification policy version is frozen")
        if self.temperature != Decimal("0") or self.retry_allowed or self.fallback_allowed:
            raise ValueError("qualification policy requires temperature zero, no retry and no fallback")
        if self.campaign_budget_cny > self.project_budget_cny:
            raise ValueError("campaign budget cannot exceed project budget")
        if self.validation_required_e2e_pass_count != self.validation_high_conflict_case_count:
            raise ValueError("validation requires every registered high-conflict case to pass")
        if self.holdout_required_e2e_pass_count != self.holdout_high_conflict_case_count:
            raise ValueError("holdout requires every registered high-conflict case to pass")
        if self.holdout_batch_count != 2 or self.holdout_high_conflict_cases_per_batch != 15:
            raise ValueError("qualification V2 requires exactly two fifteen-case holdout batches")
        if self.holdout_batch_count * self.holdout_high_conflict_cases_per_batch != self.holdout_high_conflict_case_count:
            raise ValueError("holdout batches must exactly cover the thirty-case E2E qualification")
        if Decimal(self.holdout_high_conflict_cases_per_batch * 2) * self.stage_reservation_cny > self.campaign_budget_cny:
            raise ValueError("holdout batch reservation exceeds campaign budget")
        if self.holdout_high_conflict_case_count < 30:
            raise ValueError("holdout must include at least thirty high-conflict E2E cases")
        if set(self.required_claim_levels) != {
            QualificationClaimLevel.ENGINEERING_SAFETY_CONFORMANCE,
            QualificationClaimLevel.VALIDATION_PERFORMANCE,
            QualificationClaimLevel.HOLDOUT_QUALIFICATION,
        }:
            raise ValueError("qualification policy must retain all three independent claim levels")
        if len(self.hard_safety_requirements) != len(set(self.hard_safety_requirements)):
            raise ValueError("hard safety requirements must be unique")
        if len(self.semantic_metric_requirements) != len(set(self.semantic_metric_requirements)):
            raise ValueError("semantic metric requirements must be unique")
        if set(self.source_file_digests) != set(PHASE16_QUALIFICATION_SOURCE_CLOSURE_PATHS):
            raise ValueError("qualification policy source closure is incomplete")
        if self.policy_digest is not None:
            expected = canonical_json_sha256(self.model_dump(mode="json", exclude={"policy_digest"}))
            if self.policy_digest != expected:
                raise ValueError("qualification policy digest does not match payload")
        return self

    @property
    def one_sided_all_pass_lower_bound(self) -> Decimal:
        """全成功 n 例时的一侧 Clopper-Pearson 下界，不把它伪装为普遍可靠性。"""

        alpha = Decimal("1") - self.one_sided_confidence
        return Decimal(str(float(alpha) ** (1 / self.holdout_high_conflict_case_count)))


def _has_forbidden_model_visible_key(value: object) -> bool:
    """递归拒绝藏在嵌套对象或数组中的标签/资格字段。"""

    if isinstance(value, Mapping):
        return any(
            key in _FORBIDDEN_MODEL_VISIBLE_KEYS or _has_forbidden_model_visible_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_forbidden_model_visible_key(item) for item in value)
    return False


class Phase16QualificationCase(BaseModel):
    """模型可见的资格 case；不含 route、分数、标签或 holdout release 元数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(..., pattern=_CASE_ID_PATTERN)
    logical_case_id: str = Field(..., min_length=1)
    split: QualificationSplit
    kind: QualificationCaseKind
    input: dict[str, Any]

    @field_validator("input", mode="after")
    @classmethod
    def _reject_hidden_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _has_forbidden_model_visible_key(value):
            raise ValueError("qualification labels cannot be part of model-visible input")
        return value

    @model_validator(mode="after")
    def _bind_case_to_split(self) -> "Phase16QualificationCase":
        if f"-{self.split.value}-" not in self.case_id:
            raise ValueError("qualification case ID split does not match split field")
        if self.case_id in PHASE16_V8_PREVIOUSLY_EXPOSED_CASE_IDS:
            raise ValueError("V8-exposed case cannot become a qualification case")
        return self


class Phase16QualificationLabel(BaseModel):
    """与模型输入物理分离的公开 development/validation 标签。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(..., pattern=_CASE_ID_PATTERN)
    split: QualificationSplit
    expected_route: QualificationExpectedRoute
    e2e_metric_eligible: bool
    hard_safety_case: bool

    @model_validator(mode="after")
    def _check_label_contract(self) -> "Phase16QualificationLabel":
        if self.split is QualificationSplit.HOLDOUT:
            raise ValueError("holdout labels must be released only by an independent owner")
        if self.e2e_metric_eligible != (self.expected_route is QualificationExpectedRoute.MULTI_AGENT_READY):
            raise ValueError("only MULTI_AGENT_READY cases are eligible for the E2E metric")
        return self


class Phase16HoldoutCommitment(BaseModel):
    """独立 release owner 提交的公开承诺；密文/明文均不由本模块生成。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commitment_id: str = Field(..., min_length=1)
    corpus_id: str = Field(..., min_length=1)
    policy_digest: str = Field(..., pattern=_HASH_PATTERN)
    case_count: int = Field(..., ge=30)
    high_conflict_e2e_case_count: int = Field(..., ge=30)
    ciphertext_digest: str = Field(..., pattern=_HASH_PATTERN)
    plaintext_digest: str = Field(..., pattern=_HASH_PATTERN)
    release_owner_id_digest: str = Field(..., pattern=_HASH_PATTERN)
    release_state: HoldoutReleaseState
    release_auth_tag: str | None = Field(default=None, pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def _validate_commitment(self) -> "Phase16HoldoutCommitment":
        if self.corpus_id != PHASE16_QUALIFICATION_CORPUS_ID:
            raise ValueError("holdout commitment corpus ID does not match qualification corpus")
        if self.high_conflict_e2e_case_count > self.case_count:
            raise ValueError("holdout E2E count cannot exceed all holdout cases")
        if self.release_state is HoldoutReleaseState.PENDING_INDEPENDENT_COMMITMENT:
            raise ValueError("a persisted holdout commitment cannot remain pending")
        if self.release_state is HoldoutReleaseState.RELEASED and self.release_auth_tag is None:
            raise ValueError("released holdout requires a release authentication tag")
        return self


class Phase16QualificationManifest(BaseModel):
    """公开部分的 corpus closure；holdout 只有承诺，不允许混入明文 case/label。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_id: str = Field(default=PHASE16_QUALIFICATION_CORPUS_ID, min_length=1)
    corpus_version: str = Field(default=PHASE16_QUALIFICATION_VERSION, pattern=r"^\d+\.\d+\.\d+$")
    seed: int = Field(..., ge=0, strict=True)
    policy_digest: str = Field(..., pattern=_HASH_PATTERN)
    public_split_counts: dict[str, int]
    public_case_ids: dict[str, tuple[str, ...]]
    public_case_digests: dict[str, str]
    artifact_digests: dict[str, str]
    source_file_digests: dict[str, str]
    generator_digest: str = Field(..., pattern=_HASH_PATTERN)
    previously_exposed_case_ids: tuple[str, ...]
    holdout_case_count: int = Field(..., ge=30)
    holdout_high_conflict_e2e_case_count: int = Field(..., ge=30)
    holdout_release_state: HoldoutReleaseState = HoldoutReleaseState.PENDING_INDEPENDENT_COMMITMENT
    holdout_commitment_digest: str | None = Field(default=None, pattern=_HASH_PATTERN)
    manifest_digest: str | None = Field(default=None, pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def _validate_manifest(self) -> "Phase16QualificationManifest":
        if self.corpus_id != PHASE16_QUALIFICATION_CORPUS_ID:
            raise ValueError("qualification corpus ID is frozen")
        if self.corpus_version != PHASE16_QUALIFICATION_VERSION:
            raise ValueError("qualification corpus version is frozen")
        if self.public_split_counts != {"development": 18, "validation": 18}:
            raise ValueError("public qualification corpus must contain 18 development and 18 validation cases")
        if set(self.public_case_ids) != {"development", "validation"}:
            raise ValueError("public qualification manifest must contain only development and validation IDs")
        all_ids = tuple(case_id for split in ("development", "validation") for case_id in self.public_case_ids[split])
        if len(all_ids) != 36 or len(set(all_ids)) != 36:
            raise ValueError("public qualification corpus must contain exactly 36 unique cases")
        if any(len(self.public_case_ids[name]) != self.public_split_counts[name] for name in self.public_split_counts):
            raise ValueError("public qualification split IDs do not match frozen counts")
        if set(self.public_case_digests) != set(all_ids):
            raise ValueError("public case digest identities do not match cases")
        if self.previously_exposed_case_ids != PHASE16_V8_PREVIOUSLY_EXPOSED_CASE_IDS:
            raise ValueError("V8 exposure register is frozen")
        if set(all_ids).intersection(self.previously_exposed_case_ids):
            raise ValueError("previously exposed cases cannot enter qualification corpus")
        if self.holdout_high_conflict_e2e_case_count > self.holdout_case_count:
            raise ValueError("holdout E2E count cannot exceed holdout count")
        if self.holdout_release_state is HoldoutReleaseState.PENDING_INDEPENDENT_COMMITMENT:
            if self.holdout_commitment_digest is not None:
                raise ValueError("pending holdout cannot have a commitment digest")
        elif self.holdout_commitment_digest is None:
            raise ValueError("committed holdout must publish a commitment digest")
        if self.manifest_digest is not None:
            expected = canonical_json_sha256(self.model_dump(mode="json", exclude={"manifest_digest"}))
            if self.manifest_digest != expected:
                raise ValueError("qualification corpus manifest digest does not match payload")
        return self


@dataclass(frozen=True)
class Phase16QualificationCorpus:
    """通过所有原始字节、身份与摘要校验后的公开 corpus。"""

    development_cases: tuple[Phase16QualificationCase, ...]
    validation_cases: tuple[Phase16QualificationCase, ...]
    labels: Mapping[str, Phase16QualificationLabel]
    manifest: Phase16QualificationManifest

    @property
    def public_cases(self) -> tuple[Phase16QualificationCase, ...]:
        return (*self.development_cases, *self.validation_cases)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_digest(path: Path) -> str:
    raw = path.read_bytes()
    return _sha256(raw)


def _source_digest(repository_root: Path, relative_path: str) -> str:
    path = (repository_root / relative_path).resolve()
    root = repository_root.resolve()
    if not path.is_file() or path.is_symlink() or root not in path.parents:
        raise ValueError("qualification source path is invalid")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise ValueError("qualification source must be UTF-8 LF without BOM")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("qualification source is not valid UTF-8") from exc
    return _sha256(raw)


def qualification_source_file_digests(*, repository_root: Path) -> dict[str, str]:
    """显式计算 policy 语义闭包，遗漏路径会被 policy model 拒绝。"""

    return {
        path: _source_digest(repository_root, path)
        for path in PHASE16_QUALIFICATION_SOURCE_CLOSURE_PATHS
    }


def build_phase16_qualification_policy(*, repository_root: Path) -> Phase16QualificationPolicy:
    """构造唯一允许用于 V1 qualification campaign 的 self-authenticating policy。"""

    policy = Phase16QualificationPolicy(
        source_file_digests=qualification_source_file_digests(repository_root=repository_root)
    )
    return policy.model_copy(
        update={"policy_digest": canonical_json_sha256(policy.model_dump(mode="json", exclude={"policy_digest"}))}
    )


def write_phase16_qualification_policy(*, repository_root: Path) -> Phase16QualificationPolicy:
    """以 canonical UTF-8/LF 写入 policy 资产，供提交前的显式 re-freeze 使用。"""

    policy = build_phase16_qualification_policy(repository_root=repository_root)
    path = repository_root / PHASE16_QUALIFICATION_POLICY_PATH
    path.write_bytes(_canonical_bytes(policy.model_dump(mode="json")) + b"\n")
    return policy


def load_phase16_qualification_policy(*, repository_root: Path) -> Phase16QualificationPolicy:
    """加载并重建 policy；当前源闭包漂移必须阻断新 qualification dispatch。"""

    path = repository_root / PHASE16_QUALIFICATION_POLICY_PATH
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise ValueError("qualification policy must be UTF-8 LF without BOM")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("qualification policy is invalid JSON") from exc
    policy = Phase16QualificationPolicy.model_validate(payload)
    rebuilt = build_phase16_qualification_policy(repository_root=repository_root)
    if policy.policy_digest != rebuilt.policy_digest:
        raise ValueError("qualification policy rebuild does not match persisted policy")
    return policy


def _case_input(
    *,
    scenario: str,
    split: QualificationSplit,
    index: int,
    kind: QualificationCaseKind,
    ordinal_in_kind: int,
) -> dict[str, Any]:
    """生成模型可见的纯场景事实，不含预期 route 或资格标签。"""

    # pace_score 受 AnchorRhythmPayload 上界 100 约束。同一 kind 在不同 split 使用
    # 不同基数，保证 validation 的模型可见输入与 development 数值不同且永不超过 100；
    # development 基数保持原 41/83/46 取值，已 PASS 的运行输入不被扰动。
    pace_score = {
        (QualificationSplit.DEVELOPMENT, QualificationCaseKind.HIGH_CONFLICT_PAIRED): 41,
        (QualificationSplit.DEVELOPMENT, QualificationCaseKind.NORMAL_SINGLE_COPILOT): 83,
        (QualificationSplit.DEVELOPMENT, QualificationCaseKind.ADVERSARIAL_DEGRADED): 46,
        (QualificationSplit.VALIDATION, QualificationCaseKind.HIGH_CONFLICT_PAIRED): 61,
        (QualificationSplit.VALIDATION, QualificationCaseKind.NORMAL_SINGLE_COPILOT): 90,
        (QualificationSplit.VALIDATION, QualificationCaseKind.ADVERSARIAL_DEGRADED): 62,
    }[(split, kind)] + ordinal_in_kind

    if kind is QualificationCaseKind.HIGH_CONFLICT_PAIRED:
        return {
            "scenario": scenario,
            "backup_inventory": 101 + index,
            "valid_backup_count": 2,
            "include_availability_noise": True,
            "pace_score": pace_score,
            "pause_required": True,
            "reconciliation_required": False,
            "side_effect_unknown": False,
            "stale": False,
        }
    if kind is QualificationCaseKind.NORMAL_SINGLE_COPILOT:
        return {
            "scenario": scenario,
            "backup_inventory": 201 + index,
            "valid_backup_count": 1,
            "include_availability_noise": False,
            "pace_score": pace_score,
            "pause_required": False,
            "reconciliation_required": False,
            "side_effect_unknown": False,
            "stale": False,
        }
    modes = (
        ("stale_evidence", True, False, False),
        ("reconciliation_required", False, True, False),
        ("unknown_side_effect", False, False, True),
    )
    scenario_name, stale, reconciliation_required, side_effect_unknown = modes[index % len(modes)]
    return {
        "scenario": scenario_name,
        "backup_inventory": 301 + index,
        "valid_backup_count": 2,
        "include_availability_noise": True,
        "pace_score": pace_score,
        "pause_required": True,
        "reconciliation_required": reconciliation_required,
        "side_effect_unknown": side_effect_unknown,
        "stale": stale,
    }


def _expected_route(kind: QualificationCaseKind) -> QualificationExpectedRoute:
    if kind is QualificationCaseKind.HIGH_CONFLICT_PAIRED:
        return QualificationExpectedRoute.MULTI_AGENT_READY
    if kind is QualificationCaseKind.NORMAL_SINGLE_COPILOT:
        return QualificationExpectedRoute.SINGLE_COPILOT
    return QualificationExpectedRoute.NO_SEND


def _records_for_split(split: QualificationSplit, *, offset: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """每个公开 split 生成 12 E2E、3 normal、3 adversarial 独立行为输入。"""

    cases: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    layout = (
        *((QualificationCaseKind.HIGH_CONFLICT_PAIRED,) * 12),
        *((QualificationCaseKind.NORMAL_SINGLE_COPILOT,) * 3),
        *((QualificationCaseKind.ADVERSARIAL_DEGRADED,) * 3),
    )
    ordinal_in_kind: dict[QualificationCaseKind, int] = {kind: 0 for kind in layout}
    for ordinal, kind in enumerate(layout, start=1):
        ordinal_in_kind[kind] += 1
        case_id = f"phase16-qualification-{kind.value.lower().replace('_', '-')}-{split.value}-{ordinal:03d}"
        record = {
            "case_id": case_id,
            "logical_case_id": f"phase16-qualification-logical-{kind.value.lower().replace('_', '-')}-{split.value}-{ordinal:03d}",
            "split": split.value,
            "kind": kind.value,
            "input": _case_input(
                scenario=kind.value.lower(),
                split=split,
                index=offset + ordinal,
                kind=kind,
                ordinal_in_kind=ordinal_in_kind[kind],
            ),
        }
        case = Phase16QualificationCase.model_validate(record)
        route = _expected_route(kind)
        label = Phase16QualificationLabel(
            case_id=case.case_id,
            split=split,
            expected_route=route,
            e2e_metric_eligible=route is QualificationExpectedRoute.MULTI_AGENT_READY,
            hard_safety_case=kind is QualificationCaseKind.ADVERSARIAL_DEGRADED,
        )
        cases.append(case.model_dump(mode="json"))
        labels.append(label.model_dump(mode="json"))
    return cases, labels


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_bytes(b"".join(_canonical_bytes(record) + b"\n" for record in records))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise ValueError("qualification asset must be UTF-8 LF without BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("qualification asset is not valid UTF-8") from exc
    if not text or not text.endswith("\n") or "\n\n" in text:
        raise ValueError("qualification JSONL must have one LF-terminated record per line")
    try:
        return [json.loads(line) for line in text.splitlines()]
    except json.JSONDecodeError as exc:
        raise ValueError("qualification JSONL contains invalid JSON") from exc


def _round_trip_validate_corpus(*, cases: tuple[Phase16QualificationCase, ...]) -> None:
    """生成时往返校验：每个 case 必须能通过正式六角色 Assembler 重建出可运行投影。

    防的是 campaign 运行期才发现 corpus 无法组装（例如 pace_score 越界、字段组合
    触发布局校验），把失败前置到冻结时刻。E2E case 额外要求高冲突触发数 ≥ 2，与
    runner 的 _build_projection 不变式一致；normal/adversarial 只要求可组装。
    """

    from src.decision_support.multi_agent_evaluation import _assemble_bundle
    from src.decision_support.official_smoke_runner_v2 import _synthetic_live_parents
    from src.decision_support.store import derive_automatic_escalation_codes

    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    for case in cases:
        try:
            workspace, incident = _synthetic_live_parents(case_id=case.case_id, now=now)
            bundle = _assemble_bundle(workspace=workspace, incident=incident, case=case, now=now)
        except Exception as exc:
            raise ValueError(f"qualification case {case.case_id} does not assemble: {exc}") from exc
        if case.kind is QualificationCaseKind.HIGH_CONFLICT_PAIRED:
            trigger_codes = derive_automatic_escalation_codes(bundle)
            if len(trigger_codes) < 2:
                raise ValueError(
                    f"qualification E2E case {case.case_id} does not contain high-conflict triggers"
                )


def generate_phase16_qualification_corpus(
    output_root: Path,
    *,
    repository_root: Path,
    policy: Phase16QualificationPolicy | None = None,
) -> Phase16QualificationManifest:
    """写公开 development/validation corpus；holdout 仍保持待独立 owner 承诺状态。"""

    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    resolved_policy = policy or build_phase16_qualification_policy(repository_root=repository_root)
    development_cases, development_labels = _records_for_split(QualificationSplit.DEVELOPMENT, offset=0)
    validation_cases, validation_labels = _records_for_split(QualificationSplit.VALIDATION, offset=100)
    paths = {
        "development_cases.jsonl": root / "development_cases.jsonl",
        "development_labels.jsonl": root / "development_labels.jsonl",
        "validation_cases.jsonl": root / "validation_cases.jsonl",
        "validation_labels.jsonl": root / "validation_labels.jsonl",
    }
    _write_jsonl(paths["development_cases.jsonl"], development_cases)
    _write_jsonl(paths["development_labels.jsonl"], development_labels)
    _write_jsonl(paths["validation_cases.jsonl"], validation_cases)
    _write_jsonl(paths["validation_labels.jsonl"], validation_labels)
    cases = tuple(
        Phase16QualificationCase.model_validate(record)
        for record in (*development_cases, *validation_cases)
    )
    _round_trip_validate_corpus(cases=cases)
    public_case_ids = {
        "development": tuple(case.case_id for case in cases if case.split is QualificationSplit.DEVELOPMENT),
        "validation": tuple(case.case_id for case in cases if case.split is QualificationSplit.VALIDATION),
    }
    manifest = Phase16QualificationManifest(
        seed=PHASE16_QUALIFICATION_SEED,
        policy_digest=resolved_policy.policy_digest or "",
        public_split_counts={"development": 18, "validation": 18},
        public_case_ids=public_case_ids,
        public_case_digests={
            case.case_id: _sha256(_canonical_bytes(case.model_dump(mode="json")))
            for case in cases
        },
        artifact_digests={name: _file_digest(path) for name, path in paths.items()},
        source_file_digests=qualification_source_file_digests(repository_root=repository_root),
        generator_digest=_source_digest(repository_root, str(PHASE16_QUALIFICATION_GENERATOR_PATH)),
        previously_exposed_case_ids=PHASE16_V8_PREVIOUSLY_EXPOSED_CASE_IDS,
        holdout_case_count=30,
        holdout_high_conflict_e2e_case_count=30,
    )
    manifest = manifest.model_copy(
        update={"manifest_digest": canonical_json_sha256(manifest.model_dump(mode="json", exclude={"manifest_digest"}))}
    )
    (root / "manifest.json").write_bytes(_canonical_bytes(manifest.model_dump(mode="json")) + b"\n")
    return manifest


def load_phase16_qualification_corpus(
    output_root: Path,
    *,
    repository_root: Path,
    policy: Phase16QualificationPolicy | None = None,
) -> Phase16QualificationCorpus:
    """只加载公开 corpus；尚未独立 release 的 holdout 绝不由此接口返回。"""

    root = Path(output_root)
    manifest_path = root / "manifest.json"
    raw_manifest = manifest_path.read_bytes()
    if raw_manifest.startswith(b"\xef\xbb\xbf") or b"\r" in raw_manifest:
        raise ValueError("qualification manifest must be UTF-8 LF without BOM")
    try:
        manifest = Phase16QualificationManifest.model_validate(json.loads(raw_manifest.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("qualification manifest is invalid JSON") from exc
    resolved_policy = policy or load_phase16_qualification_policy(repository_root=repository_root)
    if manifest.policy_digest != resolved_policy.policy_digest:
        raise ValueError("qualification corpus policy digest does not match frozen policy")
    if manifest.source_file_digests != qualification_source_file_digests(repository_root=repository_root):
        raise ValueError("qualification corpus source closure does not match current implementation")
    paths = {
        "development_cases.jsonl": root / "development_cases.jsonl",
        "development_labels.jsonl": root / "development_labels.jsonl",
        "validation_cases.jsonl": root / "validation_cases.jsonl",
        "validation_labels.jsonl": root / "validation_labels.jsonl",
    }
    if {name: _file_digest(path) for name, path in paths.items()} != manifest.artifact_digests:
        raise ValueError("qualification corpus artifact digest mismatch")
    development_cases = tuple(
        Phase16QualificationCase.model_validate(record)
        for record in _read_jsonl(paths["development_cases.jsonl"])
    )
    validation_cases = tuple(
        Phase16QualificationCase.model_validate(record)
        for record in _read_jsonl(paths["validation_cases.jsonl"])
    )
    labels = {
        label.case_id: label
        for label in (
            Phase16QualificationLabel.model_validate(record)
            for record in (
                *_read_jsonl(paths["development_labels.jsonl"]),
                *_read_jsonl(paths["validation_labels.jsonl"]),
            )
        )
    }
    cases = (*development_cases, *validation_cases)
    case_ids = tuple(case.case_id for case in cases)
    if len(case_ids) != 36 or len(set(case_ids)) != 36 or set(case_ids) != set(labels):
        raise ValueError("qualification case and label identities must match exactly")
    split_ids = {
        "development": tuple(case.case_id for case in development_cases),
        "validation": tuple(case.case_id for case in validation_cases),
    }
    if split_ids != manifest.public_case_ids:
        raise ValueError("qualification split IDs do not match manifest")
    expected_case_digests = {
        case.case_id: _sha256(_canonical_bytes(case.model_dump(mode="json")))
        for case in cases
    }
    if expected_case_digests != manifest.public_case_digests:
        raise ValueError("qualification case digests do not match manifest")
    if manifest.holdout_release_state is not HoldoutReleaseState.PENDING_INDEPENDENT_COMMITMENT:
        raise ValueError("released holdout must be loaded through the independent release verifier")
    return Phase16QualificationCorpus(
        development_cases=development_cases,
        validation_cases=validation_cases,
        labels=MappingProxyType(labels),
        manifest=manifest,
    )


def classify_v8_reason_code(reason_code: str) -> str:
    """把历史 V8 细码归入封闭 assessment taxonomy，绝不覆盖原 reason code。"""

    if not isinstance(reason_code, str) or not re.fullmatch(_REASON_CODE_PATTERN, reason_code):
        raise ValueError("qualification source reason code is invalid")
    if reason_code.endswith("PLANNER_RISK_COVERAGE"):
        return "PLANNER_RISK_COVERAGE"
    if reason_code.endswith("EXPLANATION_MAX_LENGTH"):
        return "ANALYST_EXPLANATION_BOUND"
    if "RESULT_SCHEMA_INVALID" in reason_code:
        return "MODEL_RESULT_SCHEMA"
    if "EVIDENCE" in reason_code or "LINEAGE" in reason_code:
        return "EVIDENCE_OR_LINEAGE"
    if "RECEIPT" in reason_code or "USAGE" in reason_code or "MODEL_OUTCOME" in reason_code:
        return "RECEIPT_OR_USAGE"
    if "BUDGET" in reason_code or "DEADLINE" in reason_code:
        return "BUDGET_OR_DEADLINE"
    return "OTHER_CONTROLLED_FAILURE"


def required_planner_risk_codes() -> frozenset[str]:
    """暴露当前闭合风险枚举，供 future candidate checklist 测试而非模型 prompt 拼接。"""

    return frozenset(item.value for item in ConflictRiskCode)
