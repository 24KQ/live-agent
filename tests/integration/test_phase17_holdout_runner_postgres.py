"""Phase 17 holdout 执行器端到端离线链 PostgreSQL 集成测试。

用脚本化 fake model port（不联网）驱动 contract → ledger → runner 全链：
- 全 pass / 语义失败 / hard block 三路径的 run 终态与结算断言；
- UNKNOWN_USAGE 按最坏情况以 reservation 全额入账；
- 身份不匹配（模型/渠道/类型）在联网前 fail-closed；
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
from src.specialist_runtime.profiles import SpecialistProfile


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_HMAC_KEY = bytes.fromhex("d3" * 32)
_RUN_ID = "phase17-holdout-run-9001"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_manifest() -> Phase17HoldoutDatasetManifest:
    """合成 30 例 manifest（与 unit fixture 同构，不触碰真实数据集）。"""
    case_ids = [f"holdout-case-{i:03d}" for i in range(1, 31)]
    payload = {
        "dataset_id": "phase17-holdout-cases-v1",
        "dataset_version": "1.0.0",
        "split": "HOLDOUT",
        "case_count": 30,
        "case_id_to_input_digest": {cid: _digest(f"input-{cid}") for cid in case_ids},
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
    model_id: str = "gpt-5.6-luna",
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
    model_id: str = "gpt-5.6-luna",
    endpoint_host: str = "synapse-ai.uk",
) -> CandidateProfileBundle:
    candidate = QualificationCandidate(
        candidate_id="phase17-candidate-luna-v1",
        policy_digest=contract.contract_digest,
        model_id=model_id,
        endpoint_host=endpoint_host,
        analyst_profile_digest="d" * 64,
        planner_profile_digest="e" * 64,
        adapter_digest="f" * 64,
    )
    return CandidateProfileBundle(
        candidate=candidate,
        analyst_profile=_profile(task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS, model_id=model_id, endpoint_host=endpoint_host),
        planner_profile=_profile(task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING, model_id=model_id, endpoint_host=endpoint_host),
    )


def _campaign(*, contract, manifest: Phase17HoldoutDatasetManifest) -> Phase17HoldoutCampaign:
    campaign_id = qualification_campaign_id(
        kind=QualificationCampaignKind.HOLDOUT,
        candidate_digest="a" * 64,
        declared_model_id="gpt-5.6-luna",
        declared_reasoning_effort=None,
        declared_endpoint_hosts=("synapse-ai.uk",),
        batch_index=1,
    )
    return Phase17HoldoutCampaign(
        campaign_id=campaign_id,
        contract_digest=contract.contract_digest,
        batch_index=1,
        candidate_digest="a" * 64,
        dataset_manifest_digest=manifest.manifest_digest,
        reservation_cny=Decimal("1.000000"),
        declared_model_id="gpt-5.6-luna",
        declared_reasoning_effort=None,
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
            output = (
                {"trigger_codes": ["PRICE_CONFLICT"], "analysis": {"severity": "HIGH"}}
                if stage == "ANALYST"
                else {"risk_codes": ["RISK_PRICE_DRIFT"], "proposal": {"action": "HOLD"}}
            )
            return ModelSuccess(
                request_id=request.request_id,
                model_id=request.model_id,
                output=output,
                usage=ModelUsage(input_tokens=1000, output_tokens=500, total_tokens=1500),
                response_digest="a" * 64,
                latency_ms=Decimal("0"),
            )
        if kind == "SEMANTIC_FAIL":
            # 已联网但结构校验失败（如 planner 未产出 risk_codes）。
            return ModelSuccess(
                request_id=request.request_id,
                model_id=request.model_id,
                output={"proposal": {"action": "HOLD"}},
                usage=ModelUsage(input_tokens=1000, output_tokens=500, total_tokens=1500),
                response_digest="a" * 64,
                latency_ms=Decimal("0"),
            )
        if kind == "BLOCK":
            return ModelFailure(
                request_id=request.request_id,
                category=ModelFailureCategory.TRANSPORT_ERROR,
                request_sent=False,
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
    env = SimpleNamespace(
        settings=settings,
        ledger=ledger,
        contract=contract,
        manifest=manifest,
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
        (case_id, f"input-{case_id}") for case_id in manifest.batch_case_ids(1)[:2]
    )


def _execute(runner, env, cases):
    """同步入口：await 一次 execute 并返回报告（仓库既有 asyncio.run 模式）。"""
    return asyncio.run(
        runner.execute(
            campaign=_campaign(contract=env.contract, manifest=env.manifest),
            run_id=_RUN_ID,
            batch_index=1,
            cases=cases,
            manifest=env.manifest,
        )
    )


def test_phase17_runner_end_to_end_pass(runner_env) -> None:
    """全 pass：run 终态 PASS、结算按实际成本、case 结论逐条入账。"""
    cases = _batch_one_cases(runner_env.manifest)
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=_bundle(contract=runner_env.contract),
        model_port=_ScriptedModelPort(("PASS", "PASS", "PASS", "PASS")),
    )
    report = _execute(runner, runner_env, cases)

    assert report.status == "PASS"
    assert report.reason_codes == ("EXECUTION_COMPLETE",)
    assert report.pass_count == 2 and report.total == 2
    assert report.cost_cny == Decimal("0.024000")  # 2 例 × 2 阶段 × (3M in + 6M out)/1M

    rows = _query(
        runner_env.settings,
        "SELECT status, reason_code FROM phase17_holdout_run_results WHERE run_id=%s",
        (_RUN_ID,),
    )
    assert rows == [{"status": "PASS", "reason_code": "PHASE17_HOLDOUT_BATCH_COMPLETE"}]
    case_rows = _query(
        runner_env.settings,
        "SELECT outcome, receipt_count, cost_cny FROM phase17_holdout_case_results"
        " WHERE run_id=%s ORDER BY case_id",
        (_RUN_ID,),
    )
    assert case_rows == [
        {"outcome": "PASS", "receipt_count": 2, "cost_cny": Decimal("0.012000")},
        {"outcome": "PASS", "receipt_count": 2, "cost_cny": Decimal("0.012000")},
    ]
    state = runner_env.ledger.budget_pool_state(runner_env.contract.contract_digest)
    assert state["reserved_cny"] == Decimal("0")
    assert state["settled_cny"] == Decimal("0.024000")
    assert state["available_cny"] == Decimal("8.371869")


def test_phase17_runner_semantic_failure_marks_failed(runner_env) -> None:
    """语义失败（已联网但结构校验不过）：run FAILED，成本如实入账。"""
    cases = _batch_one_cases(runner_env.manifest)
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=_bundle(contract=runner_env.contract),
        model_port=_ScriptedModelPort(("PASS", "PASS", "PASS", "SEMANTIC_FAIL")),
    )
    report = _execute(runner, runner_env, cases)

    assert report.status == "FAILED"
    assert report.reason_codes == ("PLANNER_VALIDATION_FAILED",)
    assert report.pass_count == 1 and report.total == 2
    assert report.cost_cny == Decimal("0.024000")
    rows = _query(
        runner_env.settings,
        "SELECT status, reason_code FROM phase17_holdout_run_results WHERE run_id=%s",
        (_RUN_ID,),
    )
    assert rows == [{"status": "FAILED", "reason_code": "PHASE17_HOLDOUT_EXECUTION_FAILED"}]


def test_phase17_runner_hard_block_settles_worst_case(runner_env) -> None:
    """hard block（未联网即失败）：run BLOCKED，UNKNOWN_USAGE 按最坏情况全额入账。"""
    cases = _batch_one_cases(runner_env.manifest)
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=_bundle(contract=runner_env.contract),
        model_port=_ScriptedModelPort(("BLOCK", "BLOCK")),
    )
    report = _execute(runner, runner_env, cases)

    assert report.status == "BLOCKED"
    assert report.reason_codes == ("MODEL_OUTCOME_UNAVAILABLE",)
    assert report.pass_count == 0 and report.total == 2
    # UNKNOWN_USAGE 按最坏情况以 stage 级预留（0.1/attempt）入账，与 v2 attempt
    # reservation 口径一致：attempt 一旦建立即占用，pre-send 失败也不得结算为 0。
    assert report.cost_cny == Decimal("0.200000")  # 2 例 × 1 stage × 0.1
    rows = _query(
        runner_env.settings,
        "SELECT status, reason_code FROM phase17_holdout_run_results WHERE run_id=%s",
        (_RUN_ID,),
    )
    assert rows == [{"status": "BLOCKED", "reason_code": "PHASE17_HOLDOUT_HARD_BLOCKED"}]
    state = runner_env.ledger.budget_pool_state(runner_env.contract.contract_digest)
    assert state["settled_cny"] == Decimal("0.200000")
    assert state["available_cny"] == Decimal("8.195869")


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
            candidate_bundle=_bundle(contract=runner_env.contract),
            model_port=_ScriptedModelPort(()),
        )


def test_phase17_runner_membership_mismatch_blocks_before_ledger_write(runner_env) -> None:
    """case 不在冻结 manifest：先于任何账本写入与模型调用阻断。"""
    cases = (("holdout-case-099", "input-holdout-case-099"),)  # 集合外 case
    runner = Phase17HoldoutCampaignRunner(
        contract=runner_env.contract,
        ledger=runner_env.ledger,
        candidate_bundle=_bundle(contract=runner_env.contract),
        model_port=_ScriptedModelPort(()),
    )
    with pytest.raises(Phase17HoldoutExecutionError, match="not in the frozen manifest"):
        _execute(runner, runner_env, cases)
    rows = _query(
        runner_env.settings, "SELECT COUNT(*) AS n FROM phase17_holdout_runs"
    )
    assert rows == [{"n": 0}]
