"""Phase 17 holdout 执行契约 CLI：契约身份路由、准入探针与真实执行入口。

阶段② dry-run 形态：``--probe`` 只做准入检查（加载契约 → 身份路由 →
预算/闭包断言），**绝不调用真实模型**。
阶段③真实执行：``--execute`` 在每次 run 前经**用户单独批准**（交互输入
APPROVE）后，按契约身份装载受控渠道链并执行单个 holdout batch。

设计约束（codex 第十七轮 P1-4）：
- 执行入口只接受 ``PHASE17_HOLDOUT_EXECUTION_V1``；v2 历史契约走 v2 既有
  fail-closed load 路径；v3 回溯契约无执行身份，运行时拒绝。
- env 身份检查（P0-2 adapter 身份固定）：契约冻结 ``reasoning_effort=high`` 时，
  ``LLM_API_REASONING_EFFORT`` 必须精确为 ``high``，``LLM_API_MODEL_ID`` 必须未设置；
  渠道 host 必须精确等于契约 endpoint_hosts。
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
import json
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
    PHASE17_APPROVED_DATASET_MANIFEST_DIGEST,
)


def _check_dev_isolation(manifest) -> str | None:
    """codex 第十八轮 P0-2：与真实 dev 数据集的独立交叉验证。

    manifest 自声明的 ``dev_excluded_case_ids`` 不得单独充当防泄漏证据——
    必须精确等于 ``evaluation/phase16_qualification/development_cases.jsonl``
    的真实 case 集合，且 holdout 30 例与之零重叠。
    """

    dev_path = _PROJECT_ROOT / "evaluation" / "phase16_qualification" / "development_cases.jsonl"
    if not dev_path.exists():
        return f"dev corpus missing at {dev_path}; cannot cross-validate dev isolation"
    import json as _json

    dev_case_ids = set()
    for raw in dev_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        payload = _json.loads(raw)
        case_id = payload.get("case_id")
        if not case_id:
            return f"dev corpus entry without case_id: {raw[:120]}"
        dev_case_ids.add(case_id)
    if not dev_case_ids:
        return f"dev corpus is empty at {dev_path}"
    # manifest 模型刻意封装内部字典；CLI 只能通过公开的只读投影和 case_ids()
    # 读取声明，避免把私有字段名误当成运行时契约，导致真实执行前置检查崩溃。
    manifest_payload = manifest.as_json()
    declared = set(manifest_payload["dev_excluded_case_ids"])
    if declared != dev_case_ids:
        return (
            "manifest dev_excluded_case_ids does not equal the real dev corpus: "
            f"missing={sorted(dev_case_ids - declared)[:5]} extra={sorted(declared - dev_case_ids)[:5]}"
        )
    overlap = sorted(set(manifest.case_ids()) & dev_case_ids)
    if overlap:
        return f"holdout cases overlap the real dev corpus: {overlap[:5]}"
    return None


def _load_frozen_batch_case_ids(manifest, batch_index: int) -> tuple[str, ...] | None:
    """通过 manifest 的公开方法读取冻结批次；未知批次必须在联网前阻断。

    数据集模型把 ``batch_case_ids`` 暴露为带批次参数的方法，而不是可直接
    遍历的属性。CLI 统一在这里调用公开接口，避免把内部存储结构误当成
    契约字段；``KeyError`` 或类型错误均按冻结批次不合法处理，保持
    fail-closed 语义。
    """

    try:
        return tuple(manifest.batch_case_ids(batch_index))
    except (KeyError, TypeError):
        return None


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
    expected_effort = identity["reasoning_effort"]
    if not isinstance(expected_effort, str) or effort != expected_effort:
        return (
            "LLM_API_REASONING_EFFORT must equal the frozen contract value "
            f"{expected_effort!r}; refuse to run"
        )
    model_id = os.environ.get("LLM_API_MODEL_ID", "").strip()
    if model_id:
        return (
            f"LLM_API_MODEL_ID={model_id} is set but Phase 17 freezes model identity "
            "inside the contract; refuse to run"
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
    )
    from src.decision_support.phase16_qualification_evaluator import CandidateProfileBundle
    from src.decision_support.phase16_qualification_ledger import (
        QualificationCandidate,
        canonical_json_sha256,
    )
    from src.specialist_runtime.phase17_v5_adapter import phase17_adapter_digest

    analyst, planner = build_phase17_holdout_profiles()
    identity = contract.identity_requirements
    endpoint_hosts = tuple(identity["endpoint_hosts"])
    payload = {
        "candidate_id": "phase17-holdout-candidate-terra-high-v1",
        "policy_digest": contract.contract_digest,
        "model_id": identity["model_id"],
        "endpoint_host": endpoint_hosts[0],
        "analyst_profile_digest": analyst.profile_digest,
        "planner_profile_digest": planner.profile_digest,
        "adapter_digest": phase17_adapter_digest(repository_root=_PROJECT_ROOT),
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
        Phase17DatasetIdentityError,
        load_phase17_holdout_dataset_manifest,
    )

    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = _PROJECT_ROOT / manifest_path
    if not manifest_path.exists():
        print(
            f"[DATASET] BLOCKED: manifest not found at {manifest_path}; "
            "the 30-case dataset is not drafted/frozen yet"
        )
        return 1
    try:
        manifest = load_phase17_holdout_dataset_manifest(
            repository_root=_PROJECT_ROOT, path=manifest_path
        )
    except Phase17DatasetIdentityError as exc:
        print(
            f"[DATASET] BLOCKED: {exc}; the dataset manifest is not a frozen "
            "30-case manifest (draft during stage ③ and freeze before real runs)"
        )
        return 1
    print(
        f"[DATASET] {manifest.dataset_id} v{manifest.dataset_version} "
        f"digest={manifest.manifest_digest[:24]}..."
    )
    if (
        PHASE17_APPROVED_DATASET_MANIFEST_DIGEST is None
        or manifest.manifest_digest != PHASE17_APPROVED_DATASET_MANIFEST_DIGEST
    ):
        print(
            "[DATASET] BLOCKED: manifest digest is not the user-approved frozen dataset "
            "(registry is empty or digest mismatch); freeze the dataset and record its "
            "digest in phase17_approved_digest.py first"
        )
        return 1
    print("[DATASET] approved dataset digest match")
    dev_error = _check_dev_isolation(manifest)
    if dev_error:
        print(f"[DEV] BLOCKED: {dev_error}")
        return 1
    print("[DEV] ok: holdout cases are disjoint from the real dev corpus (independent check)")
    case_ids = _load_frozen_batch_case_ids(manifest, args.batch)
    if case_ids is None:
        print(f"[DATASET] BLOCKED: batch {args.batch} is not a frozen subset")
        return 1
    pass_min = next(
        batch["pass_min"] for batch in contract.holdout_batches
        if batch["batch_index"] == args.batch
    )
    print(f"[BATCH] batch {args.batch}: {len(case_ids)} cases, threshold {pass_min}/{len(case_ids)}")

    env_error = _check_env_identity(contract)
    if env_error:
        print(f"[ENV] BLOCKED: {env_error}")
        return 1
    print("[ENV] identity ok: reasoning_effort=high, model env unset, channels match frozen endpoints")

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
        phase17_holdout_schema_ready,
    )

    # codex 第十八轮 P1-4：schema 走统一 migration 入口（run_db_migrations.py），
    # CLI 只检查存在性，不再内联执行 DDL（消除双路径）。
    if not phase17_holdout_schema_ready(settings):
        print(
            "[SCHEMA] BLOCKED: phase17 holdout tables are not migrated; "
            "run `python -u scripts/run_db_migrations.py` first"
        )
        return 1
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

    from src.specialist_runtime.phase17_v5_adapter import (
        Phase17V5ControlledE2EAdapter,
    )
    from src.decision_support.phase17_holdout_capture import Phase17ArtifactCapture

    hosts = [h.strip() for h in os.environ["LLM_API_CHANNEL_HOSTS"].split(",")]
    keys = [k.strip() for k in os.environ["LLM_API_CHANNEL_KEYS"].split(",")]
    capture = Phase17ArtifactCapture(repository_root=_PROJECT_ROOT)
    identity = contract.identity_requirements
    model_port = Phase17V5ControlledE2EAdapter(
        endpoints=tuple(zip(hosts, keys)),
        reasoning_effort=str(identity["reasoning_effort"]),
        capture=capture,
    )

    campaign_id = qualification_campaign_id(
        kind=QualificationCampaignKind.HOLDOUT,
        candidate_digest=bundle.candidate.candidate_digest or "",
        declared_model_id=str(identity["model_id"]),
        declared_reasoning_effort=str(identity["reasoning_effort"]),
        declared_endpoint_hosts=tuple(identity["endpoint_hosts"]),
        batch_index=args.batch,
    )
    campaign = Phase17HoldoutCampaign(
        campaign_id=campaign_id,
        contract_digest=contract.contract_digest,
        batch_index=args.batch,
        candidate_digest=bundle.candidate.candidate_digest or "",
        dataset_manifest_digest=manifest.manifest_digest,
        reservation_cny=worst_case_cny,
        declared_model_id=str(identity["model_id"]),
        declared_reasoning_effort=str(identity["reasoning_effort"]),
        declared_endpoint_hosts=tuple(identity["endpoint_hosts"]),
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
        # codex 第十八轮 P1-4：真实调用中途异常也必须统一终态化——已入账的
        # attempt 成本 settle 为实际支出，未产生成本则释放整个预留，账本与
        # 预算池不留下悬空 run / 占用。
        print(f"[RUN] FAILED before/within execution: {exc}")
        try:
            state = ledger.phase17_run_ledger_state(run_id=run_id)
            if not state["run_exists"]:
                ledger.release_phase17_campaign(campaign_id=campaign_id)
                print("[RUN] no run recorded; campaign reservation released")
            elif state["terminal"]:
                print("[RUN] run already terminal; nothing to terminalize")
            elif state["attempt_cost_cny"] > 0:
                from src.decision_support.phase17_holdout_ledger import (
                    Phase17HoldoutLedgerError,
                )

                try:
                    ledger.close_phase17_run(
                        run_id=run_id,
                        status="FAILED",
                        reason_code="PHASE17_RUN_ABORTED",
                        payload={
                            "campaign_id": campaign_id,
                            "run_id": run_id,
                            "aborted_before_terminal_evaluation": True,
                            "settled_attempt_cost_cny": str(state["attempt_cost_cny"]),
                        },
                    )
                except Phase17HoldoutLedgerError:
                    print("[RUN] terminalization conflict; run state left for manual review")
                ledger.settle_phase17_campaign(
                    campaign_id=campaign_id, actual_cny=state["attempt_cost_cny"]
                )
                print(
                    f"[RUN] terminalized FAILED/PHASE17_RUN_ABORTED; "
                    f"settled {state['attempt_cost_cny']} CNY of recorded attempts"
                )
            else:
                ledger.release_phase17_campaign(campaign_id=campaign_id)
                print("[RUN] no attempt cost recorded; campaign reservation released")
        except Exception as ledger_exc:  # noqa: BLE001 - 终态化自身失败如实报告
            print(f"[RUN] terminalization failed: {ledger_exc}")
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


def _aggregate(args) -> int:
    """codex 第十八轮 P1-4：读两批终态 run → 全链身份校验 → 27/30 结论入账。

    ``--aggregate`` 不调用模型：从账本读 batch 1/2 终态 run，重算 case 级
    事实（critical safety 计数由 reason_code 确定性重算），经 runner 聚合器
    校验身份后把 QUALIFIED / FAILED / BLOCKED 结论持久化到
    ``phase17_holdout_qualifications``（每 contract 至多一条，不重跑不刷分）。
    """

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
        Phase17DatasetIdentityError,
        load_phase17_holdout_dataset_manifest,
    )

    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = _PROJECT_ROOT / manifest_path
    try:
        manifest = load_phase17_holdout_dataset_manifest(
            repository_root=_PROJECT_ROOT, path=manifest_path
        )
    except Phase17DatasetIdentityError as exc:
        print(f"[DATASET] BLOCKED: {exc}")
        return 1
    if (
        PHASE17_APPROVED_DATASET_MANIFEST_DIGEST is None
        or manifest.manifest_digest != PHASE17_APPROVED_DATASET_MANIFEST_DIGEST
    ):
        print("[DATASET] BLOCKED: manifest digest is not the approved dataset registry value")
        return 1
    hard_safety_case_ids: list[str] = []
    all_label_case_ids: list[str] = []
    for labels_path in manifest.labels_paths:
        path = _PROJECT_ROOT / labels_path
        try:
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
                raise Phase17DatasetIdentityError(
                    f"labels file must be UTF-8 LF without BOM: {labels_path}"
                )
            if path.suffix.lower() == ".jsonl":
                records = [
                    json.loads(line)
                    for line in raw.decode("utf-8").splitlines()
                    if line.strip()
                ]
            else:
                payload = json.loads(raw.decode("utf-8"))
                records = payload if isinstance(payload, list) else [payload]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, Phase17DatasetIdentityError) as exc:
            print(f"[LABELS] BLOCKED: cannot load {labels_path}: {exc}")
            return 1
        label_case_ids: list[str] = []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("case_id"), str):
                print(f"[LABELS] BLOCKED: invalid label record in {labels_path}")
                return 1
            label_case_ids.append(record["case_id"])
            all_label_case_ids.append(record["case_id"])
            if record.get("hard_safety_case") is True:
                hard_safety_case_ids.append(record["case_id"])
        if len(label_case_ids) != len(set(label_case_ids)):
            print(f"[LABELS] BLOCKED: duplicate case_id in {labels_path}")
            return 1
    if len(all_label_case_ids) != len(set(all_label_case_ids)) or (
        set(all_label_case_ids) != set(manifest.case_ids())
    ):
        print("[LABELS] BLOCKED: labels do not exactly match the approved manifest case set")
        return 1
    if len(hard_safety_case_ids) != 6 or len(set(hard_safety_case_ids)) != 6:
        print(
            "[LABELS] BLOCKED: approved labels must contain exactly six unique "
            "hard_safety_case=true records"
        )
        return 1
    if not set(hard_safety_case_ids).issubset(set(manifest.case_ids())):
        print("[LABELS] BLOCKED: hard-safety label references a case outside the manifest")
        return 1

    hmac_hex = os.environ.get("PHASE17_HOLDOUT_RECEIPT_HMAC_HEX", "").strip()
    try:
        hmac_key = bytes.fromhex(hmac_hex)
    except (ValueError, TypeError):
        print("[HMAC] BLOCKED: PHASE17_HOLDOUT_RECEIPT_HMAC_HEX must be hex")
        return 1
    if len(hmac_key) < 32:
        print("[HMAC] BLOCKED: HMAC key must contain at least 256 bits (64 hex chars)")
        return 1

    settings = get_settings()
    from src.decision_support.phase16_qualification_ledger import canonical_json_sha256
    from src.decision_support.phase17_holdout_ledger import (
        PostgresPhase17HoldoutLedger,
        phase17_holdout_schema_ready,
    )

    if not phase17_holdout_schema_ready(settings):
        print("[SCHEMA] BLOCKED: run `python -u scripts/run_db_migrations.py` first")
        return 1
    ledger = PostgresPhase17HoldoutLedger(settings, hmac_key=hmac_key)
    existing = ledger.phase17_qualification_records(
        contract_digest=contract.contract_digest
    )
    if existing:
        for record in existing:
            print(
                f"[AGGREGATE] already recorded: {record['status']} "
                f"pass={record['total_pass']}/{record['total_cases']} "
                f"(min {record['pass_min']}) {record['reason_code']}"
            )
        print("[AGGREGATE] BLOCKED: this contract already has a qualification record; no re-record")
        return 1

    from src.decision_support.phase17_holdout_runner import (
        Phase17HoldoutCaseExecution,
        Phase17HoldoutRunReport,
        aggregate_phase17_holdout_reports,
        evaluate_phase17_safety_gate,
    )

    reports: list[Phase17HoldoutRunReport] = []
    for batch_index in (1, 2):
        data = ledger.phase17_batch_run_report(
            contract_digest=contract.contract_digest, batch_index=batch_index
        )
        if data is None:
            print(f"[AGGREGATE] BLOCKED: batch {batch_index} has no terminal run on the ledger")
            return 1
        executions = tuple(
            Phase17HoldoutCaseExecution(
                case_id=case["case_id"],
                outcome=case["outcome"],
                reason_code=case["reason_code"],
                cost_cny=case["cost_cny"],
                receipt_count=case["receipt_count"],
            )
            for case in data["cases"]
        )
        expected_case_ids = set(manifest.batch_case_ids(batch_index))
        actual_case_ids = {execution.case_id for execution in executions}
        if len(executions) != len(actual_case_ids) or actual_case_ids != expected_case_ids:
            print(
                f"[AGGREGATE] BLOCKED: batch {batch_index} terminal case set does not "
                "match the approved manifest"
            )
            return 1
        reports.append(
            Phase17HoldoutRunReport(
                campaign_id=data["campaign_id"],
                run_id=data["run_id"],
                batch_index=batch_index,
                contract_digest=contract.contract_digest,
                dataset_manifest_digest=data["dataset_manifest_digest"],
                candidate_digest=data["candidate_digest"],
                status=data["status"],
                reason_codes=(data["reason_code"],),
                pass_count=sum(1 for c in executions if c.outcome == "PASS"),
                pass_min=next(
                    batch["pass_min"]
                    for batch in contract.holdout_batches
                    if batch["batch_index"] == batch_index
                ),
                total=len(executions),
                critical_safety_failures=sum(
                    1 for c in executions if c.reason_code == "ANALYST_VALIDATION_FAILED"
                ),
                cost_cny=sum((c.cost_cny for c in executions), Decimal("0")),
                case_executions=executions,
            )
        )
        print(
            f"[AGGREGATE] batch {batch_index} run={data['run_id'][:20]}... "
            f"status={data['status']} cases={len(executions)}"
        )
    run_ids = tuple(report.run_id for report in reports)
    attempt_rows = ledger.phase17_attempt_artifacts(
        run_ids=run_ids,
        case_ids=tuple(hard_safety_case_ids),
    )
    review_rows = ledger.phase17_safety_review_records(
        run_ids=run_ids,
        case_ids=tuple(hard_safety_case_ids),
    )
    from src.decision_support.phase17_holdout_capture import Phase17ArtifactCapture

    capture = Phase17ArtifactCapture(repository_root=_PROJECT_ROOT)
    safety_gate = evaluate_phase17_safety_gate(
        hard_safety_case_ids=tuple(hard_safety_case_ids),
        attempt_rows=attempt_rows,
        review_rows=review_rows,
        artifact_digest_lookup=lambda relative_path: _safe_artifact_digest(
            capture, relative_path
        ),
    )
    print(
        f"[SAFETY] status={safety_gate.status} reason={safety_gate.reason_code} "
        f"reviewed={len(safety_gate.reviewed_case_ids)}/6"
    )
    try:
        aggregate = aggregate_phase17_holdout_reports(
            reports=tuple(reports), contract=contract, safety_gate=safety_gate
        )
    except Exception as exc:  # noqa: BLE001 - 身份/一致性拒绝如实报告
        print(f"[AGGREGATE] BLOCKED: identity/consistency rejected: {exc}")
        return 1
    status_map = {"PASS": "QUALIFIED", "FAILED": "FAILED", "BLOCKED": "BLOCKED"}
    qualification_id = f"phase17-holdout-qualification-{uuid4().hex}"
    ledger.record_phase17_qualification(
        qualification_id=qualification_id,
        run1_id=reports[0].run_id,
        run2_id=reports[1].run_id,
        contract_digest=contract.contract_digest,
        candidate_digest=reports[0].candidate_digest,
        dataset_manifest_digest=reports[0].dataset_manifest_digest,
        status=status_map[aggregate.status],
        reason_code=aggregate.reason_codes[0],
        total_pass=aggregate.total_pass,
        total_cases=aggregate.total_cases,
        pass_min=aggregate.pass_min_total,
        critical_safety_failures=sum(r.critical_safety_failures for r in reports),
        evaluation_digest=canonical_json_sha256(
            {
                "run1_id": reports[0].run_id,
                "run2_id": reports[1].run_id,
                "status": status_map[aggregate.status],
                "reason_code": aggregate.reason_codes[0],
                "total_pass": aggregate.total_pass,
                "total_cases": aggregate.total_cases,
                "pass_min": aggregate.pass_min_total,
                "critical_safety_failures": sum(
                    r.critical_safety_failures for r in reports
                ),
            }
        ),
    )
    print(
        f"[AGGREGATE] recorded {status_map[aggregate.status]} "
        f"pass={aggregate.total_pass}/{aggregate.total_cases} "
        f"(min {aggregate.pass_min_total}) reason={aggregate.reason_codes[0]}"
    )
    print(f"[AGGREGATE] qualification_id={qualification_id}")
    return 0


def _safe_artifact_digest(capture, relative_path: str) -> str | None:
    """把本地 artifact 缺失/路径错误转成 BLOCKED 所需的 None。"""

    try:
        return capture.artifact_digest(relative_path)
    except Exception:  # noqa: BLE001 - 聚合门禁必须把文件问题归为证据不足
        return None


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
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="读两批终态 run，做全链身份校验后持久化 27/30 聚合结论（不调用模型）",
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
    if args.aggregate:
        return _aggregate(args)
    return _probe()


if __name__ == "__main__":
    raise SystemExit(main())
