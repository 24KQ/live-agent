"""单 case Analyst 校验失败根因诊断（只发一次真实模型调用）。

复用 runner 的投影与受限执行装配，但绕过 ledger（不需要 DB 写入），
直接对真实模型结果调用 validate_v2_conflict_analysis_result 并暴露底层异常。
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

from dotenv import load_dotenv


def main() -> int:
    load_dotenv(_PROJECT_ROOT / ".env")
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("[ENV] BLOCKED: LLM_API_KEY not set")
        return 1

    from src.decision_support.phase16_qualification import (
        PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        build_phase16_qualification_policy,
        load_phase16_qualification_corpus,
    )
    from src.decision_support.phase16_qualification_evaluator import (
        build_phase16_qualification_candidate_bundle,
    )
    from src.decision_support.phase16_qualification_runner import (
        _NoSkillPort,
        _QualificationPricingPolicy,
        _build_projection,
    )
    from src.decision_support.multi_agent import validate_v2_conflict_analysis_result
    from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
    from src.specialist_runtime.runner import BoundedSpecialistRunner
    from src.specialist_runtime.deepseek_adapter import DeepSeekAgentModelAdapter

    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    corpus = load_phase16_qualification_corpus(
        _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        repository_root=_PROJECT_ROOT,
    )
    bundle = build_phase16_qualification_candidate_bundle(
        policy=policy, repository_root=_PROJECT_ROOT,
    )
    case = next(c for c in corpus.development_cases if c.kind.value == "HIGH_CONFLICT_PAIRED")
    now = datetime.now(timezone.utc)
    projection = _build_projection(case=case, now=now, analyst_profile=bundle.analyst_profile)
    print(f"[CASE] {case.case_id}")
    print(f"[TRIGGERS] {[c.value for c in projection.trigger_codes]}")
    print(f"[EVIDENCE REFS] {len(projection.evidence_refs)}")

    model_port = DeepSeekAgentModelAdapter(api_key=api_key)
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((bundle.analyst_profile,))),
        model_port=model_port,
        budget_store=_DiagBudgetStore(),
        evidence_registry=projection.evidence_registry,
        skill_port=_NoSkillPort(),
        skill_catalog=(),
        trusted_anchor_resolver=lambda _task: projection.trusted_anchor_id,
        pricing_policy=_QualificationPricingPolicy(),
        budget_candidate_resolver=lambda _task: "ANALYST",
        request_id_factory=lambda _task, _execution_id, _index: f"diag-{case.case_id}-analyst",
        clock=lambda: datetime.now(timezone.utc),
    )

    task = projection.analyst_task
    result = asyncio.run(runner.run(task))
    print(f"[RUNNER] status={result.status.value} failure={result.failure.code if result.failure else 'NONE'}")
    print(f"[USAGE] input={result.input_tokens} output={result.output_tokens} total={result.total_tokens}")
    if result.status.value != "SUCCEEDED":
        print("[RUNNER OUTPUT] (no output)")
        return 2

    import json
    from src.specialist_runtime.models import _plain_json
    print("[MODEL OUTPUT]")
    print(json.dumps(_plain_json(result.output), ensure_ascii=False, indent=2)[:4000])

    try:
        validated = validate_v2_conflict_analysis_result(
            task=task,
            result=result,
            expected_profile=bundle.analyst_profile,
            expected_evidence_refs=projection.evidence_refs,
            expected_finding_codes=projection.trigger_codes,
        )
        print("\n[VALIDATION] PASS")
        print(f"  constraint_codes={[c.value for c in validated.constraint_codes]}")
        print(f"  risk_codes={[c.value for c in validated.risk_codes]}")
        print(f"  explanation_len={len(validated.explanation)}")
    except Exception as error:
        print(f"\n[VALIDATION] FAILED: {type(error).__name__}: {error}")
        cause = error.__cause__
        while cause is not None:
            print(f"  caused by: {type(cause).__name__}: {cause}")
            cause = cause.__cause__
        return 3
    return 0


class _DiagBudgetStore:
    """无持久化预算桩：诊断不写入 ledger，只让受限 runner 通过预算边界。"""

    def reserve(self, *args, **kwargs):
        return _DiagClaim()

    def settle(self, *args, **kwargs):
        return _DiagClaim()

    def release(self, *args, **kwargs):
        return _DiagClaim()


class _DiagClaim:
    created = True


if __name__ == "__main__":
    raise SystemExit(main())
