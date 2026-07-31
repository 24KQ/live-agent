"""Phase 16 V9 Development Candidate 2 真实模型 campaign。

修复：
  - count_input_tokens 乘以 1.5 系数使预留金额覆盖实际计费
  - max_total_tokens 从 6000 增至 8000（避免剩余 token 不足）
  - deadline_seconds 从 60 增至 90（减少 API 超时）

一次性单发 12 个高冲突 development cases，每个 case 走
EvidenceAnalystAgent → DecisionPlannerAgent。已发送语义失败继续走完剩余 slot；
hard/receipt 失败立即阻断。预算上限 4.00 CNY，每 stage 预留 0.10 CNY。
无重试、temperature 0。

用法：
    cd .worktrees/phase16-v5-controlled-e2e
    python scripts/run_phase16_development_candidate_2.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    """加载构建条件、预检、运行 campaign 并输出脱敏报告。"""

    import argparse

    parser = argparse.ArgumentParser(description="Run a Phase 16 qualification campaign")
    parser.add_argument(
        "--kind",
        choices=("DEVELOPMENT", "VALIDATION"),
        default="DEVELOPMENT",
        help="campaign kind（默认 DEVELOPMENT；VALIDATION 使用验证 split）",
    )
    args = parser.parse_args()

    # ── 1. 加载 .env ──
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")

    # V9 矩阵配置：渠道是有序列表（LLM_API_CHANNEL_HOSTS / LLM_API_CHANNEL_KEYS，
    # 逗号分隔、位置一一对应、顺序即优先级）；模型与思考强度单值 env 覆写。
    channel_hosts = tuple(
        host.strip()
        for host in os.environ.get("LLM_API_CHANNEL_HOSTS", "").split(",")
        if host.strip()
    )
    channel_keys = tuple(
        key.strip()
        for key in os.environ.get("LLM_API_CHANNEL_KEYS", "").split(",")
        if key.strip()
    )
    model_id = os.environ.get("LLM_API_MODEL_ID", "gpt-5.6-luna").strip()
    reasoning_effort = os.environ.get("LLM_API_REASONING_EFFORT", "").strip()

    if not channel_hosts:
        print("[ENV] BLOCKED: LLM_API_CHANNEL_HOSTS not set (comma-separated, priority order)")
        return 1
    if len(channel_hosts) != len(channel_keys):
        print("[ENV] BLOCKED: LLM_API_CHANNEL_HOSTS / LLM_API_CHANNEL_KEYS count mismatch")
        return 1
    if not all(channel_keys):
        print("[ENV] BLOCKED: every channel needs a non-empty API key")
        return 1
    endpoints = tuple(zip(channel_hosts, channel_keys))
    print(f"[ENV] model={model_id} channels={','.join(channel_hosts)}"
          + (f" reasoning={reasoning_effort}" if reasoning_effort else ""))
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
    kind = QualificationCampaignKind(args.kind)
    print(f"[POLICY] {policy.policy_id} v{policy.policy_version} digest={policy.policy_digest[:24]}...")
    print(f"[CORPUS] {corpus.manifest.corpus_id} manifest={corpus.manifest.manifest_digest[:24]}...")
    case_source = (
        corpus.validation_cases
        if kind is QualificationCampaignKind.VALIDATION
        else corpus.development_cases
    )
    high_conflict_cases = tuple(
        c for c in case_source
        if c.kind.value == "HIGH_CONFLICT_PAIRED"
    )
    print(f"[CASES] {len(high_conflict_cases)} high-conflict {kind.value.lower()} cases")

    from src.decision_support.phase16_qualification_evaluator import (
        QualificationEvaluator,
        build_phase16_qualification_candidate_bundle,
    )
    bundle = build_phase16_qualification_candidate_bundle(
        policy=policy, repository_root=_PROJECT_ROOT,
    )
    print(f"[CANDIDATE] {bundle.candidate.candidate_id} digest={bundle.candidate.candidate_digest[:24]}...")
    print(f"[CANDIDATE] declared model={model_id} channels={','.join(channel_hosts)}")
    if reasoning_effort:
        print(f"[CANDIDATE] reasoning_effort={reasoning_effort}")

    # ── 4. 注册 identity（幂等；同一 digest 的 policy/corpus/candidate 已存在则跳过） ──
    ledger.ensure_policy(policy)
    ledger.ensure_corpus(identity)
    ledger.ensure_candidate(bundle.candidate)

    # campaign_id 绑定 candidate digest 前缀：冻结输入变化 → 新 digest → 新 campaign 行，
    # 旧证据保留（append-only），不需要清库；同一 digest 再次运行会被 UNIQUE(campaign_id)
    # 与 begin_run 的终态检查拒绝（无重试语义）。
    campaign_id = f"phase16-{kind.value.lower()}-{bundle.candidate.candidate_digest[:16]}"
    # 每 campaign 预留只需覆盖自身最坏情况（24 stages × ~0.053 ≈ 1.27），
    # 让同一 project 下可并存 DEVELOPMENT + VALIDATION 两个 campaign。
    campaign = QualificationCampaign(
        campaign_id=campaign_id,
        campaign_kind=kind,
        policy_digest=policy.policy_digest or "",
        corpus_digest=identity.corpus_digest,
        candidate_digest=bundle.candidate.candidate_digest or "",
        manifest_digest=corpus.manifest.manifest_digest or "",
        reservation_cny="1.500000",
        # V9 矩阵配置：campaign 钉死本次运行时组合；白名单外值由校验器 fail-fast。
        declared_model_id=model_id,
        declared_reasoning_effort=reasoning_effort or None,
        declared_endpoint_hosts=channel_hosts,
    )
    ledger.ensure_campaign(campaign)

    run_id = f"{campaign_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    print(f"[CAMPAIGN] {campaign_id} → run_id={run_id}")

    # ── 5. 装配模型端口 ──
    # candidate digest 绑定 controlled_e2e_adapter_v5.py（V9 起含传输层重试与渠道链），
    # 实际发送必须走同一包装层，否则重试不生效、digest 绑定失真。
    # 渠道有序列表由 LLM_API_CHANNEL_HOSTS/LLM_API_CHANNEL_KEYS 装配（顺序即优先级）；
    # 思考强度/模型白名单由 adapter 构造时对 env 强制校验。
    from src.decision_support.controlled_e2e_adapter_v5 import DeepSeekV5ControlledE2EAdapter
    model_port = DeepSeekV5ControlledE2EAdapter(endpoints=endpoints)

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
    print(f"RUNNING {kind.value.title()} Candidate 2 — {len(high_conflict_cases)} cases × 2 stages")
    print(f"{'='*60}")
    report = asyncio.run(runner.execute(
        campaign=campaign, run_id=run_id,
    ))

    # ── 7. 输出报告 ──
    print(f"\n{'='*60}")
    print(f"{kind.value.title()} CAMPAIGN REPORT")
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
        from psycopg import sql
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
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

    print(f"\nDone. Budget cap: 4.00 CNY | Stage res: 0.10 CNY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
