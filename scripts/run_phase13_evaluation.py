"""Phase 13 正式评估的预检入口。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import argparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # 允许从仓库根直接执行 python scripts/run_phase13_evaluation.py，避免依赖外部 PYTHONPATH。
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings
from src.specialist_evaluation.formal_runtime import (
    FrozenPricingPolicy,
    build_formal_bounded_runner,
    build_formal_slice,
)
from src.specialist_evaluation.manifest_authorization import verify_formal_manifest_at_git_head
from src.specialist_evaluation.models import EvaluationCandidate, EvaluationRun
from src.specialist_evaluation.runner import (
    FormalEvaluationCoordinator,
    build_formal_manifest_from_dataset,
    evaluate_real_model_preflight,
    verify_formal_pricing_snapshot,
)
from src.specialist_evaluation.store import (
    EvaluationInvariantError,
    PostgresSpecialistEvaluationStore,
    initialize_specialist_evaluation_schema,
)
from src.specialist_runtime.budget import PostgresModelBudgetStore, initialize_specialist_budget_schema
from src.specialist_runtime.deepseek_adapter import DeepSeekAgentModelAdapter


def main() -> int:
    """执行预检，且只有显式 --execute 才允许发起正式模型请求。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="在全部可信预检通过后运行正式评估")
    arguments = parser.parse_args()

    settings = get_settings()
    project_root = Path(__file__).parents[1]
    pricing_snapshot = (
        project_root / "evaluation" / "pricing" / "deepseek-v4-flash-2026-07-16.json"
    )
    root = Path(__file__).parents[1]
    manifest = build_formal_manifest_from_dataset(root / "evaluation", root)
    model_preflight = evaluate_real_model_preflight(
        api_key=settings.llm_api_key,
        # 预检必须接收完整 URL，才能拒绝 HTTP 降级、用户信息和意外 path/query。
        endpoint_host=settings.llm_api_base_url,
        model_id=settings.llm_model,
        pricing_snapshot_present=pricing_snapshot.is_file(),
    )
    pricing_preflight = verify_formal_pricing_snapshot(
        manifest=manifest,
        evaluation_root=root / "evaluation",
        pricing_snapshot_path=pricing_snapshot,
    )
    allowed = model_preflight.allowed and pricing_preflight.allowed
    if not allowed or not arguments.execute:
        # 不带 --execute 时明确标记“等待执行”，避免空候选输出被 CI 误认为评估成功。
        print(json.dumps({"status": "PREFLIGHT_PASSED_AWAITING_EXECUTION" if allowed else "PREFLIGHT_BLOCKED", "model_preflight": model_preflight.reason_code, "pricing_preflight": pricing_preflight.reason_code}, ensure_ascii=False, sort_keys=True))
        return 0 if allowed else 2

    # Git/源码授权在最后一刻签发，任何未提交 src/evaluation 改动都会在网络前 fail-closed。
    authorization = verify_formal_manifest_at_git_head(manifest, root)
    initialize_specialist_budget_schema(settings)
    initialize_specialist_evaluation_schema(settings)
    store = PostgresSpecialistEvaluationStore(settings)
    store.register_manifest(manifest, authorization=authorization)
    dataset = json.loads((root / "evaluation" / "manifests" / "phase13-v3.json").read_text(encoding="utf-8"))
    runner = build_formal_bounded_runner(
        evaluation_root=root / "evaluation",
        model_port=DeepSeekAgentModelAdapter(api_key=settings.llm_api_key),
        budget_store=PostgresModelBudgetStore(settings),
        pricing_policy=FrozenPricingPolicy(pricing=dataset["pricing"], policy_digest=manifest.price_policy_digest),
    )
    coordinator = FormalEvaluationCoordinator(store=store)
    reports = {}
    for candidate in EvaluationCandidate:
        run = EvaluationRun(run_id=f"{manifest.manifest_id}:{candidate.value.lower()}", manifest_id=manifest.manifest_id, manifest_digest=manifest.manifest_digest, candidate=candidate)
        try:
            store.create_run(run, authorization=authorization)
        except EvaluationInvariantError as error:
            # 同一 Manifest/Candidate 已有终局结论时，重放只能读取原证据并继续后续候选；
            # 绝不能为了“重新运行”创建新 Run 或重新向模型发送请求。
            if "retention decision already exists" not in str(error):
                raise
            decision = store.get_retention_decision(run.run_id)
            reports[candidate.value] = {"decision": decision.decision.value, "reason_code": decision.reason_code, "run_id": run.run_id}
            continue
        claim = store.claim_next_run("phase13-formal-cli", manifest_id=manifest.manifest_id)
        if claim is None or claim.run_id != run.run_id:
            raise RuntimeError("formal evaluation run claim failed")
        report = __import__("asyncio").run(
            coordinator.evaluate_candidate(run=run, claim=claim, slice_=build_formal_slice(candidate=candidate, evaluation_root=root / "evaluation", runner=runner, store=store))
        )
        reports[candidate.value] = {"decision": report.decision.decision.value, "reason_code": report.decision.reason_code, "run_id": run.run_id}
    # 输出中只保留门禁代码和候选结论，严禁将 API key、请求正文或模型响应写入终端。
    print(
        json.dumps(
            {
                "status": "FORMAL_EVALUATION_COMPLETED",
                "candidate_outcomes": reports,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
