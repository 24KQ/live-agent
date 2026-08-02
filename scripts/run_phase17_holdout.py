"""Phase 17 holdout 执行契约 CLI：契约身份路由、准入探针与真实执行入口。

阶段② dry-run 形态：``--probe`` 只做准入检查（加载契约 → 身份路由 →
预算/闭包断言），**绝不调用真实模型**。
阶段③真实执行：``--execute`` 在每次 run 前经**用户单独批准**（交互输入
APPROVE）后，按契约身份装载受控渠道链并执行单个 holdout batch。

设计约束（codex 第十七轮 P1-4）：
- 执行入口只接受 ``PHASE17_HOLDOUT_EXECUTION_V1``；v2 历史契约走 v2 既有
  fail-closed load 路径；v3 回溯契约无执行身份，运行时拒绝。
- env 身份检查（P0-2 adapter 身份固定）：契约 ``reasoning_effort=null`` 时
  ``LLM_API_REASONING_EFFORT`` 必须未设置；``LLM_API_MODEL_ID`` 未设置或
  == gpt-5.6-luna；渠道 host 必须精确等于契约 endpoint_hosts。
- 不读取/不打印 .env 内容；不打印任何 API key；不触碰 v2/v3 manifest。
- 输入文件约定：``{inputs_root}/{case_id}.txt``（UTF-8/LF 无 BOM）；runner
  按 frozen manifest 的 input_digest 校验。
- 文档 UTF-8/LF/无 BOM。

用法：
    python -u scripts/run_phase17_holdout.py --probe
    python -u scripts/run_phase17_holdout.py --reject-v2-identity
    python -u scripts/run_phase17_holdout.py --execute --batch 1 \\
        --manifest evaluation/phase17_holdout/manifests/phase17-holdout-cases-v1.json
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.decision_support.phase16_qualification import (  # noqa: E402
    QualificationExecutionContract,
    admit_phase17_holdout_execution,
    load_phase17_holdout_execution_contract,
)
from src.decision_support.phase17_approved_digest import (  # noqa: E402
    PHASE17_APPROVED_CONTRACT_DIGEST,
)


def _probe() -> int:
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    print(f"[phase17] contract_id={contract.contract_id} digest={contract.contract_digest}")
    print(f"[phase17] approved_registry_digest={PHASE17_APPROVED_CONTRACT_DIGEST}")
    print(f"[phase17] parent_v3={contract.parent_v3_evaluation_digest[:12]}...")
    print(
        f"[phase17] budget project={contract.project_budget_cny} "
        f"retrospective={contract.retrospective_budget_actual_cny} "
        f"forward_remaining={contract.forward_budget_remaining_cny}"
    )
    identity = contract.identity_requirements
    print(
        f"[phase17] identity provider={identity['provider_id']} "
        f"model={identity['model_id']} endpoints={','.join(identity['endpoint_hosts'])} "
        f"reasoning={identity['reasoning_effort']} json_mode={identity['json_mode']} "
        f"tokens={identity['max_total_tokens']}/{identity['max_output_tokens']} "
        f"deadline={identity['per_attempt_deadline_seconds']}s"
    )
    batches = ", ".join(
        f"batch{b['batch_index']}:{b['case_count']}cases(min{b['pass_min']})"
        for b in contract.holdout_batches
    )
    print(
        f"[phase17] holdout={contract.holdout_case_count} cases "
        f"[{batches}] total_pass_min={contract.holdout_total_e2e_pass_min}"
    )
    allowed, reasons = admit_phase17_holdout_execution(
        requested_identity=QualificationExecutionContract.PHASE17_HOLDOUT_EXECUTION_V1,
        contract=contract,
    )
    if not allowed:
        print(f"[phase17] ADMISSION_REJECTED reasons={','.join(reasons)}")
        return 1
    print("[phase17] ADMISSION_OK (dry-run, no model call)")
    print("[phase17] identity routing: PHASE17_HOLDOUT_EXECUTION_V1 accepted; "
          "V2_HISTORICAL_EXECUTION via v2 fail-closed; v3 read-only rejected")
    return 0


def _reject_wrong_identity() -> int:
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    allowed, reasons = admit_phase17_holdout_execution(
        requested_identity=QualificationExecutionContract.V2_HISTORICAL_EXECUTION,
        contract=contract,
    )
    print(
        f"[phase17] v2 identity -> allowed={allowed} reasons={','.join(reasons)} "
        "(expected: rejected, 历史入口不接受 phase17 契约)"
    )
    return 0 if not allowed else 1


def _check_env_identity(contract) -> str | None:
    """adapter 层身份固定（codex 第十七轮 P0-2）：联网前检查 env 注入。"""

    identity = contract.identity_requirements
    effort = os.environ.get("LLM_API_REASONING_EFFORT", "").strip()
    if effort:
        return (
            f"LLM_API_REASONING_EFFORT={effort} is set but the contract freezes "
            f"reasoning_effort={identity['reasoning_effort']} (null); refuse to run"
        )
    model_id = os.environ.get("LLM_API_MODEL_ID", "").strip()
    if model_id and model_id != identity["model_id"]:
        return (
            f"LLM_API_MODEL_ID={model_id} does not match the frozen model "
            f"identity {identity['model_id']}"
        )
    hosts = tuple(
        host.strip() for host in os.environ.get("LLM_API_CHANNEL_HOSTS", "").split(",") if host.strip()
    )
    keys = tuple(
        key.strip() for key in os.environ.get("LLM_API_CHANNEL_KEYS", "").split(",") if key.strip()
    )
    if not hosts:
        return "LLM_API_CHANNEL_HOSTS is not set (comma-separated, priority order)"
    if len(hosts) != len(keys):
        return "LLM_API_CHANNEL_HOSTS / LLM_API_CHANNEL_KEYS count mismatch"
    if not all(keys):
        return "every channel needs a non-empty API key"
    if tuple(hosts) != tuple(identity["endpoint_hosts"]):
        return (
            f"channel hosts {hosts} do not match the frozen endpoints "
            f"{tuple(identity['endpoint_hosts'])}"
        )
    return None


def _build_candidate_bundle(contract):
    """phase17 专用 candidate：policy_digest 绑定契约 digest，profiles 用契约身份。"""

    from src.decision_support.phase16_qualification_candidate import (
        build_phase17_holdout_profiles,
        qualification_adapter_digest,
    )
    from src.decision_support.phase16_qualification_evaluator import CandidateProfileBundle
    from src.decision_support.phase16_qualification_ledger import (
        QualificationCandidate,
        canonical_json_sha256,
    )

    analyst, planner = build_phase17_holdout_profiles()
    payload = {
        "candidate_id": "phase17-holdout-candidate-luna-v1",
        "policy_digest": contract.contract_digest,
        "model_id": "gpt-5.6-luna",
        "endpoint_host": "synapse-ai.uk",
        "analyst_profile_digest": analyst.profile_digest,
        "planner_profile_digest": planner.profile_digest,
        "adapter_digest": qualification_adapter_digest(repository_root=_PROJECT_ROOT),
    }
    candidate = QualificationCandidate(
        candidate_id=payload["candidate_id"],
        policy_digest=payload["policy_digest"],
        model_id=payload["model_id"],
        endpoint_host=payload["endpoint_host"],
        analyst_profile_digest=payload["analyst_profile_digest"],
        planner_profile_digest=payload["planner_profile_digest"],
        adapter_digest=payload["adapter_digest"],
        candidate_digest=canonical_json_sha256(payload),
    )
    print(f"[CANDIDATE] {candidate.candidate_id} digest={candidate.candidate_digest[:24]}...")
    return CandidateProfileBundle(
        candidate=candidate, analyst_profile=analyst, planner_profile=planner
    )


def _execute(args) -> int:
    from src.config.settings import get_settings

    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    allowed, reasons = admit_phase17_holdout_execution(
        requested_identity=QualificationExecutionContract.PHASE17_HOLDOUT_EXECUTION_V1,
        contract=contract,
    )
    if not allowed:
        print(f"[phase17] ADMISSION_REJECTED reasons={','.join(reasons)}")
        return 1

    from src.decision_support.phase17_holdout_dataset import (
        load_phase17_holdout_dataset_manifest,
    )

    manifest = load_phase17_holdout_dataset_manifest(
        repository_root=_PROJECT_ROOT, path=Path(args.manifest)
    )
    print(
        f"[DATASET] {manifest.dataset_id} v{manifest.dataset_version} "
        f"digest={manifest.manifest_digest[:24]}..."
    )
    if args.batch not in manifest.batch_case_ids:
        print(f"[DATASET] BLOCKED: batch {args.batch} is not a frozen subset")
        return 1
    case_ids = tuple(manifest.batch_case_ids(args.batch))
    pass_min = next(
        batch["pass_min"] for batch in contract.holdout_batches
        if batch["batch_index"] == args.batch
    )
    print(f"[BATCH] batch {args.batch}: {len(case_ids)} cases, threshold {pass_min}/{len(case_ids)}")

    env_error = _check_env_identity(contract)
    if env_error:
        print(f"[ENV] BLOCKED: {env_error}")
        return 1
    print("[ENV] identity ok: reasoning_effort unset, model ok, channels match frozen endpoints")

    hmac_hex = os.environ.get("PHASE17_HOLDOUT_RECEIPT_HMAC_HEX", "").strip()
    try:
        hmac_key = bytes.fromhex(hmac_hex)
    except (ValueError, TypeError):
        print("[HMAC] BLOCKED: PHASE17_HOLDOUT_RECEIPT_HMAC_HEX must be hex")
        return 1
    if len(hmac_key) < 32:
        print("[HMAC] BLOCKED: HMAC key must contain at least 256 bits (64 hex chars)")
        return 1

    bundle = _build_candidate_bundle(contract)
    settings = get_settings()
    from src.decision_support.phase16_qualification_ledger import (
        QualificationCampaignKind,
        qualification_campaign_id,
    )
    from src.decision_support.phase17_holdout_ledger import (
        Phase17HoldoutCampaign,
        PostgresPhase17HoldoutLedger,
        initialize_phase17_holdout_schema,
    )

    initialize_phase17_holdout_schema(settings)
    ledger = PostgresPhase17HoldoutLedger(settings, hmac_key=hmac_key)
    ledger.ensure_phase17_contract(contract)
    pool = ledger.budget_pool_state(contract.contract_digest)

    # 最坏情况预算预检：case × 2 阶段 × stage 级预留；池内可用余额核对。
    worst_case_cny = Decimal(len(case_ids)) * Decimal("2") * contract.stage_reservation_cny
    print(
        f"[BUDGET] project={pool['project_budget_cny']} forward={pool['forward_budget_remaining_cny']} "
        f"reserved={pool['reserved_cny']} settled={pool['settled_cny']} "
        f"available={pool['available_cny']}"
    )
    print(
        f"[BUDGET] worst-case batch cost={worst_case_cny} "
        f"(cases={len(case_ids)} × 2 stages × {contract.stage_reservation_cny}/attempt)"
    )
    if worst_case_cny > pool["available_cny"]:
        print("[BUDGET] BLOCKED: worst-case cost exceeds available pool")
        return 1

    # 交互式人工批准（纪律：每次真实 run 前用户单独批准；EOF 即拒绝）。
    try:
        answer = input(
            f"[APPROVAL] confirm real model run for batch {args.batch} "
            f"({len(case_ids)} cases, worst-case {worst_case_cny} CNY)? "
            "type APPROVE to continue: "
        ).strip()
    except EOFError:
        answer = ""
    if answer != "APPROVE":
        print("[APPROVAL] declined; no model call made")
        return 1
    print("[APPROVAL] confirmed")

    from src.decision_support.controlled_e2e_adapter_v5 import DeepSeekV5ControlledE2EAdapter

    hosts = [h.strip() for h in os.environ["LLM_API_CHANNEL_HOSTS"].split(",")]
    keys = [k.strip() for k in os.environ["LLM_API_CHANNEL_KEYS"].split(",")]
    model_port = DeepSeekV5ControlledE2EAdapter(endpoints=tuple(zip(hosts, keys)))

    campaign_id = qualification_campaign_id(
        kind=QualificationCampaignKind.HOLDOUT,
        candidate_digest=bundle.candidate.candidate_digest or "",
        declared_model_id="gpt-5.6-luna",
        declared_reasoning_effort=None,
        declared_endpoint_hosts=("synapse-ai.uk",),
        batch_index=args.batch,
    )
    campaign = Phase17HoldoutCampaign(
        campaign_id=campaign_id,
        contract_digest=contract.contract_digest,
        batch_index=args.batch,
        candidate_digest=bundle.candidate.candidate_digest or "",
        dataset_manifest_digest=manifest.manifest_digest,
        reservation_cny=worst_case_cny,
        declared_model_id="gpt-5.6-luna",
        declared_reasoning_effort=None,
        declared_endpoint_hosts=("synapse-ai.uk",),
    )
    run_id = f"phase17-holdout-{uuid4().hex}"

    # 输入文件约定：{inputs_root}/{case_id}.txt（UTF-8/LF 无 BOM）。
    inputs_root = _PROJECT_ROOT / manifest.inputs_root
    cases: list[tuple[str, str]] = []
    for case_id in case_ids:
        path = inputs_root / f"{case_id}.txt"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            print(f"[INPUT] BLOCKED: cannot read {path}: {exc}")
            return 1
        if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
            print(f"[INPUT] BLOCKED: {path} must be UTF-8 LF without BOM")
            return 1
        cases.append((case_id, raw.decode("utf-8")))

    from src.decision_support.phase17_holdout_runner import (
        Phase17HoldoutCampaignRunner,
    )

    runner = Phase17HoldoutCampaignRunner(
        contract=contract,
        ledger=ledger,
        candidate_bundle=bundle,
        model_port=model_port,
    )
    try:
        report = asyncio.run(
            runner.execute(
                campaign=campaign,
                run_id=run_id,
                batch_index=args.batch,
                cases=tuple(cases),
                manifest=manifest,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 前置失败如实报告，不吞没
        print(f"[RUN] FAILED before/within execution: {exc}")
        return 1

    print(
        f"[RUN] run_id={report.run_id} status={report.status} "
        f"pass={report.pass_count}/{report.total} (min {report.pass_min}) "
        f"cost={report.cost_cny} CNY"
    )
    for execution in report.case_executions:
        print(
            f"      {execution.case_id}: {execution.outcome} "
            f"({execution.reason_code}) cost={execution.cost_cny}"
        )
    after = ledger.budget_pool_state(contract.contract_digest)
    print(
        f"[POOL] after run: reserved={after['reserved_cny']} settled={after['settled_cny']} "
        f"available={after['available_cny']}"
    )
    if after["settled_cny"] != pool["settled_cny"] + report.cost_cny:
        print("[POOL] MISMATCH: settled delta does not equal reported cost")
        return 1
    print("[POOL] ok: settled delta == reported cost")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="dry-run 契约加载与身份路由准入检查（不调用模型）",
    )
    parser.add_argument(
        "--reject-v2-identity",
        action="store_true",
        help="验证 v2 历史执行身份在 phase17 入口被拒绝",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真实执行单个 holdout batch（需交互 APPROVE 批准）",
    )
    parser.add_argument("--batch", type=int, choices=[1, 2], default=1)
    parser.add_argument(
        "--manifest",
        default="evaluation/phase17_holdout/manifests/phase17-holdout-cases-v1.json",
        help="冻结的 holdout dataset manifest 路径（仓库相对）",
    )
    args = parser.parse_args()
    if args.reject_v2_identity:
        return _reject_wrong_identity()
    if args.execute:
        return _execute(args)
    return _probe()


if __name__ == "__main__":
    raise SystemExit(main())
