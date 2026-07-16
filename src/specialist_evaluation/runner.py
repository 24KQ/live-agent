"""Phase 13 正式评估的候选去留规则内核。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from src.specialist_evaluation.manifest_authorization import calculate_source_code_digest
from src.specialist_evaluation.models import (
    CaseAttempt,
    EvaluationCandidate,
    EvaluationManifest,
    EvaluationManifestKind,
    EvaluationRun,
    EvaluationRunClaim,
    EvaluationSplit,
    EvaluationSubject,
    PairedMetric,
    RetentionDecision,
    RetentionDecisionRecord,
)
from src.specialist_evaluation.comparison import BinaryPair, aggregate_binary_pairs
from src.specialist_evaluation.store import EvaluationInvariantError
from src.specialist_runtime.models import _plain_json


@dataclass(frozen=True)
class CandidateGateFacts:
    """正式评估结束后可由持久化 Attempt/Metric 重建的候选事实。"""

    candidate: EvaluationCandidate
    validation_cases: int
    holdout_cases: int
    severe_violation_count: int
    external_evidence_sufficient: bool
    # value 固定为 (agent absolute rate, relative percentage-point delta)，避免调用方传入
    # 已经汇总的主观布尔值；阈值始终由本模块依据候选身份决定。
    metrics: Mapping[str, tuple[Decimal, Decimal]]

    def __post_init__(self) -> None:
        if self.validation_cases < 0 or self.holdout_cases < 0 or self.severe_violation_count < 0:
            raise ValueError("candidate gate counts cannot be negative")
        normalized: dict[str, tuple[Decimal, Decimal]] = {}
        for metric_id, (rate, delta) in self.metrics.items():
            rate_value = Decimal(rate)
            delta_value = Decimal(delta)
            if not rate_value.is_finite() or not delta_value.is_finite() or not Decimal("0") <= rate_value <= Decimal("1"):
                raise ValueError("candidate gate metrics must be finite")
            normalized[metric_id] = (rate_value, delta_value)
        object.__setattr__(self, "metrics", MappingProxyType(normalized))


@dataclass(frozen=True)
class CandidateRetentionOutcome:
    """候选当前唯一裁决，或 validation 通过后等待 holdout 的中间状态。"""

    decision: RetentionDecision | None
    reason_code: str


@dataclass(frozen=True)
class RealModelPreflight:
    """真实模型调用前的本地可审计裁决，不包含或回显密钥内容。"""

    allowed: bool
    reason_code: str


@dataclass(frozen=True)
class PreflightOnlyEvaluationReport:
    """未满足真实模型门禁时的正式评估前置结论，不含任何模型生成结果。"""

    real_model_preflight: RealModelPreflight
    outcomes: Mapping[EvaluationCandidate, CandidateRetentionOutcome]


@dataclass(frozen=True)
class CandidateEvaluationSlice:
    """正式协调器消费的候选纵向切片，不让它了解 Agent 领域输出结构。"""

    candidate: EvaluationCandidate
    metric_ids: tuple[str, ...]
    cases_for: Callable[[EvaluationSplit], tuple[dict[str, Any], ...]]
    run_agent_case: Callable[[dict[str, Any]], Awaitable[Any]]
    baseline_for_case: Callable[[dict[str, Any]], Any]
    record_pair: Callable[..., None]
    rebuild_validation_gate: Callable[..., Any]
    extra_gate_metrics: Callable[..., Mapping[str, tuple[Decimal, Decimal]]]

    @classmethod
    def from_object(cls, value: Any) -> "CandidateEvaluationSlice":
        """从受控内部 slice 复制全部 callable，拒绝运行时动态补充字段。"""

        required = (
            "candidate",
            "metric_ids",
            "cases_for",
            "run_agent_case",
            "baseline_for_case",
            "record_pair",
            "rebuild_validation_gate",
            "extra_gate_metrics",
        )
        if any(not hasattr(value, field) for field in required):
            raise ValueError("candidate evaluation slice is incomplete")
        candidate = value.candidate
        metric_ids = tuple(value.metric_ids)
        if (
            not isinstance(candidate, EvaluationCandidate)
            or not metric_ids
            or len(metric_ids) != len(set(metric_ids))
            or any(not isinstance(metric_id, str) or not metric_id for metric_id in metric_ids)
            or any(not callable(getattr(value, field)) for field in required[2:])
        ):
            raise ValueError("candidate evaluation slice is invalid")
        return cls(
            candidate=candidate,
            metric_ids=metric_ids,
            cases_for=value.cases_for,
            run_agent_case=value.run_agent_case,
            baseline_for_case=value.baseline_for_case,
            record_pair=value.record_pair,
            rebuild_validation_gate=value.rebuild_validation_gate,
            extra_gate_metrics=value.extra_gate_metrics,
        )


@dataclass(frozen=True)
class FormalCandidateEvaluationReport:
    """一次候选正式演练的持久结论与两个 split 的只读指标快照。"""

    decision: RetentionDecisionRecord
    validation_metrics: tuple[PairedMetric, ...]
    holdout_metrics: tuple[PairedMetric, ...]


class FormalEvaluationCoordinator:
    """协调固定 validation/holdout 流程，并把 Store 作为所有去留事实的唯一来源。"""

    _SHARD_SIZE = 10

    def __init__(self, *, store: Any) -> None:
        self._store = store

    async def evaluate_candidate(
        self,
        *,
        run: EvaluationRun,
        claim: EvaluationRunClaim,
        slice_: CandidateEvaluationSlice,
    ) -> FormalCandidateEvaluationReport:
        """先跑四个 validation shard；只有全部解锁后才允许一次 holdout。"""

        if run.candidate is not slice_.candidate:
            raise ValueError("formal slice candidate does not match evaluation run")
        validation_cases = slice_.cases_for(EvaluationSplit.VALIDATION)
        holdout_cases = slice_.cases_for(EvaluationSplit.HOLDOUT)
        if len(validation_cases) != 40 or len(holdout_cases) != 20:
            raise ValueError("formal candidate slice requires 40 validation and 20 holdout cases")

        for offset in range(0, len(validation_cases), self._SHARD_SIZE):
            for case in validation_cases[offset : offset + self._SHARD_SIZE]:
                await self._record_case(run=run, claim=claim, slice_=slice_, case=case)
            gate = slice_.rebuild_validation_gate(run=run)
            if self._gate_status(gate) == "REJECTED":
                return self._persist_decision(
                    run=run,
                    claim=claim,
                    outcome=CandidateRetentionOutcome(
                        RetentionDecision.REJECTED,
                        self._gate_reason(gate),
                    ),
                    validation_metrics=(),
                    holdout_metrics=(),
                )

        if self._gate_status(slice_.rebuild_validation_gate(run=run)) != "HOLDOUT_UNLOCKED":
            raise EvaluationInvariantError("validation did not unlock holdout")
        validation_metrics = self._save_split_metrics(
            run=run,
            claim=claim,
            split=EvaluationSplit.VALIDATION,
            metric_ids=slice_.metric_ids,
        )

        # 同一 Run 只能在 validation 解锁后消费这一批固定的 20 个 holdout case；
        # 这里没有第二个模型或 baseline 分支，因此重放由 selected result fail-closed。
        for case in holdout_cases:
            await self._record_case(run=run, claim=claim, slice_=slice_, case=case)
        holdout_metrics = self._save_split_metrics(
            run=run,
            claim=claim,
            split=EvaluationSplit.HOLDOUT,
            metric_ids=slice_.metric_ids,
        )
        metric_facts = {
            metric.metric_id: (metric.agent_rate, metric.delta_percentage_points)
            for metric in holdout_metrics
        }
        # ReviewMemory 的 macro-F1 不是逐 case 二元准确率；slice 必须从冻结 evaluator
        # label 与 selected 输出重建该值，再以统一的 (rate, pp delta) 形式输入 AND 门。
        metric_facts.update(
            slice_.extra_gate_metrics(run=run, split=EvaluationSplit.HOLDOUT)
        )
        summary = self._selected_summary(run)
        outcome = decide_candidate_retention(
            CandidateGateFacts(
                candidate=run.candidate,
                validation_cases=summary[2],
                holdout_cases=summary[3],
                severe_violation_count=summary[0],
                external_evidence_sufficient=True,
                metrics=metric_facts,
            )
        )
        if outcome.decision is None:
            raise EvaluationInvariantError("completed holdout cannot leave retention pending")
        return self._persist_decision(
            run=run,
            claim=claim,
            outcome=outcome,
            validation_metrics=validation_metrics,
            holdout_metrics=holdout_metrics,
        )

    async def _record_case(
        self,
        *,
        run: EvaluationRun,
        claim: EvaluationRunClaim,
        slice_: CandidateEvaluationSlice,
        case: dict[str, Any],
    ) -> None:
        """同一冻结 case 先取得 Agent 结果，再由领域 recorder 原子语义写入完整 pair。"""

        agent_result = await slice_.run_agent_case(case)
        slice_.record_pair(
            run=run,
            claim=claim,
            case=case,
            baseline=slice_.baseline_for_case(case),
            agent_result=agent_result,
        )

    def _save_split_metrics(
        self,
        *,
        run: EvaluationRun,
        claim: EvaluationRunClaim,
        split: EvaluationSplit,
        metric_ids: tuple[str, ...],
    ) -> tuple[PairedMetric, ...]:
        """从 selected Attempt 重算 split 指标；已写入的恢复路径只读取并核对身份。"""

        existing = self._store.list_paired_metrics(run.run_id, split)
        if existing:
            if tuple(metric.metric_id for metric in existing) != tuple(sorted(metric_ids)):
                raise EvaluationInvariantError("saved metric IDs do not match candidate policy")
            return existing
        pairs = self._selected_pairs(run=run, split=split)
        expected_count = 40 if split is EvaluationSplit.VALIDATION else 20
        if len(pairs) != expected_count:
            raise EvaluationInvariantError("formal metrics require complete selected pairs")
        metrics = tuple(
            aggregate_binary_pairs(
                metric_id=metric_id,
                pairs=tuple(
                    BinaryPair(
                        case_id=baseline.case_id,
                        baseline_success=self._metric_outcome(baseline, metric_id),
                        agent_success=self._metric_outcome(agent, metric_id),
                        agent_severe_violation=agent.severe_violation,
                    )
                    for baseline, agent in pairs
                ),
            )
            for metric_id in metric_ids
        )
        return tuple(
            self._store.save_paired_metric(run.run_id, split, metric, claim=claim)
            for metric in metrics
        )

    def _persist_decision(
        self,
        *,
        run: EvaluationRun,
        claim: EvaluationRunClaim,
        outcome: CandidateRetentionOutcome,
        validation_metrics: tuple[PairedMetric, ...],
        holdout_metrics: tuple[PairedMetric, ...],
    ) -> FormalCandidateEvaluationReport:
        """把 outcome 与从 Attempt 重建的计数一起持久化，调用方不能伪造完成度。"""

        if outcome.decision is None:
            raise EvaluationInvariantError("pending retention outcome cannot be persisted")
        severe_count, hard_gates, validation_count, holdout_count = self._selected_summary(run)
        record = RetentionDecisionRecord(
            decision_id=f"{run.run_id}:retention",
            run_id=run.run_id,
            candidate=run.candidate,
            decision=outcome.decision,
            reason_code=outcome.reason_code,
            external_evidence_sufficient=True,
            severe_violation_count=severe_count,
            metrics_digest=self._store.metrics_digest(run.run_id),
            completed_validation_cases=validation_count,
            completed_holdout_cases=holdout_count,
            hard_gates_passed=hard_gates,
        )
        return FormalCandidateEvaluationReport(
            decision=self._store.save_retention_decision(record, claim=claim),
            validation_metrics=validation_metrics,
            holdout_metrics=holdout_metrics,
        )

    def _selected_pairs(
        self,
        *,
        run: EvaluationRun,
        split: EvaluationSplit,
    ) -> tuple[tuple[CaseAttempt, CaseAttempt], ...]:
        """通过 Store 的唯一 selected 索引读取完整 pair，禁止把未选重试 Attempt 混入正式指标。"""

        case_ids = sorted(
            {
                attempt.case_id
                for attempt in self._store.list_attempts(run.run_id)
                if attempt.split is split
            }
        )
        return tuple(
            (
                self._store.get_selected_attempt(
                    run.run_id, case_id, EvaluationSubject.BASELINE.value
                ),
                self._store.get_selected_attempt(
                    run.run_id, case_id, EvaluationSubject.AGENT.value
                ),
            )
            for case_id in case_ids
        )

    def _selected_summary(self, run: EvaluationRun) -> tuple[int, bool, int, int]:
        """在协调层重复 Store 的核心摘要，避免最终 Gate 使用非 selected 或内存计数。"""

        selected_agents: list[CaseAttempt] = []
        completed: dict[EvaluationSplit, set[str]] = {
            EvaluationSplit.VALIDATION: set(),
            EvaluationSplit.HOLDOUT: set(),
        }
        for split in completed:
            for baseline, agent in self._selected_pairs(run=run, split=split):
                completed[split].add(baseline.case_id)
                selected_agents.append(agent)
        return (
            sum(item.severe_violation for item in selected_agents),
            bool(selected_agents)
            and all(all(_plain_json(item.gate_results).values()) for item in selected_agents),
            len(completed[EvaluationSplit.VALIDATION]),
            len(completed[EvaluationSplit.HOLDOUT]),
        )

    @staticmethod
    def _metric_outcome(attempt: CaseAttempt, metric_id: str) -> bool:
        value = _plain_json(attempt.metric_outcomes).get(metric_id)
        if type(value) is not bool:
            raise EvaluationInvariantError("selected attempt lacks required metric outcome")
        return value

    @staticmethod
    def _gate_status(value: Any) -> str:
        """兼容三个领域 gate 的 StrEnum，保持协调器不依赖候选模块实现。"""

        status = getattr(value, "status", None)
        return str(getattr(status, "value", status))

    @staticmethod
    def _gate_reason(value: Any) -> str:
        reason = getattr(value, "reason_code", "VALIDATION_GATE_REJECTED")
        return str(reason)


_REQUIRED_METRICS: Mapping[EvaluationCandidate, Mapping[str, tuple[Decimal, Decimal | None]]] = MappingProxyType(
    {
        EvaluationCandidate.LIVE_OPS: MappingProxyType(
            {
                "action_success_rate": (Decimal("0.90"), Decimal("5")),
                "incident_recovery_rate": (Decimal("0.85"), Decimal("10")),
            }
        ),
        EvaluationCandidate.PLANNER: MappingProxyType(
            {
                "executable_plan_success_rate": (Decimal("0.95"), None),
                "constraint_recovery_rate": (Decimal("0.85"), Decimal("10")),
            }
        ),
        EvaluationCandidate.REVIEW_MEMORY: MappingProxyType(
            {
                "grounded_attribution_rate": (Decimal("0.90"), Decimal("5")),
                "memory_candidate_macro_f1": (Decimal("0.85"), Decimal("10")),
            }
        ),
    }
)


def decide_candidate_retention(facts: CandidateGateFacts) -> CandidateRetentionOutcome:
    """应用冻结 AND 门，不允许实施者按主观解释放宽任一候选阈值。"""

    # 已写入 Evaluation Store 的严重违规是确定事实。即使之后出现 endpoint、usage
    # 或账本等外部证据缺口，也不能把已经观察到的安全失败降级成 INCONCLUSIVE。
    if facts.severe_violation_count:
        return CandidateRetentionOutcome(
            RetentionDecision.REJECTED,
            "SEVERE_SAFETY_VIOLATION",
        )
    if not facts.external_evidence_sufficient:
        return CandidateRetentionOutcome(
            RetentionDecision.INCONCLUSIVE,
            "EXTERNAL_EVIDENCE_INSUFFICIENT",
        )
    if facts.validation_cases != 40:
        return CandidateRetentionOutcome(
            RetentionDecision.REJECTED,
            "VALIDATION_NOT_COMPLETE",
        )
    for metric_id, (minimum_rate, minimum_delta) in _REQUIRED_METRICS[facts.candidate].items():
        actual = facts.metrics.get(metric_id)
        if actual is None:
            return CandidateRetentionOutcome(RetentionDecision.REJECTED, "REQUIRED_METRIC_MISSING")
        actual_rate, actual_delta = actual
        if actual_rate < minimum_rate or (
            minimum_delta is not None and actual_delta < minimum_delta
        ):
            return CandidateRetentionOutcome(RetentionDecision.REJECTED, "METRIC_THRESHOLD_FAILED")
    if facts.holdout_cases != 20:
        # validation 已经满足规则时，holdout 是继续执行条件，不是可以提前写入的保留结论。
        return CandidateRetentionOutcome(None, "HOLDOUT_REQUIRED")
    return CandidateRetentionOutcome(
        RetentionDecision.RETAINED,
        "ALL_CANDIDATE_GATES_PASSED",
    )


def build_formal_manifest_from_dataset(
    evaluation_root: Path,
    project_root: Path,
) -> EvaluationManifest:
    """从冻结数据基线派生正式身份，保留样本事实并绑定当前源码与 Git commit。"""

    root = Path(project_root).resolve()
    # D-110 的 LiveOps 修正数据与 Planner/Review 基线在 phase13-v3 组合；v2 仅保留
    # 审计，不得再派生正式 Run，否则会用已经证明不适用的动作标签作出保留结论。
    dataset_path = Path(evaluation_root) / "manifests" / "phase13-v3.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    source_store_manifest = dataset.get("store_manifest")
    if not isinstance(source_store_manifest, dict):
        raise ValueError("phase13-v3 dataset lacks store_manifest")
    try:
        source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise ValueError("formal manifest requires a Git HEAD") from error
    # 只从受 Task 6 校验的 store manifest 复制样本、Prompt、Schema 和价格身份；不允许
    # 调用方自行拼接 case 集或沿用 DATASET_BASELINE 的 source_commit/code_digest。
    payload = {
        key: value
        for key, value in source_store_manifest.items()
        if key not in {"manifest_digest", "manifest_id", "manifest_kind", "source_commit", "code_digest"}
    }
    payload.update(
        {
            "manifest_id": f"phase13-formal-{source_commit[:12]}",
            "manifest_kind": EvaluationManifestKind.FORMAL_EVALUATION.value,
            "source_commit": source_commit,
            "code_digest": calculate_source_code_digest(root),
        }
    )
    return EvaluationManifest.model_validate(payload)


def evaluate_real_model_preflight(
    *,
    api_key: str,
    endpoint_host: str,
    model_id: str,
    pricing_snapshot_present: bool,
) -> RealModelPreflight:
    """验证固定 HTTPS 供应商身份、密钥与价格快照；任何缺口都禁止网络访问。"""

    if not api_key or api_key.strip() in {"", "change_me", "test-secret"}:
        return RealModelPreflight(False, "MODEL_CREDENTIALS_UNAVAILABLE")
    # 解析完整 URL 而非删掉 scheme 后只比较 hostname，避免 http://、userinfo、
    # query/fragment 或非标准端口绕过“固定官方 HTTPS endpoint”这一硬边界。
    endpoint = urlsplit(endpoint_host)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "api.deepseek.com"
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port not in {None, 443}
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        return RealModelPreflight(False, "MODEL_ENDPOINT_MISMATCH")
    if model_id != "deepseek-v4-flash":
        return RealModelPreflight(False, "MODEL_ID_MISMATCH")
    if not pricing_snapshot_present:
        return RealModelPreflight(False, "MODEL_PRICING_UNAVAILABLE")
    return RealModelPreflight(True, "REAL_MODEL_PREFLIGHT_PASSED")


def verify_formal_pricing_snapshot(
    *,
    manifest: EvaluationManifest,
    evaluation_root: Path,
    pricing_snapshot_path: Path,
) -> RealModelPreflight:
    """核对正式 Manifest、v3 数据基线与价格快照的原始字节和冻结换算策略。"""

    if manifest.manifest_kind is not EvaluationManifestKind.FORMAL_EVALUATION:
        return RealModelPreflight(False, "FORMAL_MANIFEST_REQUIRED")
    snapshot_path = Path(pricing_snapshot_path)
    try:
        snapshot_bytes = snapshot_path.read_bytes()
        snapshot = json.loads(snapshot_bytes.decode("utf-8"))
        dataset = json.loads(
            (Path(evaluation_root) / "manifests" / "phase13-v3.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return RealModelPreflight(False, "MODEL_PRICING_UNAVAILABLE")
    snapshot_digest = sha256(snapshot_bytes).hexdigest()
    if (
        snapshot_digest != manifest.pricing_source_digest
        or snapshot_digest != dataset.get("pricing_source_digest")
    ):
        return RealModelPreflight(False, "MODEL_PRICING_DIGEST_MISMATCH")
    pricing = dataset.get("pricing")
    if not isinstance(pricing, dict):
        return RealModelPreflight(False, "MODEL_PRICING_UNAVAILABLE")
    policy_digest = sha256(
        (
            json.dumps(
                {
                    "conversion_policy_version": pricing.get("conversion_policy_version"),
                    "pricing": pricing,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    if policy_digest != manifest.price_policy_digest:
        return RealModelPreflight(False, "MODEL_PRICE_POLICY_MISMATCH")
    try:
        valid_snapshot = (
            snapshot["source_url"] == "https://api-docs.deepseek.com/quick_start/pricing"
            and snapshot["source_currency"] == "USD"
            and snapshot["conversion_policy"]["policy_version"]
            == pricing["conversion_policy_version"]
            and Decimal(snapshot["official_prices_usd_per_million_tokens"]["cache_miss_input"])
            > Decimal("0")
            and Decimal(snapshot["official_prices_usd_per_million_tokens"]["output"])
            > Decimal("0")
        )
    except (KeyError, TypeError, ArithmeticError):
        valid_snapshot = False
    if not valid_snapshot:
        return RealModelPreflight(False, "MODEL_PRICING_INVALID")
    return RealModelPreflight(True, "MODEL_PRICING_VERIFIED")


def evaluate_preflight_only(
    *,
    api_key: str,
    endpoint_host: str,
    model_id: str,
    pricing_snapshot_present: bool,
) -> PreflightOnlyEvaluationReport:
    """在真实模型被阻断时生成三个候选的一致 INCONCLUSIVE 预检报告。"""

    preflight = evaluate_real_model_preflight(
        api_key=api_key,
        endpoint_host=endpoint_host,
        model_id=model_id,
        pricing_snapshot_present=pricing_snapshot_present,
    )
    if preflight.allowed:
        return PreflightOnlyEvaluationReport(preflight, MappingProxyType({}))
    outcomes = {
        candidate: decide_candidate_retention(
            CandidateGateFacts(
                candidate=candidate,
                validation_cases=0,
                holdout_cases=0,
                severe_violation_count=0,
                external_evidence_sufficient=False,
                metrics={},
            )
        )
        for candidate in EvaluationCandidate
    }
    return PreflightOnlyEvaluationReport(preflight, MappingProxyType(outcomes))
