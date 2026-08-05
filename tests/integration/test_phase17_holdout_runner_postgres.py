"""Phase 17 holdout 执行器端到端离线链 PostgreSQL 集成测试。

用脚本化 fake model port（不联网）驱动 contract → ledger → runner 全链：
- 全 pass / 阈值边界（9/10 达标、8/10 未达标）/ hard block 三路径；
- 精确 batch 集合校验（子集/混合集合在账本写入前拒绝）；
- 全链身份绑定（campaign/candidate/manifest 任一漂移在联网前 fail-closed）；
- 逐 attempt 证据（phase17_holdout_attempts 行 + receipt_hmac）与 case 聚合对账；
- UNKNOWN_USAGE 按最坏情况以 stage 级预留全额入账；
- case membership 校验失败在写入任何账本行之前阻断。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from src.config.settings import get_settings
from src.decision_support.phase16_qualification import (
    load_phase17_holdout_execution_contract,
)
from src.decision_support.phase16_qualification_evaluator import CandidateProfileBundle
from src.decision_support.phase16_qualification_ledger import (
    QualificationCampaignKind,
    QualificationCandidate,
    canonical_json_sha256,
    qualification_campaign_id,
)
from src.decision_support.phase17_holdout_dataset import (
    Phase17HoldoutDatasetManifest,
)
from src.decision_support.phase17_holdout_ledger import (
    Phase17HoldoutCampaign,
    PostgresPhase17HoldoutLedger,
    initialize_phase17_holdout_schema,
)
from src.decision_support.phase17_holdout_runner import (
    Phase17HoldoutCampaignRunner,
    Phase17HoldoutExecutionError,
    Phase17HoldoutRunReport,
    Phase17SafetyGateResult,
    aggregate_phase17_holdout_reports,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelRequest,
    ModelSuccess,
    ModelUsage,
)
from src.specialist_runtime.models import (
    SpecialistTaskKind,
)
from src.specialist_runtime.phase17_v5_adapter import (
    Phase17AdapterOutcome,
    Phase17AttemptDetail,
    phase17_adapter_digest,
)
from src.specialist_runtime.profiles import SpecialistProfile


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_HMAC_KEY = bytes.fromhex("d3" * 32)
_RUN_ID = "phase17-holdout-run-9001"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _case_input(case_id: str) -> str:
    """为离线 fake 链提供带真实可见证据 ID 的原始 case 输入。

    运行器现在会把模型 evidence_ids 与原始输入中的 ID 做硬子集校验，
    所以测试输入必须显式携带一个合成证据 ID，不能再依赖只在 fake 输出中
    存在的 ``synthetic-evidence-001`` 占位值。
    """

    return f"input-{case_id} SYN-P-9001"


def _build_manifest() -> Phase17HoldoutDatasetManifest:
    """合成 30 例 manifest（与 unit fixture 同构，不触碰真实数据集）。"""
    case_ids = [f"holdout-case-{i:03d}" for i in range(1, 31)]
    payload = {
        "dataset_id": "phase17-holdout-cases-v1",
        "dataset_version": "1.0.0",
        "split": "HOLDOUT",
        "case_count": 30,
        "case_id_to_input_digest": {cid: _digest(_case_input(cid)) for cid in case_ids},
        "batch_case_ids": {"1": case_ids[:10], "2": case_ids[10:]},
        "dev_excluded_case_ids": ["development-case-001"],
        "inputs_root": "evaluation/phase17_holdout/inputs",
        "labels_root": "evaluation/phase17_holdout/labels",
        "labels_paths": ["evaluation/phase17_holdout/labels/labels-v1.json"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["manifest_digest"] = hashlib.sha256(raw).hexdigest()
    return Phase17HoldoutDatasetManifest(**payload)


def _profile(
    *,
    task_kind: SpecialistTaskKind,
    model_id: str = "gpt-5.6-terra",
    endpoint_host: str = "synapse-ai.uk",
) -> SpecialistProfile:
    prompt_text = f"phase17 {task_kind.value} system prompt"
    result_schema = {"type": "object"}
    return SpecialistProfile(
        profile_id=f"phase17-{task_kind.value.lower()}",
        profile_version="1.0.0",
        task_kind=task_kind,
        model_id=model_id,
        endpoint_host=endpoint_host,
        temperature=Decimal("0"),
        prompt_text=prompt_text,
        prompt_hash=_digest(prompt_text),
        result_schema_hash=_digest(json.dumps(result_schema, sort_keys=True, separators=(",", ":"))),
        result_schema=result_schema,
        max_model_calls=2,
        max_skill_calls=0,
        max_total_tokens=8000,
        max_output_tokens=2800,
        deadline_seconds=90,
        max_case_cost_cny=Decimal("0.100000"),
    )


def _bundle(
    *,
    contract,
    model_id: str = "gpt-5.6-terra",
    endpoint_host: str = "synapse-ai.uk",
) -> CandidateProfileBundle:
    analyst = _profile(
        task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
        model_id=model_id,
        endpoint_host=endpoint_host,
    )
    planner = _profile(
        task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING,
        model_id=model_id,
        endpoint_host=endpoint_host,
    )
    payload = {
        "candidate_id": "phase17-candidate-terra-high-v1",
        "policy_digest": contract.contract_digest,
        "model_id": model_id,
        "endpoint_host": endpoint_host,
        "analyst_profile_digest": analyst.profile_digest,
        "planner_profile_digest": planner.profile_digest,
        # 18 轮后 runner 校验 adapter digest 必须等于当前 phase17 adapter 源码 digest。
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
    return CandidateProfileBundle(
        candidate=candidate,
        analyst_profile=analyst,
        planner_profile=planner,
    )


def _campaign(
    *,
    contract,
    manifest: Phase17HoldoutDatasetManifest,
    bundle: CandidateProfileBundle,
    batch_index: int = 1,
) -> Phase17HoldoutCampaign:
    campaign_id = qualification_campaign_id(
        kind=QualificationCampaignKind.HOLDOUT,
        candidate_digest=bundle.candidate.candidate_digest or "",
        declared_model_id="gpt-5.6-terra",
        declared_reasoning_effort="high",
        declared_endpoint_hosts=("synapse-ai.uk",),
        batch_index=batch_index,
    )
    return Phase17HoldoutCampaign(
        campaign_id=campaign_id,
        contract_digest=contract.contract_digest,
        batch_index=batch_index,
        candidate_digest=bundle.candidate.candidate_digest or "",
        dataset_manifest_digest=manifest.manifest_digest,
        reservation_cny=Decimal("1.000000"),
        declared_model_id="gpt-5.6-terra",
        declared_reasoning_effort="high",
        declared_endpoint_hosts=("synapse-ai.uk",),
    )


class _ScriptedModelPort:
    """按调用序号放脚本的 fake port；记录收到的请求供断言。"""

    def __init__(self, plan: tuple[str, ...]) -> None:
        self._plan = plan
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest):
        self.requests.append(request)
        kind = self._plan[len(self.requests) - 1]
        if kind == "PASS":
            stage = "ANALYST" if request.messages[-1].content.startswith("input-") else "PLANNER"
            output = _analyst_fake_output() if stage == "ANALYST" else _planner_fake_output()
            return Phase17AdapterOutcome(
                outcome=ModelSuccess(
                    request_id=request.request_id,
                    model_id=request.model_id,
                    output=output,
                    usage=ModelUsage(input_tokens=1000, output_tokens=500, total_tokens=1500),
                    response_digest="a" * 64,
                    latency_ms=Decimal("0"),
                    endpoint_host=request.endpoint_host,
                ),
                attempt_details=(),
            )
        if kind == "SEMANTIC_FAIL":
            # 已联网但冻结 FINAL envelope 的 result 字段不完整，模拟语义校验失败。
            return Phase17AdapterOutcome(
                outcome=ModelSuccess(
                    request_id=request.request_id,
                    model_id=request.model_id,
                    output=_final_envelope({}),
                    usage=ModelUsage(input_tokens=1000, output_tokens=500, total_tokens=1500),
                    response_digest="b" * 64,
                    latency_ms=Decimal("0"),
                    endpoint_host=request.endpoint_host,
                ),
                attempt_details=(),
            )
        if kind == "BLOCK":
            return Phase17AdapterOutcome(
                outcome=ModelFailure(
                    request_id=request.request_id,
                    category=ModelFailureCategory.TRANSPORT_ERROR,
                    request_sent=False,
                ),
                attempt_details=(),
            )
        if kind == "RETRY_OK":
            # 18 轮 P0-3：第一次网络尝试失败（TRANSPORT_ERROR）后同端点重试成功，
            # 返回 attempt_details 两行（FAILED → PASS）验证逐 attempt 入账。
            return Phase17AdapterOutcome(
                outcome=ModelSuccess(
                    request_id=request.request_id,
                    model_id=request.model_id,
                    output=(
                        _analyst_fake_output()
                        if request.messages[-1].content.startswith("input-")
                        else _planner_fake_output()
                    ),
                    usage=ModelUsage(input_tokens=1000, output_tokens=500, total_tokens=1500),
                    response_digest="c" * 64,
                    latency_ms=Decimal("0"),
                    endpoint_host=request.endpoint_host,
                    attempts=2,
                ),
                attempt_details=(
                    Phase17AttemptDetail(
                        attempt_index=1,
                        endpoint_host=request.endpoint_host,
                        outcome="FAILED",
                        category=ModelFailureCategory.TRANSPORT_ERROR,
                        http_status=None,
                        latency_ms=Decimal("0"),
                        response_digest=None,
                        provider_response_id=None,
                        input_tokens=None,
                        output_tokens=None,
                        total_tokens=None,
                    ),
                    Phase17AttemptDetail(
                        attempt_index=2,
                        endpoint_host=request.endpoint_host,
                        outcome="PASS",
                        category=None,
                        http_status=None,
                        latency_ms=Decimal("0"),
                        response_digest="c" * 64,
                        provider_response_id="retry-ok-receipt",
                        input_tokens=1000,
                        output_tokens=500,
                        total_tokens=1500,
                    ),
                ),
            )
        raise AssertionError(f"unexpected plan entry: {kind}")


@pytest.fixture()
def runner_env():
    """独立 schema + ledger + 真实契约 + 合成 manifest 的 phase17 执行环境。"""
    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase17_holdout_runner_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    initialize_phase17_holdout_schema(settings)
    ledger = PostgresPhase17HoldoutLedger(settings, hmac_key=_TEST_HMAC_KEY)
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    manifest = _build_manifest()
    bundle = _bundle(contract=contract)
    env = SimpleNamespace(
        settings=settings,
        ledger=ledger,
        contract=contract,
        manifest=manifest,
        bundle=bundle,
    )
    try:
        yield env
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
            connection.commit()


def _query(settings, statement: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    with psycopg.connect(
        **settings.postgres_connection_kwargs, row_factory=dict_row
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            return cursor.fetchall()


def _batch_one_cases(manifest: Phase17HoldoutDatasetManifest) -> tuple[tuple[str, str], ...]:
    return tuple(
        (case_id, _case_input(case_id)) for case_id in manifest.batch_case_ids(1)
    )


def _final_envelope(final_output: dict[str, object]) -> dict[str, object]:
    """生成与冻结 Phase 17 prompt 完全一致的模型 FINAL envelope fake。"""

    return {"kind": "FINAL", "final_output": final_output}


def _analyst_fake_output() -> dict[str, object]:
    """返回通过冻结 Analyst result schema 的最小合成结果。"""

    return _final_envelope(
        {
            "constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED"],
            "risk_codes": ["INVENTORY_CONFLICT_REQUIRES_REVIEW"],
            "explanation": "synthetic evidence requires operator confirmation",
            "evidence_ids": ["SYN-P-9001"],
        }
    )


def _planner_fake_output() -> dict[str, object]:
    """返回通过冻结 Planner result schema 的最小合成 option。"""

    return _final_envelope(
        {
            "options": [
                {
                    "option_id": "hold-current",
                    "product_strategy": "HOLD_AND_ESCALATE",
                    "backup_product_id": None,
                    "host_prompt": "请人工确认后再处理",
                    "timing": "AFTER_OPERATOR_CONFIRMATION",
                    "risk_flags": [
                        "INVENTORY_CONFLICT_REQUIRES_REVIEW",
                        "HUMAN_CONFIRMATION_REQUIRED",
                    ],
                    "evidence_ids": ["SYN-P-9001"],
                }
            ]
        }
    )


def _execute(runner, env, cases):
    """同步入口：await 一次 execute 并返回报告（仓库既有 asyncio.run 模式）。"""
    return asyncio.run(
        runner.execute(
            campaign=_campaign(
                contract=env.contract,
                manifest=env.manifest,
                bundle=env.bundle,
            ),
            run_id=_RUN_ID,
            batch_index=1,
            cases=cases,
            manifest=env.manifest,
        )
    )


def test_phase17_runner_end_to_end_pass(runner_env) -> None:
    """全 pass：run 终态 PASS、结算按实际成本、逐 attempt 证据入账。"""
    cases = _batch_one_cases(runner_env.manifest)
    port = _ScriptedModelPort(("PASS",) * (len(cases) * 2))
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=runner_env.bundle,
        model_port=port,
    )
    report = _execute(runner, runner_env, cases)

    assert report.status == "PASS"
    assert report.reason_codes == ("EXECUTION_COMPLETE",)
    assert report.pass_count == 10 and report.total == 10
    assert report.pass_min == 9
    assert report.cost_cny == Decimal("0.120000")  # 10 例 × 2 阶段 × (3M in + 6M out)/1M
    first_planner_input = json.loads(port.requests[1].messages[-1].content)
    assert set(first_planner_input["analysis"]) == {
        "constraint_codes",
        "risk_codes",
        "explanation",
        "evidence_ids",
    }
    assert "kind" not in first_planner_input["analysis"]

    rows = _query(
        runner_env.settings,
        "SELECT status, reason_code FROM phase17_holdout_run_results WHERE run_id=%s",
        (_RUN_ID,),
    )
    assert rows == [{"status": "PASS", "reason_code": "PHASE17_HOLDOUT_BATCH_THRESHOLD_MET"}]
    case_rows = _query(
        runner_env.settings,
        "SELECT outcome, receipt_count, cost_cny FROM phase17_holdout_case_results"
        " WHERE run_id=%s ORDER BY case_id",
        (_RUN_ID,),
    )
    assert case_rows == [
        {"outcome": "PASS", "receipt_count": 2, "cost_cny": Decimal("0.012000")}
        for _ in range(10)
    ]
    # 逐 attempt 证据：10 例 × 2 阶段 = 20 行，receipt_hmac 非空且与 case 聚合对账。
    attempt_rows = _query(
        runner_env.settings,
        "SELECT stage, outcome, response_digest, endpoint_host, model_id,"
        " cost_cny, receipt_hmac FROM phase17_holdout_attempts WHERE run_id=%s"
        " ORDER BY case_id, attempt_index",
        (_RUN_ID,),
    )
    assert len(attempt_rows) == 20
    assert all(row["outcome"] == "PASS" for row in attempt_rows)
    assert all(row["receipt_hmac"] and len(row["receipt_hmac"]) == 64 for row in attempt_rows)
    assert all(row["endpoint_host"] == "synapse-ai.uk" for row in attempt_rows)
    assert all(row["model_id"] == "gpt-5.6-terra" for row in attempt_rows)
    assert all(row["response_digest"] == "a" * 64 for row in attempt_rows)
    assert sum(Decimal(row["cost_cny"]) for row in attempt_rows) == report.cost_cny
    state = runner_env.ledger.budget_pool_state(runner_env.contract.contract_digest)
    assert state["reserved_cny"] == Decimal("0")
    assert state["settled_cny"] == Decimal("0.120000")
    assert state["available_cny"] == Decimal("6.958814")


def test_phase17_runner_threshold_met_at_9_of_10(runner_env) -> None:
    """阈值语义（codex 第十七轮 P0-1）：9/10 达标即 batch PASS，允许 1 例失败。"""
    cases = _batch_one_cases(runner_env.manifest)
    plan = ("PASS",) * 19 + ("SEMANTIC_FAIL",)  # case1-9 全过 + case10 planner 失败
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=runner_env.bundle,
        model_port=_ScriptedModelPort(plan),
    )
    report = _execute(runner, runner_env, cases)

    assert report.status == "PASS"
    assert report.pass_count == 9 and report.total == 10
    assert report.reason_codes == ("PLANNER_VALIDATION_FAILED",)
    assert report.cost_cny == Decimal("0.120000")
    rows = _query(
        runner_env.settings,
        "SELECT status, reason_code FROM phase17_holdout_run_results WHERE run_id=%s",
        (_RUN_ID,),
    )
    assert rows == [{"status": "PASS", "reason_code": "PHASE17_HOLDOUT_BATCH_THRESHOLD_MET"}]


def test_phase17_runner_threshold_not_met_at_8_of_10(runner_env) -> None:
    """阈值语义：8/10 未达标 → batch FAILED（THRESHOLD_NOT_MET）。"""
    cases = _batch_one_cases(runner_env.manifest)
    plan = (
        ("PASS",) * 17
        + ("SEMANTIC_FAIL",)  # case9 planner 失败
        + ("PASS",)  # case10 analyst 过
        + ("SEMANTIC_FAIL",)  # case10 planner 失败
    )  # case1-8 全过（16 次）+ case9 analyst PASS = 17 次 PASS
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=runner_env.bundle,
        model_port=_ScriptedModelPort(plan),
    )
    report = _execute(runner, runner_env, cases)

    assert report.status == "FAILED"
    assert report.reason_codes == ("PLANNER_VALIDATION_FAILED",)
    assert report.pass_count == 8 and report.total == 10
    rows = _query(
        runner_env.settings,
        "SELECT status, reason_code FROM phase17_holdout_run_results WHERE run_id=%s",
        (_RUN_ID,),
    )
    assert rows == [{"status": "FAILED", "reason_code": "PHASE17_HOLDOUT_THRESHOLD_NOT_MET"}]


def test_phase17_runner_hard_block_settles_worst_case(runner_env) -> None:
    """hard block（未联网即失败）：run BLOCKED，UNKNOWN_USAGE 按最坏情况全额入账。"""
    cases = _batch_one_cases(runner_env.manifest)
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=runner_env.bundle,
        model_port=_ScriptedModelPort(("BLOCK",) * len(cases)),
    )
    report = _execute(runner, runner_env, cases)

    assert report.status == "BLOCKED"
    assert report.reason_codes == ("MODEL_OUTCOME_UNAVAILABLE",)
    assert report.pass_count == 0 and report.total == 10
    # UNKNOWN_USAGE 按最坏情况以 stage 级预留（0.1/attempt）入账，与 v2 attempt
    # reservation 口径一致：attempt 一旦建立即占用，pre-send 失败也不得结算为 0。
    assert report.cost_cny == Decimal("1.000000")  # 10 例 × 1 stage × 0.1
    rows = _query(
        runner_env.settings,
        "SELECT status, reason_code FROM phase17_holdout_run_results WHERE run_id=%s",
        (_RUN_ID,),
    )
    assert rows == [{"status": "BLOCKED", "reason_code": "PHASE17_HOLDOUT_HARD_BLOCKED"}]
    attempt_rows = _query(
        runner_env.settings,
        "SELECT outcome, category FROM phase17_holdout_attempts WHERE run_id=%s",
        (_RUN_ID,),
    )
    assert len(attempt_rows) == 10
    assert all(row["outcome"] == "FAILED" for row in attempt_rows)
    assert all(row["category"] == "TRANSPORT_ERROR" for row in attempt_rows)
    state = runner_env.ledger.budget_pool_state(runner_env.contract.contract_digest)
    assert state["settled_cny"] == Decimal("1.000000")
    assert state["available_cny"] == Decimal("6.078814")


def test_phase17_runner_rejects_subset_batch_before_ledger_write(runner_env) -> None:
    """精确集合校验（codex 第十七轮 P0-1）：子集（2 例）不再是合法 batch。"""
    cases = tuple(
        (case_id, _case_input(case_id))
        for case_id in runner_env.manifest.batch_case_ids(1)[:2]
    )
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=runner_env.bundle,
        model_port=_ScriptedModelPort(()),
    )
    with pytest.raises(Phase17HoldoutExecutionError, match="exact frozen case set"):
        _execute(runner, runner_env, cases)
    rows = _query(
        runner_env.settings, "SELECT COUNT(*) AS n FROM phase17_holdout_runs"
    )
    assert rows == [{"n": 0}]


def test_phase17_runner_rejects_mixed_batch(runner_env) -> None:
    """精确集合校验：混入 batch2 的 case（9+1）也必须是 batch1 精确全集。"""
    batch1_ids = runner_env.manifest.batch_case_ids(1)
    batch2_id = runner_env.manifest.batch_case_ids(2)[0]
    cases = tuple(
        (case_id, _case_input(case_id)) for case_id in batch1_ids[:9]
    ) + ((batch2_id, _case_input(batch2_id)),)
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=runner_env.bundle,
        model_port=_ScriptedModelPort(()),
    )
    with pytest.raises(Phase17HoldoutExecutionError, match="exact frozen case set"):
        _execute(runner, runner_env, cases)


def test_phase17_runner_rejects_campaign_contract_mismatch(runner_env) -> None:
    """全链身份绑定（codex 第十七轮 P0-2）：campaign 声称的契约与实参不一致 → 拒绝。"""
    cases = _batch_one_cases(runner_env.manifest)
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=runner_env.bundle,
        model_port=_ScriptedModelPort(()),
    )
    bad_campaign = _campaign(
        contract=runner_env.contract,
        manifest=runner_env.manifest,
        bundle=runner_env.bundle,
    )
    bad_campaign = SimpleNamespace(
        **{**vars(bad_campaign), "contract_digest": "0" * 64}
    )
    with pytest.raises(Phase17HoldoutExecutionError, match="campaign contract identity"):
        asyncio.run(
            runner.execute(
                campaign=bad_campaign,
                run_id=_RUN_ID,
                batch_index=1,
                cases=cases,
                manifest=runner_env.manifest,
            )
        )
    rows = _query(
        runner_env.settings, "SELECT COUNT(*) AS n FROM phase17_holdout_runs"
    )
    assert rows == [{"n": 0}]


def test_phase17_runner_rejects_campaign_dataset_mismatch(runner_env) -> None:
    """campaign 声称的 dataset manifest 与实参不一致 → 拒绝。"""
    cases = _batch_one_cases(runner_env.manifest)
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=runner_env.bundle,
        model_port=_ScriptedModelPort(()),
    )
    bad_campaign = SimpleNamespace(
        **{**vars(_campaign(contract=runner_env.contract, manifest=runner_env.manifest, bundle=runner_env.bundle)),
           "dataset_manifest_digest": "1" * 64}
    )
    with pytest.raises(Phase17HoldoutExecutionError, match="campaign dataset identity"):
        asyncio.run(
            runner.execute(
                campaign=bad_campaign,
                run_id=_RUN_ID,
                batch_index=1,
                cases=cases,
                manifest=runner_env.manifest,
            )
        )


def test_phase17_runner_rejects_candidate_policy_mismatch(runner_env) -> None:
    """candidate policy_digest 与契约不一致 → 构造即拒绝（联网前）。"""
    bad_bundle = _bundle(contract=runner_env.contract)
    bad_bundle = SimpleNamespace(
        candidate=SimpleNamespace(
            **{**vars(bad_bundle.candidate), "policy_digest": "2" * 64}
        ),
        analyst_profile=bad_bundle.analyst_profile,
        planner_profile=bad_bundle.planner_profile,
    )
    with pytest.raises(Phase17HoldoutExecutionError, match="candidate policy identity"):
        Phase17HoldoutCampaignRunner(
            contract=runner_env.contract,
            ledger=runner_env.ledger,
            candidate_bundle=bad_bundle,
            model_port=_ScriptedModelPort(()),
        )


def test_phase17_runner_rejects_identity_mismatch_before_network(runner_env) -> None:
    """身份固定：candidate model_id 与契约冻结身份不一致 → 联网前 fail-closed。"""
    with pytest.raises(Phase17HoldoutExecutionError, match="model identity does not match"):
        Phase17HoldoutCampaignRunner(
            contract=runner_env.contract,
            ledger=runner_env.ledger,
            candidate_bundle=_bundle(contract=runner_env.contract, model_id="deepseek-v4-pro"),
            model_port=_ScriptedModelPort(()),
        )
    # 未写入任何账本行（contract/campaign/run 均未创建）。
    rows = _query(
        runner_env.settings, "SELECT COUNT(*) AS n FROM phase17_holdout_contracts"
    )
    assert rows == [{"n": 0}]


def test_phase17_runner_rejects_non_phase17_contract(runner_env) -> None:
    """反向隔离：v2 policy 对象作为契约传入 phase17 入口 → 类型守卫拒绝。"""
    v2_like_contract = SimpleNamespace(
        execution_identity="V2_HISTORICAL_EXECUTION",
        implementation_status="WIRED_INTO_RUNTIME",
    )
    with pytest.raises(Phase17HoldoutExecutionError, match="CONTRACT_TYPE_MISMATCH"):
        Phase17HoldoutCampaignRunner(
            contract=v2_like_contract,
            ledger=runner_env.ledger,
            candidate_bundle=runner_env.bundle,
            model_port=_ScriptedModelPort(()),
        )


def test_phase17_runner_membership_mismatch_blocks_before_ledger_write(runner_env) -> None:
    """case 输入篡改（input digest 漂移）：精确集合校验通过后由 membership 校验阻断，
    先于任何账本写入与模型调用。"""
    cases = list(_batch_one_cases(runner_env.manifest))
    last_id, _ = cases[-1]
    cases[-1] = (last_id, "tampered-input-content")  # case_id 在集合内但输入被改
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=runner_env.bundle,
        model_port=_ScriptedModelPort(()),
    )
    with pytest.raises(Phase17HoldoutExecutionError, match="input digest does not match the frozen manifest"):
        _execute(runner, runner_env, tuple(cases))
    rows = _query(
        runner_env.settings, "SELECT COUNT(*) AS n FROM phase17_holdout_runs"
    )
    assert rows == [{"n": 0}]


def test_phase17_aggregate_27_of_30_qualified(runner_env) -> None:
    """27/30 聚合（codex 第十七轮 P0-1）：两批达标且总 pass >= 27 → QUALIFIED。"""
    batch1 = _execute(
        Phase17HoldoutCampaignRunner(
            contract=runner_env.contract,
            ledger=runner_env.ledger,
            candidate_bundle=runner_env.bundle,
            model_port=_ScriptedModelPort(("PASS",) * (10 * 2)),
        ),
        runner_env,
        _batch_one_cases(runner_env.manifest),
    )
    # batch2 用 18/20 达标（18 PASS + 2 planner 语义失败）→ 总 28/30。
    batch2_cases = tuple(
        (case_id, _case_input(case_id))
        for case_id in runner_env.manifest.batch_case_ids(2)
    )
    plan2 = ("PASS",) * 37 + ("SEMANTIC_FAIL",) + ("PASS",) + ("SEMANTIC_FAIL",)
    batch2 = asyncio.run(
        Phase17HoldoutCampaignRunner(
            contract=runner_env.contract,
            ledger=runner_env.ledger,
            candidate_bundle=runner_env.bundle,
            model_port=_ScriptedModelPort(plan2),
        ).execute(
            campaign=_campaign(
                contract=runner_env.contract,
                manifest=runner_env.manifest,
                bundle=runner_env.bundle,
                batch_index=2,
            ),
            run_id="phase17-holdout-run-9002",
            batch_index=2,
            cases=batch2_cases,
            manifest=runner_env.manifest,
        )
    )
    assert batch1.status == "PASS" and batch2.status == "PASS"
    assert batch2.pass_count == 18 and batch2.pass_min == 18

    aggregate = aggregate_phase17_holdout_reports(
        reports=(batch1, batch2),
        contract=runner_env.contract,
        safety_gate=Phase17SafetyGateResult(
            status="PASS",
            reason_code="TEST_HARD_SAFETY_PASS",
            reviewed_case_ids=(),
        ),
    )
    assert aggregate.status == "PASS"
    assert aggregate.reason_codes == ("PHASE17_HOLDOUT_QUALIFIED_90PCT_PORTFOLIO_THRESHOLD",)
    assert aggregate.total_pass == 28
    assert aggregate.total_cases == 30
    assert aggregate.batch_statuses == ("PASS", "PASS")


def test_phase17_aggregate_26_of_30_failed(runner_env) -> None:
    """27/30 聚合：总 pass 26（9 + 17）即使两批都"达标"也不足 27 → FAILED。"""
    batch1 = _execute(
        Phase17HoldoutCampaignRunner(
            contract=runner_env.contract,
            ledger=runner_env.ledger,
            candidate_bundle=runner_env.bundle,
            model_port=_ScriptedModelPort(("PASS",) * (10 * 2)),
        ),
        runner_env,
        _batch_one_cases(runner_env.manifest),
    )
    # batch2 精确 17/20：17 例全过 + 3 例 planner 失败（3 例 ≤ 阈值缺口，batch 仍 PASS 17>=18? 不，17 < 18 → FAILED）
    # 构造：batch2 17/20 → batch2 FAILED（未达 18）→ 聚合 FAILED。
    batch2_cases = tuple(
        (case_id, _case_input(case_id))
        for case_id in runner_env.manifest.batch_case_ids(2)
    )
    plan2 = (
        ("PASS",) * 35
        + ("SEMANTIC_FAIL",)  # case18 planner 失败
        + ("PASS",)  # case19 analyst 过
        + ("SEMANTIC_FAIL",)  # case19 planner 失败
        + ("PASS",)  # case20 analyst 过
        + ("SEMANTIC_FAIL",)  # case20 planner 失败
    )  # case1-17 全过（34 次）+ case18 analyst PASS = 35 次 PASS → pass=17
    batch2 = asyncio.run(
        Phase17HoldoutCampaignRunner(
            contract=runner_env.contract,
            ledger=runner_env.ledger,
            candidate_bundle=runner_env.bundle,
            model_port=_ScriptedModelPort(plan2),
        ).execute(
            campaign=_campaign(
                contract=runner_env.contract,
                manifest=runner_env.manifest,
                bundle=runner_env.bundle,
                batch_index=2,
            ),
            run_id="phase17-holdout-run-9003",
            batch_index=2,
            cases=batch2_cases,
            manifest=runner_env.manifest,
        )
    )
    assert batch2.status == "FAILED"
    assert batch2.pass_count == 17

    aggregate = aggregate_phase17_holdout_reports(
        reports=(batch1, batch2),
        contract=runner_env.contract,
        # 本用例只验证自动阈值 FAILED；显式提供合成安全 PASS，避免把
        # “未评估安全门禁”的 BLOCKED 结果误当成阈值判定结果。
        safety_gate=Phase17SafetyGateResult(
            status="PASS",
            reason_code="TEST_HARD_SAFETY_PASS",
            reviewed_case_ids=(),
        ),
    )
    assert aggregate.status == "FAILED"
    assert aggregate.reason_codes == ("PHASE17_HOLDOUT_AGGREGATE_THRESHOLD_NOT_MET",)
    assert aggregate.total_pass == 27


def test_phase17_aggregate_blocked_wins_over_threshold(runner_env) -> None:
    """BLOCKED 优先（inconclusive）：任一批 BLOCKED 即聚合 BLOCKED，不进入达标判定。"""
    batch1 = Phase17HoldoutRunReport(
        campaign_id="c-1", run_id="r-1", batch_index=1,
        contract_digest=runner_env.contract.contract_digest,
        dataset_manifest_digest=runner_env.manifest.manifest_digest,
        candidate_digest="c" * 64,
        status="PASS",
        reason_codes=("EXECUTION_COMPLETE",),
        pass_count=10, pass_min=9, total=10,
        critical_safety_failures=0,
        cost_cny=Decimal("0.120000"), case_executions=(),
    )
    batch2 = Phase17HoldoutRunReport(
        campaign_id="c-2", run_id="r-2", batch_index=2,
        contract_digest=runner_env.contract.contract_digest,
        dataset_manifest_digest=runner_env.manifest.manifest_digest,
        candidate_digest="c" * 64,
        status="BLOCKED",
        reason_codes=("MODEL_OUTCOME_UNAVAILABLE",),
        pass_count=0, pass_min=18, total=20,
        critical_safety_failures=0,
        cost_cny=Decimal("1.000000"), case_executions=(),
    )
    aggregate = aggregate_phase17_holdout_reports(
        reports=(batch1, batch2), contract=runner_env.contract
    )
    assert aggregate.status == "BLOCKED"
    assert aggregate.reason_codes == ("PHASE17_HOLDOUT_AGGREGATE_BLOCKED",)
    assert aggregate.total_pass == 10
    assert aggregate.batch_statuses == ("PASS", "BLOCKED")


def test_phase17_critical_safety_zero_failure_enforced(runner_env) -> None:
    """18 轮 P0-1：9 PASS + 1 analyst 失败 → 通过率达标但关键安全红线触发 FAILED。

    证明 critical safety 与 9/10 阈值是两条独立线：analyst 未产出可用证据
    （ANALYST_VALIDATION_FAILED）即使 pass 数达标也必须 FAILED。
    """
    plan = ("PASS",) * 18 + ("SEMANTIC_FAIL",)  # case10 analyst 结构失败 → 短路
    report = _execute(
        Phase17HoldoutCampaignRunner(
            contract=runner_env.contract,
            ledger=runner_env.ledger,
            candidate_bundle=runner_env.bundle,
            model_port=_ScriptedModelPort(plan),
        ),
        runner_env,
        _batch_one_cases(runner_env.manifest),
    )
    assert report.pass_count == 9
    assert report.critical_safety_failures == 1
    assert report.status == "FAILED"
    # reason_codes 是 case 级失败原因集合；run 级红线 reason 在账本终态行。
    assert report.reason_codes == ("ANALYST_VALIDATION_FAILED",)
    run_row = _query(
        runner_env.settings,
        "SELECT status, reason_code FROM phase17_holdout_run_results WHERE run_id=%s",
        (_RUN_ID,),
    )
    assert run_row[0]["status"] == "FAILED"
    assert run_row[0]["reason_code"] == "PHASE17_HOLDOUT_CRITICAL_SAFETY_ZERO_FAILURE_VIOLATED"


def test_phase17_critical_safety_blocked_wins_over_critical(runner_env) -> None:
    """18 轮 P0-1：BLOCKED 优先——critical 失败与 BLOCKED 并存时终态为 BLOCKED。"""
    plan = ("BLOCK",) + ("PASS",) * 18  # case1 analyst pre-send 失败 → 短路
    report = _execute(
        Phase17HoldoutCampaignRunner(
            contract=runner_env.contract,
            ledger=runner_env.ledger,
            candidate_bundle=runner_env.bundle,
            model_port=_ScriptedModelPort(plan),
        ),
        runner_env,
        _batch_one_cases(runner_env.manifest),
    )
    assert report.critical_safety_failures == 0
    assert report.status == "BLOCKED"
    # case 级失败集合是 MODEL_OUTCOME_UNAVAILABLE；run 级 BLOCKED reason 在账本。
    assert report.reason_codes == ("MODEL_OUTCOME_UNAVAILABLE",)
    run_row = _query(
        runner_env.settings,
        "SELECT status, reason_code FROM phase17_holdout_run_results WHERE run_id=%s",
        (_RUN_ID,),
    )
    assert run_row[0]["status"] == "BLOCKED"
    assert run_row[0]["reason_code"] == "PHASE17_HOLDOUT_HARD_BLOCKED"


def test_phase17_attempt_rows_per_network_attempt(runner_env) -> None:
    """18 轮 P0-3：重试后成功 → 每次网络 attempt 独立一行；中间失败行 0 成本、
    最终成功行按 usage 定价；receipt_count 对账真实行数而非 stage 数。"""
    report = _execute(
        Phase17HoldoutCampaignRunner(
            contract=runner_env.contract,
            ledger=runner_env.ledger,
            candidate_bundle=runner_env.bundle,
            model_port=_ScriptedModelPort(("RETRY_OK",) * 20),
        ),
        runner_env,
        _batch_one_cases(runner_env.manifest),
    )
    assert report.status == "PASS"
    # 20 次调用 × 2 attempt = 40 行；每行最终 PASS usage (1000,500) → 0.006。
    assert report.cost_cny == Decimal("0.120000")
    rows = _query(
        runner_env.settings,
        """SELECT case_id, stage, attempt_index, outcome, cost_cny, receipt_hmac
             FROM phase17_holdout_attempts
            ORDER BY case_id, stage, attempt_index""",
    )
    assert len(rows) == 40
    for index in range(0, len(rows), 2):
        assert rows[index]["outcome"] == "FAILED"
        assert rows[index]["cost_cny"] == Decimal("0")
        assert rows[index]["attempt_index"] == 1
        assert rows[index + 1]["outcome"] == "PASS"
        assert rows[index + 1]["cost_cny"] == Decimal("0.006000")
        assert rows[index + 1]["attempt_index"] == 2
        assert rows[index]["receipt_hmac"] != rows[index + 1]["receipt_hmac"]
        assert len(rows[index]["receipt_hmac"]) == 64
    # 每 case 每 stage 的真实 receipt 数为 2，非 stage 数 1。
    case_rows = _query(
        runner_env.settings,
        "SELECT receipt_count FROM phase17_holdout_case_results ORDER BY case_id",
    )
    assert [row["receipt_count"] for row in case_rows] == [4] * 10


def test_phase17_attempt_unknown_usage_last_row_reservation(runner_env) -> None:
    """18 轮 P0-3 + cost bug：全网络失败无 usage → 每 stage 一行记 stage 级预留
    全额（0.1），绝不使用 campaign 级预留（多 case 失败不得重复全额占用池）。"""
    report = _execute(
        Phase17HoldoutCampaignRunner(
            contract=runner_env.contract,
            ledger=runner_env.ledger,
            candidate_bundle=runner_env.bundle,
            model_port=_ScriptedModelPort(("BLOCK",) * 10),
        ),
        runner_env,
        _batch_one_cases(runner_env.manifest),
    )
    assert report.status == "BLOCKED"
    assert report.cost_cny == Decimal("1.000000")  # 10 analyst 行 × 0.1 stage 预留
    rows = _query(
        runner_env.settings,
        "SELECT stage, cost_cny FROM phase17_holdout_attempts ORDER BY case_id",
    )
    assert len(rows) == 10
    assert all(row["stage"] == "ANALYST" for row in rows)
    assert all(row["cost_cny"] == Decimal("0.100000") for row in rows)


def test_phase17_aggregate_identity_checks(runner_env) -> None:
    """18 轮 P0-1：聚合前身份校验——batch 归属、contract/dataset/candidate
    身份任一漂移或缺失都必须拒绝，防伪造 report。"""
    base = dict(
        campaign_id="c-1",
        run_id="r-1",
        batch_index=1,
        contract_digest=runner_env.contract.contract_digest,
        dataset_manifest_digest=runner_env.manifest.manifest_digest,
        candidate_digest="c" * 64,
        status="PASS",
        reason_codes=("EXECUTION_COMPLETE",),
        pass_count=10,
        pass_min=9,
        total=10,
        critical_safety_failures=0,
        cost_cny=Decimal("0.120000"),
        case_executions=(),
    )
    batch1 = Phase17HoldoutRunReport(**base)
    batch2 = Phase17HoldoutRunReport(
        **{**base, "campaign_id": "c-2", "run_id": "r-2", "batch_index": 2,
           "pass_count": 18, "pass_min": 18, "total": 20}
    )
    with pytest.raises(Phase17HoldoutExecutionError):
        aggregate_phase17_holdout_reports(
            reports=(batch2, batch1), contract=runner_env.contract
        )  # 错序：batch(2,1)
    with pytest.raises(Phase17HoldoutExecutionError):
        aggregate_phase17_holdout_reports(
            reports=(
                batch1,
                Phase17HoldoutRunReport(
                    **{**base, "campaign_id": "c-2", "run_id": "r-2", "batch_index": 2,
                       "pass_count": 18, "pass_min": 18, "total": 20,
                       "candidate_digest": "d" * 64}
                ),
            ),
            contract=runner_env.contract,
        )  # candidate 身份漂移
    with pytest.raises(Phase17HoldoutExecutionError):
        aggregate_phase17_holdout_reports(
            reports=(
                batch1,
                Phase17HoldoutRunReport(
                    **{**base, "campaign_id": "c-2", "run_id": "r-2", "batch_index": 2,
                       "pass_count": 18, "pass_min": 18, "total": 20,
                       "dataset_manifest_digest": "e" * 64}
                ),
            ),
            contract=runner_env.contract,
        )  # dataset 身份漂移
    with pytest.raises(Phase17HoldoutExecutionError):
        aggregate_phase17_holdout_reports(
            reports=(batch1,), contract=runner_env.contract
        )  # 缺 batch2
