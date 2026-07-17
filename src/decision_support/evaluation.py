"""Phase 14 Task 10 的固定数据集与人机协同离线评估内核。

本模块只处理脱敏、确定性和可重放的评估事实：它不调用模型、不访问生产 Store，
也不把离线结果转换为经营命令。这样 Task 11 才能在独立预检完成后接入真实 smoke，
而 Task 12 可以复用同一份数据和指标生成业务附录。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import random
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from src.specialist_runtime.models import (
    _freeze_json,
    _plain_json,
    canonical_json_sha256,
)


HASH_PATTERN = r"^[0-9a-f]{64}$"
_SENSITIVE_KEYS = frozenset(
    {"free_text", "raw_text", "chain_of_thought", "prompt", "secret", "token", "embedding"}
)


class HumanSupportScenario(StrEnum):
    """Task 10 固定的四类复合事故场景组。"""

    SOLD_OUT_BACKUP_CONFLICT = "SOLD_OUT_BACKUP_CONFLICT"
    DANMAKU_NOISE = "DANMAKU_NOISE"
    PACE_SHIFT = "PACE_SHIFT"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"


class DecisionCondition(StrEnum):
    """同一场景的无 Copilot 基线和人机协同条件。"""

    BASELINE = "BASELINE"
    DECISION_SUPPORT = "DECISION_SUPPORT"


class DecisionAction(StrEnum):
    """离线评估允许的结构化决定，不包含自由执行指令。"""

    WAIT_OPERATOR_BACKUP = "WAIT_OPERATOR_BACKUP"
    IGNORE_DANMAKU_NOISE = "IGNORE_DANMAKU_NOISE"
    WAIT_OPERATOR_TIMING = "WAIT_OPERATOR_TIMING"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"


class HumanSupportCase(BaseModel):
    """一个固定的脱敏复合事故 case 和人工可判定的参考标签。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(..., pattern=r"^phase14-human-support-[a-z_]+-[0-9]{2}$")
    scenario_group: HumanSupportScenario
    comparison_slot: int = Field(..., ge=1, le=4, strict=True)
    facts: Any
    expected_action: DecisionAction
    key_conflict: bool
    evidence_expired: bool
    cas_version_conflict: bool
    unknown_side_effect: bool

    @field_validator("facts", mode="after")
    @classmethod
    def _freeze_and_scrub_facts(cls, value: Any) -> Any:
        """冻结 JSON 并递归拒绝自由文本、秘密和模型内部推理字段。"""

        plain = _plain_json(value)
        _assert_no_sensitive_fields(plain)
        return _freeze_json(plain)

    @field_serializer("facts", when_used="json")
    def _serialize_facts(self, value: Any) -> Any:
        """把内部 FrozenDict 转回普通 JSON，保证文件与 API 序列化一致。"""

        return _plain_json(value)


class HumanSupportManifest(BaseModel):
    """绑定数据、生成器和分组身份的不可变本地 Manifest。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = "phase14-human-support-v1"
    manifest_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    seed: int = Field(..., ge=0, strict=True)
    dataset_digest: str = Field(..., pattern=HASH_PATTERN)
    schema_digest: str = Field(..., pattern=HASH_PATTERN)
    generator_digest: str = Field(..., pattern=HASH_PATTERN)
    case_ids: tuple[str, ...] = Field(..., min_length=1)
    group_case_ids: Any
    manifest_digest: str = ""

    @field_validator("group_case_ids", mode="after")
    @classmethod
    def _freeze_groups(cls, value: Any) -> Any:
        """冻结分组映射，避免评估运行时重新排列或替换 case。"""

        return _freeze_json(_plain_json(value))

    @field_serializer("group_case_ids", when_used="json")
    def _serialize_groups(self, value: Any) -> Any:
        """把冻结的分组映射转为稳定 JSON 对象。"""

        return _plain_json(value)

    @model_validator(mode="after")
    def _verify_manifest(self) -> "HumanSupportManifest":
        """校验四组完整覆盖，并为 Manifest 事实计算稳定摘要。"""

        groups = _plain_json(self.group_case_ids)
        expected_groups = {group.value for group in HumanSupportScenario}
        if set(groups) != expected_groups:
            raise ValueError("manifest must contain exactly four scenario groups")
        if len(self.case_ids) != 16 or len(set(self.case_ids)) != 16:
            raise ValueError("phase14 manifest must contain 16 unique cases")
        flattened = [case_id for values in groups.values() for case_id in values]
        if flattened != list(self.case_ids) or len(flattened) != 16:
            raise ValueError("manifest groups must cover case_ids in stable order")
        if any(len(values) != 4 for values in groups.values()):
            raise ValueError("every scenario group must contain four cases")
        payload = self.model_dump(mode="json", exclude={"manifest_digest"})
        calculated = canonical_json_sha256(payload)
        if self.manifest_digest and self.manifest_digest != calculated:
            raise ValueError("manifest_digest does not match manifest facts")
        object.__setattr__(self, "manifest_digest", calculated)
        return self


class Phase14Dataset(BaseModel):
    """评估执行时使用的 case 快照和对应 Manifest。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cases: tuple[HumanSupportCase, ...]
    manifest: HumanSupportManifest

    @model_validator(mode="after")
    def _bind_manifest_to_cases(self) -> "Phase14Dataset":
        """重新计算 cases 身份和字节摘要，拒绝用篡改样本复用旧 Manifest。"""

        case_ids = tuple(case.case_id for case in self.cases)
        if case_ids != self.manifest.case_ids:
            raise ValueError("dataset cases do not match manifest case_ids")
        groups = _plain_json(self.manifest.group_case_ids)
        for group, expected_ids in groups.items():
            actual_ids = tuple(case.case_id for case in self.cases if case.scenario_group.value == group)
            if actual_ids != tuple(expected_ids):
                raise ValueError("dataset cases do not match manifest scenario groups")
        if sha256(_case_bytes(self.cases)).hexdigest() != self.manifest.dataset_digest:
            raise ValueError("dataset cases do not match manifest dataset_digest")
        return self

    def case_by_id(self, case_id: str) -> HumanSupportCase:
        """按冻结身份读取 case，未知身份直接拒绝而不是构造默认事实。"""

        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise ValueError(f"case not found: {case_id}")


class CrossoverAssignment(BaseModel):
    """一个人工交叉实验单元，稳定绑定运营员、场景、条件和 case。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assignment_id: str = Field(..., min_length=1)
    operator_id: str = Field(..., min_length=1)
    scenario_group: HumanSupportScenario
    case_id: str = Field(..., min_length=1)
    condition: DecisionCondition
    sequence: int = Field(..., ge=1, strict=True)


class HumanDecisionRecord(BaseModel):
    """运营员对一个分配单元的结构化结果，不保存自由文本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assignment_id: str = Field(..., min_length=1)
    operator_id: str = Field(..., min_length=1)
    scenario_group: HumanSupportScenario
    case_id: str = Field(..., min_length=1)
    condition: DecisionCondition
    action: DecisionAction
    conflict_detected: bool
    severe_violation: bool
    latency_ms: Decimal = Field(..., ge=0, le=Decimal("999999999.999"))
    workload_score: int = Field(..., ge=1, le=7, strict=True)

    @field_validator("latency_ms", mode="after")
    @classmethod
    def _latency_precision(cls, value: Decimal) -> Decimal:
        """与数据库毫秒精度保持一致，避免隐式浮点四舍五入。"""

        if value != value.quantize(Decimal("0.001")):
            raise ValueError("latency_ms exceeds three decimal places")
        return value


class OfflineEvaluationSummary(BaseModel):
    """规则优先的离线结果；严格 AND 门槛由这里统一重算。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_count: int = Field(..., ge=1, strict=True)
    operator_count: int = Field(default=0, ge=0, strict=True)
    total_decisions: int = Field(default=0, ge=0, strict=True)
    severe_violation_count: int = Field(..., ge=0, strict=True)
    baseline_safety_correctness: Decimal = Field(..., ge=0, le=1)
    copilot_safety_correctness: Decimal = Field(..., ge=0, le=1)
    baseline_key_conflict_miss_rate: Decimal = Field(..., ge=0, le=1)
    copilot_key_conflict_miss_rate: Decimal = Field(..., ge=0, le=1)
    key_conflict_miss_rate_reduction: Decimal
    baseline_decision_median_ms: Decimal = Field(..., ge=0)
    copilot_decision_median_ms: Decimal = Field(..., ge=0)
    decision_median_reduction: Decimal
    baseline_workload_median: Decimal | None = Field(default=None, ge=1, le=7)
    copilot_workload_median: Decimal | None = Field(default=None, ge=1, le=7)
    workload_median_reduction: Decimal | None = None
    meets_acceptance_gate: bool
    is_usability_evidence: bool = False
    production_ab: bool = False


class _ScriptedDecision(BaseModel):
    """内部确定性响应，不暴露给生产 Runtime。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: DecisionAction
    conflict_detected: bool
    severe_violation: bool
    latency_ms: Decimal


_EXPECTED_ACTIONS: dict[HumanSupportScenario, DecisionAction] = {
    HumanSupportScenario.SOLD_OUT_BACKUP_CONFLICT: DecisionAction.WAIT_OPERATOR_BACKUP,
    HumanSupportScenario.DANMAKU_NOISE: DecisionAction.IGNORE_DANMAKU_NOISE,
    HumanSupportScenario.PACE_SHIFT: DecisionAction.WAIT_OPERATOR_TIMING,
    HumanSupportScenario.EVIDENCE_CONFLICT: DecisionAction.WAIT_RECONCILIATION,
}

_BASELINE_ACTIONS: dict[HumanSupportScenario, DecisionAction] = {
    HumanSupportScenario.SOLD_OUT_BACKUP_CONFLICT: DecisionAction.WAIT_OPERATOR_TIMING,
    HumanSupportScenario.DANMAKU_NOISE: DecisionAction.IGNORE_DANMAKU_NOISE,
    HumanSupportScenario.PACE_SHIFT: DecisionAction.WAIT_OPERATOR_TIMING,
    HumanSupportScenario.EVIDENCE_CONFLICT: DecisionAction.WAIT_OPERATOR_TIMING,
}

_SCHEMA_DESCRIPTOR = {
    "case": {
        "fields": (
            "case_id",
            "scenario_group",
            "comparison_slot",
            "facts",
            "expected_action",
            "key_conflict",
            "evidence_expired",
            "cas_version_conflict",
            "unknown_side_effect",
        ),
        "facts_fields": (
            "event",
            "inventory_version",
            "backup_conflict",
            "danmaku_noise_level",
            "pace_signal",
            "evidence_state",
            "version_state",
            "side_effect_state",
            "template_slot",
        ),
    },
    "assignment": ("assignment_id", "operator_id", "scenario_group", "case_id", "condition", "sequence"),
    "record": (
        "assignment_id",
        "operator_id",
        "scenario_group",
        "case_id",
        "condition",
        "action",
        "conflict_detected",
        "severe_violation",
        "latency_ms",
        "workload_score",
    ),
}


def build_phase14_dataset(*, seed: int) -> Phase14Dataset:
    """生成固定四组、每组四例的脱敏复合事故数据集。"""

    if seed < 0:
        raise ValueError("seed must be non-negative")
    cases: list[HumanSupportCase] = []
    group_case_ids: dict[str, list[str]] = {}
    for group in HumanSupportScenario:
        ids: list[str] = []
        for slot in range(1, 5):
            case_id = f"phase14-human-support-{group.value.lower()}-{slot:02d}"
            evidence_expired = slot == 2
            cas_version_conflict = slot == 3
            unknown_side_effect = slot == 4
            expected_action = (
                DecisionAction.WAIT_RECONCILIATION
                if evidence_expired or cas_version_conflict or unknown_side_effect
                else _EXPECTED_ACTIONS[group]
            )
            case = HumanSupportCase(
                case_id=case_id,
                scenario_group=group,
                comparison_slot=slot,
                facts={
                    "event": "trusted_sold_out",
                    "inventory_version": 100 + slot,
                    "backup_conflict": group is HumanSupportScenario.SOLD_OUT_BACKUP_CONFLICT,
                    "danmaku_noise_level": "high" if group is HumanSupportScenario.DANMAKU_NOISE else "low",
                    "pace_signal": "unstable" if group is HumanSupportScenario.PACE_SHIFT else "stable",
                    "evidence_state": (
                        "expired"
                        if evidence_expired
                        else "conflict" if group is HumanSupportScenario.EVIDENCE_CONFLICT else "fresh"
                    ),
                    "version_state": "conflict" if cas_version_conflict else "stable",
                    "side_effect_state": "unknown" if unknown_side_effect else "known",
                    "template_slot": slot,
                },
                expected_action=expected_action,
                key_conflict=group
                in {
                    HumanSupportScenario.SOLD_OUT_BACKUP_CONFLICT,
                    HumanSupportScenario.EVIDENCE_CONFLICT,
                }
                or cas_version_conflict,
                evidence_expired=evidence_expired,
                cas_version_conflict=cas_version_conflict,
                unknown_side_effect=unknown_side_effect,
            )
            cases.append(case)
            ids.append(case_id)
        group_case_ids[group.value] = ids
    case_bytes = _case_bytes(tuple(cases))
    manifest = HumanSupportManifest(
        seed=seed,
        dataset_digest=sha256(case_bytes).hexdigest(),
        schema_digest=canonical_json_sha256(_SCHEMA_DESCRIPTOR),
        generator_digest=sha256(Path(__file__).read_bytes()).hexdigest(),
        case_ids=tuple(case.case_id for case in cases),
        group_case_ids=group_case_ids,
    )
    return Phase14Dataset(cases=tuple(cases), manifest=manifest)


def write_phase14_dataset(root: Path, *, seed: int) -> Phase14Dataset:
    """把数据和 Manifest 以 UTF-8、LF、稳定 JSON 写入指定评估目录。"""

    dataset = build_phase14_dataset(seed=seed)
    root.mkdir(parents=True, exist_ok=True)
    (root / "cases.jsonl").write_bytes(_case_bytes(dataset.cases))
    manifest_payload = json.dumps(
        dataset.manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (root / "manifest.json").write_bytes(manifest_payload + b"\n")
    return dataset


def run_scripted_evaluation(dataset: Phase14Dataset) -> OfflineEvaluationSummary:
    """运行无网络 ScriptedModel，并按 case 配对重算自动化指标。"""

    dataset = _validated_dataset(dataset)
    if not dataset.cases:
        raise ValueError("scripted evaluation requires cases")
    pairs: list[tuple[HumanSupportCase, _ScriptedDecision, _ScriptedDecision]] = []
    for case in dataset.cases:
        baseline = _ScriptedDecision(
            action=_BASELINE_ACTIONS[case.scenario_group],
            conflict_detected=False,
            severe_violation=False,
            latency_ms=Decimal("1200"),
        )
        copilot = _ScriptedDecision(
            action=case.expected_action,
            conflict_detected=case.key_conflict,
            severe_violation=False,
            latency_ms=Decimal("800"),
        )
        pairs.append((case, baseline, copilot))
    return _summarize_pairs(pairs, operator_count=0, total_decisions=0, usability=False)


def build_crossover_assignments(
    operator_ids: Sequence[str],
    group_case_ids: Mapping[str, str],
    *,
    seed: int,
) -> tuple[CrossoverAssignment, ...]:
    """为 3-5 名代理运营生成四组双条件的确定性随机交叉顺序。"""

    if not 3 <= len(operator_ids) <= 5 or len(set(operator_ids)) != len(operator_ids):
        raise ValueError("operator count must be 3..5 and unique")
    expected_groups = {group.value for group in HumanSupportScenario}
    if set(group_case_ids) != expected_groups:
        raise ValueError("case group mapping must contain exactly the four scenario groups")
    for group in HumanSupportScenario:
        prefix = f"phase14-human-support-{group.value.lower()}-"
        case_id = group_case_ids[group.value]
        if not case_id.startswith(prefix) or len(case_id) != len(prefix) + 2:
            raise ValueError("case IDs must match their frozen scenario group")
    entries: list[tuple[HumanSupportScenario, str, DecisionCondition]] = []
    for group in HumanSupportScenario:
        for condition in DecisionCondition:
            entries.append((group, group_case_ids[group.value], condition))
    assignments: list[CrossoverAssignment] = []
    for operator_id in operator_ids:
        if not operator_id.strip():
            raise ValueError("operator_id must not be empty")
        shuffled = list(entries)
        # 不依赖 Python 字符串 hash 随进程变化，使用 SHA-256 派生跨进程稳定的伪随机种子。
        stable_seed = int(sha256(f"{seed}:{operator_id}".encode("utf-8")).hexdigest(), 16)
        random.Random(stable_seed).shuffle(shuffled)
        for sequence, (group, case_id, condition) in enumerate(shuffled, start=1):
            assignments.append(
                CrossoverAssignment(
                    assignment_id=f"phase14-crossover-{operator_id}-{sequence:03d}",
                    operator_id=operator_id,
                    scenario_group=group,
                    case_id=case_id,
                    condition=condition,
                    sequence=sequence,
                )
            )
    return tuple(assignments)


def evaluate_human_crossover(
    dataset: Phase14Dataset,
    assignments: Sequence[CrossoverAssignment],
    records: Sequence[HumanDecisionRecord],
) -> OfflineEvaluationSummary:
    """校验完整交叉样本，并将其标记为可用性证据而非生产 A/B。"""

    dataset = _validated_dataset(dataset)
    cases = dataset.cases
    if len(assignments) != len(records):
        raise ValueError("every assignment must have exactly one decision record")
    if not 24 <= len(records) <= 40:
        raise ValueError("human crossover must contain 24..40 decisions")
    case_by_id = {case.case_id: case for case in cases}
    assignment_by_id = {item.assignment_id: item for item in assignments}
    record_ids = [record.assignment_id for record in records]
    if len(record_ids) != len(set(record_ids)) or set(record_ids) != set(assignment_by_id):
        raise ValueError("decision records must match assignments exactly")
    operators = {item.operator_id for item in assignments}
    if not 3 <= len(operators) <= 5:
        raise ValueError("operator count must be 3..5")
    pairs: dict[tuple[str, HumanSupportScenario], dict[DecisionCondition, tuple[HumanSupportCase, HumanDecisionRecord]]] = {}
    for record in records:
        assignment = assignment_by_id.get(record.assignment_id)
        case = case_by_id.get(record.case_id)
        if assignment is None or case is None:
            raise ValueError("record references unknown assignment or case")
        if (
            record.operator_id != assignment.operator_id
            or record.scenario_group is not assignment.scenario_group
            or record.case_id != assignment.case_id
            or record.condition is not assignment.condition
            or case.scenario_group is not assignment.scenario_group
        ):
            raise ValueError("record identity does not match assignment")
        key = (record.operator_id, record.scenario_group)
        by_condition = pairs.setdefault(key, {})
        if record.condition in by_condition:
            raise ValueError("each assignment pair must be unique")
        by_condition[record.condition] = (case, record)
    if any(set(values) != set(DecisionCondition) for values in pairs.values()) or len(pairs) != len(operators) * 4:
        raise ValueError("each operator must have both conditions for all four groups")
    paired_rows = []
    for values in pairs.values():
        baseline_case, baseline_record = values[DecisionCondition.BASELINE]
        copilot_case, copilot_record = values[DecisionCondition.DECISION_SUPPORT]
        if copilot_case.case_id != baseline_case.case_id:
            raise ValueError("baseline and decision-support must use the same case")
        paired_rows.append((baseline_case, baseline_record, copilot_record))
    scripted_pairs = [
        (
            case,
            _ScriptedDecision(
                action=baseline.action,
                conflict_detected=baseline.conflict_detected,
                severe_violation=baseline.severe_violation,
                latency_ms=baseline.latency_ms,
            ),
            _ScriptedDecision(
                action=copilot.action,
                conflict_detected=copilot.conflict_detected,
                severe_violation=copilot.severe_violation,
                latency_ms=copilot.latency_ms,
            ),
        )
        for case, baseline, copilot in paired_rows
    ]
    return _summarize_pairs(
        scripted_pairs,
        operator_count=len(operators),
        total_decisions=len(records),
        usability=True,
        workload_pairs=tuple(
            (baseline.workload_score, copilot.workload_score)
            for _case, baseline, copilot in paired_rows
        ),
    )


def _summarize_pairs(
    pairs: Sequence[tuple[HumanSupportCase, _ScriptedDecision, _ScriptedDecision]],
    *,
    operator_count: int,
    total_decisions: int,
    usability: bool,
    workload_pairs: Sequence[tuple[int, int]] | None = None,
) -> OfflineEvaluationSummary:
    """从不可变 pair 事实计算规则优先指标和严格 AND 门槛。"""

    sample_count = len(pairs)
    conflict_count = sum(case.key_conflict for case, _baseline, _copilot in pairs)
    baseline_correct = sum(
        _is_safe_correct(case, baseline) for case, baseline, _copilot in pairs
    )
    copilot_correct = sum(
        _is_safe_correct(case, copilot) for case, _baseline, copilot in pairs
    )
    baseline_misses = sum(
        case.key_conflict and not baseline.conflict_detected
        for case, baseline, _copilot in pairs
    )
    copilot_misses = sum(
        case.key_conflict and not copilot.conflict_detected
        for case, _baseline, copilot in pairs
    )
    baseline_rate = _rate(baseline_correct, sample_count)
    copilot_rate = _rate(copilot_correct, sample_count)
    baseline_miss_rate = _rate(baseline_misses, conflict_count)
    copilot_miss_rate = _rate(copilot_misses, conflict_count)
    reduction = _relative_reduction(baseline_miss_rate, copilot_miss_rate)
    baseline_median = _median(tuple(baseline.latency_ms for _case, baseline, _copilot in pairs))
    copilot_median = _median(tuple(copilot.latency_ms for _case, _baseline, copilot in pairs))
    latency_reduction = _relative_reduction(baseline_median, copilot_median)
    severe_count = sum(
        int(baseline.severe_violation) + int(copilot.severe_violation)
        for _case, baseline, copilot in pairs
    )
    meets = (
        severe_count == 0
        and copilot_rate >= Decimal("0.90")
        and reduction >= Decimal("0.30")
        and latency_reduction >= Decimal("0.20")
    )
    baseline_workload = None
    copilot_workload = None
    workload_reduction = None
    if workload_pairs:
        baseline_workload = _median(tuple(Decimal(item[0]) for item in workload_pairs))
        copilot_workload = _median(tuple(Decimal(item[1]) for item in workload_pairs))
        workload_reduction = _relative_reduction(baseline_workload, copilot_workload)
    return OfflineEvaluationSummary(
        case_count=sample_count,
        operator_count=operator_count,
        total_decisions=total_decisions,
        severe_violation_count=severe_count,
        baseline_safety_correctness=baseline_rate,
        copilot_safety_correctness=copilot_rate,
        baseline_key_conflict_miss_rate=baseline_miss_rate,
        copilot_key_conflict_miss_rate=copilot_miss_rate,
        key_conflict_miss_rate_reduction=reduction,
        baseline_decision_median_ms=baseline_median,
        copilot_decision_median_ms=copilot_median,
        decision_median_reduction=latency_reduction,
        baseline_workload_median=baseline_workload,
        copilot_workload_median=copilot_workload,
        workload_median_reduction=workload_reduction,
        meets_acceptance_gate=meets,
        is_usability_evidence=usability,
        production_ab=False,
    )


def _case_bytes(cases: Sequence[HumanSupportCase]) -> bytes:
    """按固定顺序输出 JSONL，避免平台换行和字典顺序影响数据摘要。"""

    return b"".join(
        (
            json.dumps(
                case.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        for case in cases
    )


def _validated_dataset(dataset: Phase14Dataset) -> Phase14Dataset:
    """对外部传入的 Dataset 重新走模型校验，阻断 model_construct 绕过身份绑定。"""

    if not isinstance(dataset, Phase14Dataset):
        raise TypeError("evaluation requires Phase14Dataset")
    return Phase14Dataset.model_validate(dataset.model_dump(mode="json"))


def _assert_no_sensitive_fields(value: Any) -> None:
    """递归阻断自由文本和秘密字段进入冻结评估输入。"""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                raise ValueError(f"sensitive field is not allowed: {key}")
            _assert_no_sensitive_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_sensitive_fields(item)


def _is_safe_correct(case: HumanSupportCase, decision: _ScriptedDecision) -> bool:
    """安全正确要求无严重违规且结构化动作命中人工冻结标签。"""

    return not decision.severe_violation and decision.action is case.expected_action


def _rate(numerator: int, denominator: int) -> Decimal:
    """用 Decimal 计算可审计比例，空冲突集合按 0 处理。"""

    if denominator == 0:
        return Decimal("0")
    # 门槛比较必须使用未四舍五入的真实比例；展示层如需短格式应另行格式化。
    return Decimal(numerator) / Decimal(denominator)


def _relative_reduction(before: Decimal, after: Decimal) -> Decimal:
    """计算“下降比例”；基线为零时不制造虚假的改善证据。"""

    if before == 0:
        return Decimal("0")
    # 严格 AND 门槛不能把 19.99% 四舍五入成 20.0% 后误判为通过。
    return (before - after) / before


def _median(values: Sequence[Decimal]) -> Decimal:
    """以 Decimal 计算稳定中位数，避免 statistics 混入二进制浮点。"""

    if not values:
        raise ValueError("median requires values")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle].quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return ((ordered[middle - 1] + ordered[middle]) / Decimal("2")).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )
