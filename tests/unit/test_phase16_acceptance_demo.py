"""Phase 16 Task 11 受控双 Agent 本地 Demo 与 Acceptance 的 RED 契约。

这些测试只回放固定的内存事实链与 ScriptedModel，不连接真实模型、淘宝 API、
生产 PostgreSQL 或任何外部服务。真实 smoke 的外部证据没有齐备时，报告必须保持
INCONCLUSIVE，不能把本地演练误写为生产通过。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts import run_all
from scripts.run_phase16_controlled_multi_agent_demo import (
    DEMO_LIVE_SESSION_ID,
    Phase16AcceptanceStatus,
    _ready_lineage_complete,
    render_acceptance_report,
    run_demo,
    write_acceptance_report,
)
from src.decision_support.models import MultiAgentOutcomeStatus
from src.specialist_runtime.models import canonical_json_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_phase16_demo_replays_protection_before_controlled_dual_agent_and_operator_boundary(
    tmp_path: Path,
) -> None:
    """同一售罄会话必须先完成确定性保护，再产生一次受控双 Agent READY 方案。"""

    result = run_demo(tmp_path / "evaluation")

    assert result.status is Phase16AcceptanceStatus.INCONCLUSIVE
    assert result.phase_state == "AWAITING_PHASE_17_GATE"
    assert result.live_session_id == DEMO_LIVE_SESSION_ID
    assert result.automatic_protection_status == "APPLIED"
    assert result.automatic_protection_authoritative is True
    assert result.automatic_protection_event_application_state == "APPLIED"
    assert result.automatic_protection_external_write_count == 1
    assert result.automatic_protection_root_plan_run_id
    assert result.automatic_protection_evidence_bound is True
    assert result.execution_order == (
        "AUTOMATIC_PROTECTION",
        "CONFLICT_ANALYSIS",
        "LIVE_DECISION_PLANNING",
        "OPERATOR_DECISION_COMPILED",
    )
    assert result.dual_agent_call_sequence == (
        "CONFLICT_ANALYSIS",
        "LIVE_DECISION_PLANNING",
    )
    assert result.dual_agent_call_counts == {"analyst": 1, "planner": 1}
    assert result.ready_proposal_origin == "MULTI_AGENT"
    assert result.ready_outcome_status == "READY"
    assert result.ready_lineage_complete is True
    assert result.ready_proposal_digest
    assert result.ready_outcome_id
    assert result.ready_outcome_digest
    assert result.operator_decision_kinds == ("APPROVE", "MODIFY", "REJECT")
    assert result.selected_operator_decision_kind == "MODIFY"
    assert result.compiled_command_id
    assert result.compiled_command_context_bound is True
    assert result.execution_command_persisted is True
    assert result.execution_command_submitted is False
    assert result.execution_submission_count == 0
    assert result.replay_stable is True
    assert result.restart_store_reconstructed is True
    assert result.replay_agent_call_sequence == ()


def test_phase16_demo_is_byte_stable_and_honestly_blocks_real_smoke(tmp_path: Path) -> None:
    """重建 Demo 不得重发 Agent；缺失外部证据时真实 smoke 和阶段结论必须保持保守。"""

    first = run_demo(tmp_path / "first")
    second = run_demo(tmp_path / "second")

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.real_smoke_status == "BLOCKED"
    assert "ENDPOINT_UNAVAILABLE" in first.real_smoke_reason_codes
    assert "USAGE_CONTRACT_UNAVAILABLE" in first.real_smoke_reason_codes
    assert first.real_model_call_count == 0
    assert first.real_model_cost_cny == "0.000000"
    assert first.production_default_route == "DETERMINISTIC_ONLY"
    assert first.task9_total_cases == 48
    assert first.task9_route_correct_cases == 48
    assert first.task9_pairwise_identity_correct_cases == 24

    rendered = render_acceptance_report(first)
    assert "Phase 16 Controlled Multi-Agent Escalation Acceptance" in rendered
    assert "AWAITING_PHASE_17_GATE" in rendered
    assert "REAL_MODEL_SMOKE_NOT_RUN" in rendered
    assert f"- Escalation: `{first.escalation_id}` / `{first.escalation_digest}`" in rendered
    assert f"- Analysis: `{first.analysis_id}` / `{first.analysis_digest}`" in rendered
    assert f"- Proposal: `{first.ready_proposal_id}` / `{first.ready_proposal_digest}`" in rendered
    assert f"- Outcome: `{first.ready_outcome_id}` / `{first.ready_outcome_digest}`" in rendered

    report_path = write_acceptance_report(tmp_path, first)
    assert report_path.read_text(encoding="utf-8") == rendered


def test_phase16_demo_is_discoverable_from_the_unified_entrypoint(
    monkeypatch: Any,
) -> None:
    """本地演示必须从统一 CLI 和 README 可发现，且入口本身不得探测外部服务。"""

    calls: list[tuple[str, tuple[str, ...]]] = []

    def record_run(script_name: str, *args: str) -> int:
        calls.append((script_name, args))
        return 16

    monkeypatch.setattr(run_all, "_run_python", record_run)

    assert run_all.main(["phase16-demo"]) == 16
    assert calls == [("run_phase16_controlled_multi_agent_demo.py", ())]
    assert "python scripts/run_all.py phase16-demo" in Path("README.md").read_text(
        encoding="utf-8"
    )


def test_checked_in_phase16_acceptance_matches_current_deterministic_renderer() -> None:
    """仓库中的 Acceptance 必须由当前 Demo 事实生成，整改后不能留下过期报告。"""

    result = run_demo(PROJECT_ROOT / "evaluation")
    checked_in = (
        PROJECT_ROOT
        / "docs"
        / "superpowers"
        / "reports"
        / "phase-16-controlled-multi-agent-acceptance.md"
    )

    assert checked_in.read_text(encoding="utf-8") == render_acceptance_report(result)


def test_ready_lineage_rejects_mismatched_bundle_evidence_and_outcome_digests() -> None:
    """Acceptance 的完整 lineage 结论必须逐字段闭合全部父事实，不能只核对局部 ID。"""

    digest = "a" * 64
    proposal_snapshot = {"proposal": "phase16-demo"}
    references = ("evidence-a", "evidence-b")
    lineage = SimpleNamespace(
        escalation_id="escalation-a",
        escalation_digest=digest,
        analysis_id="analysis-a",
        analysis_digest=digest,
        evidence_bundle_id="bundle-a",
        evidence_bundle_digest=digest,
        evidence_refs=references,
    )
    proposal = SimpleNamespace(
        proposal_id="proposal-a",
        multi_agent_lineage=lineage,
        evidence_bundle_id="bundle-a",
        evidence_bundle_digest=digest,
        evidence_refs=references,
        model_dump=lambda **_kwargs: proposal_snapshot,
    )
    proposal_digest = canonical_json_sha256(proposal_snapshot)
    result = SimpleNamespace(
        escalation=SimpleNamespace(
            escalation_id="escalation-a",
            escalation_digest=digest,
            evidence_bundle_id="bundle-a",
            evidence_bundle_digest=digest,
        ),
        analysis=SimpleNamespace(
            analysis_id="analysis-a",
            analysis_digest=digest,
            escalation_id="escalation-a",
            evidence_bundle_id="bundle-a",
            evidence_bundle_digest=digest,
            evidence_refs=references,
        ),
        proposal=proposal,
        outcome=SimpleNamespace(
            status=MultiAgentOutcomeStatus.READY,
            escalation_id="escalation-a",
            escalation_digest=digest,
            analysis_id="analysis-a",
            analysis_digest=digest,
            proposal_id="proposal-a",
            proposal_digest=proposal_digest,
            evidence_bundle_id="bundle-forged",
            evidence_bundle_digest=digest,
        ),
    )

    assert _ready_lineage_complete(result) is False


def test_ready_lineage_rejects_forged_outcome_digest() -> None:
    """Outcome 自身的摘要也属于 append-only 审计链，不能只校验它引用的父摘要。"""

    digest = "a" * 64
    references = ("evidence-a",)
    proposal_snapshot = {"proposal": "phase16-demo"}
    lineage = SimpleNamespace(
        escalation_id="escalation-a",
        escalation_digest=digest,
        analysis_id="analysis-a",
        analysis_digest=digest,
        evidence_bundle_id="bundle-a",
        evidence_bundle_digest=digest,
        evidence_refs=references,
    )
    proposal = SimpleNamespace(
        proposal_id="proposal-a",
        multi_agent_lineage=lineage,
        evidence_bundle_id="bundle-a",
        evidence_bundle_digest=digest,
        evidence_refs=references,
        model_dump=lambda **_kwargs: proposal_snapshot,
    )
    result = SimpleNamespace(
        escalation=SimpleNamespace(
            escalation_id="escalation-a",
            escalation_digest=digest,
            evidence_bundle_id="bundle-a",
            evidence_bundle_digest=digest,
        ),
        analysis=SimpleNamespace(
            analysis_id="analysis-a",
            analysis_digest=digest,
            escalation_id="escalation-a",
            evidence_bundle_id="bundle-a",
            evidence_bundle_digest=digest,
            evidence_refs=references,
        ),
        proposal=proposal,
        outcome=SimpleNamespace(
            status=MultiAgentOutcomeStatus.READY,
            escalation_id="escalation-a",
            escalation_digest=digest,
            analysis_id="analysis-a",
            analysis_digest=digest,
            proposal_id="proposal-a",
            proposal_digest=canonical_json_sha256(proposal_snapshot),
            evidence_bundle_id="bundle-a",
            evidence_bundle_digest=digest,
            outcome_digest="c" * 64,
            model_dump=lambda **_kwargs: {"outcome": "phase16-demo"},
        ),
    )

    assert _ready_lineage_complete(result) is False


def test_ready_lineage_rejects_analysis_linked_to_a_different_escalation() -> None:
    """Analysis 即使复用了同一 Bundle/证据，也必须精确属于当前 Escalation。"""

    digest = "a" * 64
    references = ("evidence-a",)
    proposal_snapshot = {"proposal": "phase16-demo"}
    lineage = SimpleNamespace(
        escalation_id="escalation-a",
        escalation_digest=digest,
        analysis_id="analysis-a",
        analysis_digest=digest,
        evidence_bundle_id="bundle-a",
        evidence_bundle_digest=digest,
        evidence_refs=references,
    )
    proposal = SimpleNamespace(
        proposal_id="proposal-a",
        multi_agent_lineage=lineage,
        evidence_bundle_id="bundle-a",
        evidence_bundle_digest=digest,
        evidence_refs=references,
        model_dump=lambda **_kwargs: proposal_snapshot,
    )
    outcome_snapshot = {"outcome": "phase16-demo"}
    result = SimpleNamespace(
        escalation=SimpleNamespace(
            escalation_id="escalation-a",
            escalation_digest=digest,
            evidence_bundle_id="bundle-a",
            evidence_bundle_digest=digest,
        ),
        analysis=SimpleNamespace(
            analysis_id="analysis-a",
            analysis_digest=digest,
            escalation_id="escalation-forged",
            evidence_bundle_id="bundle-a",
            evidence_bundle_digest=digest,
            evidence_refs=references,
        ),
        proposal=proposal,
        outcome=SimpleNamespace(
            status=MultiAgentOutcomeStatus.READY,
            escalation_id="escalation-a",
            escalation_digest=digest,
            analysis_id="analysis-a",
            analysis_digest=digest,
            proposal_id="proposal-a",
            proposal_digest=canonical_json_sha256(proposal_snapshot),
            evidence_bundle_id="bundle-a",
            evidence_bundle_digest=digest,
            outcome_digest=canonical_json_sha256(outcome_snapshot),
            model_dump=lambda **_kwargs: outcome_snapshot,
        ),
    )

    assert _ready_lineage_complete(result) is False
