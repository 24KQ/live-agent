"""Phase 13 配对二元指标与 Wilson 区间计算。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from math import sqrt

from src.specialist_evaluation.models import PairedMetric
from src.specialist_runtime.models import canonical_json_sha256


@dataclass(frozen=True)
class BinaryPair:
    """同一个 case 的 baseline/Agent 二元结果。"""

    case_id: str
    baseline_success: bool
    agent_success: bool
    agent_severe_violation: bool = False


def _rate(successes: int, total: int) -> Decimal:
    return (Decimal(successes) / Decimal(total)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _wilson(successes: int, total: int) -> tuple[Decimal, Decimal]:
    """用固定 95% 正态近似计算区间，避免评估报告依赖外部统计库。"""

    p = successes / total
    z = 1.959963984540054
    denominator = 1 + (z * z / total)
    center = (p + (z * z / (2 * total))) / denominator
    radius = z * sqrt((p * (1 - p) / total) + (z * z / (4 * total * total))) / denominator
    quantize = lambda value: Decimal(str(max(0.0, min(1.0, value)))).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return quantize(center - radius), quantize(center + radius)


def aggregate_binary_pairs(*, metric_id: str, pairs: tuple[BinaryPair, ...]) -> PairedMetric:
    """聚合配对绝对率、百分点差、paired wins/losses 和严重违规。"""

    if not pairs or any(not pair.case_id for pair in pairs) or len({pair.case_id for pair in pairs}) != len(pairs):
        raise ValueError("pairs must contain unique non-empty cases")
    baseline_successes = sum(pair.baseline_success for pair in pairs)
    agent_successes = sum(pair.agent_success for pair in pairs)
    wins = sum(pair.agent_success and not pair.baseline_success for pair in pairs)
    losses = sum(pair.baseline_success and not pair.agent_success for pair in pairs)
    tied = len(pairs) - wins - losses
    baseline_rate = _rate(baseline_successes, len(pairs))
    agent_rate = _rate(agent_successes, len(pairs))
    baseline_low, baseline_high = _wilson(baseline_successes, len(pairs))
    agent_low, agent_high = _wilson(agent_successes, len(pairs))
    facts = [
        {
            "case_id": pair.case_id,
            "baseline_success": pair.baseline_success,
            "agent_success": pair.agent_success,
            "agent_severe_violation": pair.agent_severe_violation,
        }
        for pair in sorted(pairs, key=lambda item: item.case_id)
    ]
    return PairedMetric(
        metric_id=metric_id,
        case_ids=tuple(item["case_id"] for item in facts),
        sample_count=len(pairs),
        baseline_success_count=baseline_successes,
        agent_success_count=agent_successes,
        baseline_rate=baseline_rate,
        agent_rate=agent_rate,
        delta_percentage_points=((agent_rate - baseline_rate) * 100).quantize(Decimal("0.000001")),
        paired_wins=wins,
        paired_losses=losses,
        tied=tied,
        severe_violation_count=sum(pair.agent_severe_violation for pair in pairs),
        baseline_wilson_low=baseline_low,
        baseline_wilson_high=baseline_high,
        agent_wilson_low=agent_low,
        agent_wilson_high=agent_high,
        metric_facts_digest=canonical_json_sha256(facts),
    )
