"""Phase 16 V9 Development Candidate 1 真实模型 campaign。

一次性单发 12 个高冲突 development cases，每个 case 走
EvidenceAnalystAgent → DecisionPlannerAgent。已发送语义失败继续走完剩余 slot；
hard/receipt 失败立即阻断。预算上限 1.00 CNY，每 stage 预留 0.03 CNY。
无重试、temperature 0、temperature 0。

用法：
    cd .worktrees/phase16-v5-controlled-e2e
    python scripts/run_phase16_development_candidate_1.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    """加载构建条件、预检、运行 campaign 并输出脱敏报告。"""

    # ── 1. 加载 .env ──
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_API_BASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()

    if not api_key or "api.deepseek.com" not in base_url:
        print("[ENV] BLOCKED: LLM_API_KEY or LLM_API_BASE_URL not set")
        return 1
    from src.config.settings import get_settings
    settings = get_settings()

    # ── 2. 初始化资格 schema ──
    from src.decision_support.phase16_qualification_ledger import (
        QualificationCampaign,
        QualificationCampaignKind,
        corpus_identity_from_manifest,
        initialize_phase16_qualification_schema,
    )
    initialize_phase16_qualification_schema(settings)
    print("[DB] qualification schema ready")

    from src.decision_support.phase16_qualification_execution_ledger import (
        PostgresPhase16QualificationExecutionLedger,
    )
    hmac_hex = os.environ.get("PHASE16_OFFICIAL_SMOKE_V2_RECEIPT_HMAC_HEX", "").strip()
    try:
        hmac_key = bytes.fromhex(hmac_hex)
    except (ValueError, TypeError):
        print("[HMAC] BLOCKED: invalid HMAC key")
        return 1
    ledger = PostgresPhase16QualificationExecutionLedger(settings, hmac_key=hmac_key)

    # ── 3. 加载 Policy / Corpus / Candidate ──
    from src.decision_support.phase16_qualification import (
        PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        build_phase16_qualification_policy,
        load_phase16_qualification_corpus,
    )
    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    corpus = load_phase16_qualification_corpus(
        _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        repository_root=_PROJECT_ROOT,
        policy=policy,
    )
    identity = corpus_identity_from_manifest(corpus.manifest)
    print(f"[POLICY] {policy.policy_id} v{policy.policy_version} digest={policy.policy_digest[:24]}...")
    print(f"[CORPUS] {corpus.manifest.corpus_id} manifest={corpus.manifest.manifest_digest[:24]}...")
    hc_dev = tuple(
        c for c in corpus.development_cases
        if c.kind.value == "HIGH_CONFLICT_PAIRED"
    )
    print(f"[CASES] {len(hc_dev)} high-conflict development cases")

    from src.decision_support.phase16_qualification_evaluator import (
        QualificationEvaluator,
        build_phase16_qualification_candidate_bundle,
    )
    bundle = build_phase16_qualification_candidate_bundle(
        policy=policy, repository_root=_PROJECT_ROOT,
    )
    print(f"[CANDIDATE] {bundle.candidate.candidate_id} digest={bundle.candidate.candidate_digest[:24]}...")

    # ── 4. 注册 identity（幂等） ──
    ledger.ensure_policy(policy)
    ledger.ensure_corpus(identity)
    ledger.ensure_candidate(bundle.candidate)

    campaign_id = "phase16-development-candidate-1"
    campaign = QualificationCampaign(
        campaign_id=campaign_id,
        campaign_kind=QualificationCampaignKind.DEVELOPMENT,
        policy_digest=policy.policy_digest or "",
        corpus_digest=identity.corpus_digest,
        candidate_digest=bundle.candidate.candidate_digest or "",
        manifest_digest=corpus.manifest.manifest_digest or "",
        reservation_cny="1.000000",
    )
    ledger.ensure_campaign(campaign)

    run_id = f"{campaign_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    print(f"[CAMPAIGN] {campaign_id} → run_id={run_id}")

    # ── 5. 装配模型端口 ──
    from src.specialist_runtime.deepseek_adapter import DeepSeekAgentModelAdapter
    model_port = DeepSeekAgentModelAdapter(api_key=api_key)

    # ── 6. 运行 campaign ──
    from src.decision_support.phase16_qualification_runner import (
        Phase16QualificationCampaignRunner,
    )
    runner = Phase16QualificationCampaignRunner(
        policy=policy,
        corpus=corpus,
        candidate_bundle=bundle,
        ledger=ledger,
        model_port=model_port,
    )
    print(f"\n{'='*60}")
    print(f"RUNNING Development Candidate 1 — {len(hc_dev)} cases × 2 stages")
    print(f"{'='*60}")
    report = asyncio.run(runner.execute(
        campaign=campaign, run_id=run_id,
    ))

    # ── 7. 输出报告 ──
    print(f"\n{'='*60}")
    print(f"DEVELOPMENT CAMPAIGN REPORT")
    print(f"{'='*60}")
    print(f"Status:       {report.status}")
    print(f"Model calls:  {report.model_calls}")
    print(f"Reason codes: {', '.join(report.reason_codes)}")
    print()
    metrics = {m.metric_code: m for m in report.metric_facts}
    for code in [
        "E2E_MULTI_AGENT_READY",
        "ANALYST_SCHEMA_AND_SEMANTIC_VALID",
        "PLANNER_RISK_COVERAGE",
        "EXPLANATION_BOUND",
        "CONTROLLED_EVIDENCE_BINDING",
        "HARD_SAFETY_CONFORMANCE",
        "OPTION_VALIDITY",
    ]:
        m = metrics.get(code)
        if m:
            print(f"  {code:40s} {m.numerator}/{m.denominator}")
        else:
            print(f"  {code:40s} MISSING")

    # ── 8. Evaluator assessment ──
    ev = QualificationEvaluator()
    try:
        assessment = ev.assess(
            campaign=campaign,
            policy=policy,
            corpus=identity,
            candidate=bundle.candidate,
            metric_facts=report.metric_facts,
        )
        print(f"\n[ASSESSMENT] status: {assessment.status.value}")
        print(f"[ASSESSMENT] reason_codes: {', '.join(assessment.reason_codes)}")
        print(f"[ASSESSMENT] assessment_digest: {assessment.assessment_digest[:24]}...")
    except Exception as exc:
        print(f"\n[ASSESSMENT] FAILED: {type(exc).__name__}: {exc}")

    # ── 9. Ledger 终态验证 ──
    try:
        result = ledger.report(run_id=run_id)
        print(f"\n[LEDGER] status: {result.status.value}")
        print(f"[LEDGER] authenticated: {result.authenticated}")
    except Exception as exc:
        print(f"\n[LEDGER] report FAILED: {type(exc).__name__}: {exc}")

    # ── 10. 简要 case 级摘要（从 ledger 直接读取） ──
    print(f"\n{'='*60}")
    print("CASE-LEVEL SUMMARY (from ledger)")
    print(f"{'='*60}")
    try:
        import psycopg
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            from psycopg import sql
            rows = conn.execute(
                sql.SQL(
                    "SELECT case_id, status, reason_code "
                    "FROM phase16_qualification_case_outcomes "
                    "WHERE run_id = %s ORDER BY case_id"
                ),
                (run_id,),
            ).fetchall()
            for row in rows:
                print(f"  {row[0]:65s} {row[1]:10s} {row[2]}")
    except Exception as exc:
        print(f"  (ledger read unavailable: {exc})")

    print(f"\nDone. Budget cap: 1.00 CNY | Stage res: 0.03 CNY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
