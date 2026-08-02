"""Phase 16 qualification campaign runner 的 PostgreSQL 集成契约。

使用 fake ModelPort 模拟全 pass、语义失败与阻断路径，不发送真实模型请求。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from src.config.settings import get_settings
from src.decision_support.multi_agent_evaluation import _assemble_bundle
from src.decision_support.official_smoke_runner_v2 import (
    _projection_evidence_registry,
    _synthetic_live_parents,
)
from src.decision_support.phase16_qualification import (
    PHASE16_QUALIFICATION_ASSET_DIRECTORY,
    PHASE16_QUALIFICATION_POLICY_PATH,
    QualificationCaseKind,
    build_phase16_qualification_policy,
    load_phase16_qualification_corpus,
)
from src.decision_support.phase16_qualification_evaluator import (
    QualificationEvaluator,
    build_phase16_qualification_candidate_bundle,
)
from src.decision_support.phase16_qualification_execution_ledger import (
    PostgresPhase16QualificationExecutionLedger,
)
from src.decision_support.phase16_qualification_ledger import (
    QualificationCampaign,
    QualificationCampaignKind,
    QualificationRunStatus,
    corpus_identity_from_manifest,
    initialize_phase16_qualification_schema,
    qualification_campaign_id,
)
from src.decision_support.phase16_qualification_runner import (
    Phase16QualificationCampaignRunner,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelRequest,
    ModelSuccess,
    ModelUsage,
)
from src.specialist_runtime.models import canonical_json_sha256, _plain_json


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_HMAC_KEY = bytes.fromhex("d3" * 32)


@pytest.fixture(autouse=True)
def frozen_v2_closure(monkeypatch: pytest.MonkeyPatch):
    """v2 冻结闭包快照（与 unit 侧同一模式）。

    ledger.py 演进（407b43c 运行时强制、并发竞态修复）后当前工作树相对 v2
    冻结闭包漂移，真实 load 路径 fail-closed（正确行为，由 unit 测试
    test_qualification_policy_rejects_source_closure_drift 断言）。本 fixture
    让聚焦执行语义的集成测试在 v2 时刻闭包下运行，不改变被测行为。
    """
    frozen = json.loads(
        (_PROJECT_ROOT / PHASE16_QUALIFICATION_POLICY_PATH).read_bytes()
    )["source_file_digests"]
    monkeypatch.setattr(
        "src.decision_support.phase16_qualification.qualification_source_file_digests",
        lambda *, repository_root: frozen,
    )


class _FakeModelPort:
    """预设 ModelOutcome 序列，每调用一次推进一个。自动设置 request_id。"""

    def __init__(self, outcomes: list[ModelSuccess | ModelFailure]) -> None:
        self._outcomes = list(outcomes)
        self._index = 0
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelSuccess | ModelFailure:
        self.requests.append(request)
        if self._index >= len(self._outcomes):
            raise RuntimeError(f"fake model port depleted at index {self._index}")
        item = self._outcomes[self._index]
        self._index += 1
        import json
        if isinstance(item, ModelFailure):
            return ModelFailure(
                request_id=request.request_id,
                category=item.category,
                request_sent=item.request_sent,
                latency_ms=item.latency_ms,
            )
        from src.specialist_runtime.models import _plain_json
        plain_output = _plain_json(item.output)
        return ModelSuccess(
            request_id=request.request_id,
            model_id=item.model_id,
            output=plain_output,
            usage=item.usage,
            provider_response_id=item.provider_response_id,
            finish_reason=item.finish_reason,
            response_digest=canonical_json_sha256(plain_output),
            latency_ms=item.latency_ms,
            endpoint_host=item.endpoint_host,
            attempts=item.attempts,
        )# ---------------------------------------------------------------------------
# 构建有效模型输出的辅助函数
# ---------------------------------------------------------------------------

def _evidence_ids_from_case(*, case, now) -> list[str]:
    """从六角色投影提取 evidence_id，供模型输出绑定时使用。"""
    from src.decision_support.evidence import EvidenceBundleSnapshot

    workspace, incident = _synthetic_live_parents(case_id=case.case_id, now=now)
    bundle = _assemble_bundle(workspace=workspace, incident=incident, case=case, now=now)
    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    return [comp.reference.evidence_id for comp in snapshot.components]


def _analyst_output(evidence_ids: list[str]) -> dict:
    return {
        "kind": "FINAL",
        "final_output": {
            "constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED"],
            "risk_codes": ["HUMAN_CONFIRMATION_REQUIRED", "SIDE_EFFECT_UNKNOWN"],
            "explanation": "Controlled qualification explanation passes guardrails.",
            "evidence_ids": [evidence_ids[0]],
        },
    }


def _planner_output(evidence_ids: list[str], risk_flags: list[str]) -> dict:
    return {
        "kind": "FINAL",
        "final_output": {
            "options": [
                {
                    "option_id": "qual-runner-test-option",
                    "product_strategy": "KEEP_CURRENT",
                    "backup_product_id": None,
                    "host_prompt": "Operator review: controlled qualification test.",
                    "timing": "AFTER_OPERATOR_CONFIRMATION",
                    "risk_flags": sorted(set(risk_flags)),
                    "evidence_ids": [evidence_ids[0]],
                }
            ],
        },
    }


def _mk_success(output: dict, provider_suffix: str) -> ModelSuccess:
    return ModelSuccess(
        request_id="placeholder",
        # V9 矩阵配置：fake 回显 campaign 声明默认组合（gpt-5.6-luna × synapse-ai.uk），
        # 使 identity_matched 成立、HARD_SAFETY_CONFORMANCE 按声明一致计数。
        model_id="gpt-5.6-luna",
        output=output,
        usage=ModelUsage(input_tokens=50, output_tokens=50, total_tokens=100),
        provider_response_id=f"qual-runner-provider-{provider_suffix}",
        finish_reason="stop",
        response_digest=canonical_json_sha256(output),
        latency_ms=Decimal("0.500"),
        endpoint_host="synapse-ai.uk",
    )


# ---------------------------------------------------------------------------
# PostgreSQL fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def ledger_factory():
    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_qualification_runner_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    initialize_phase16_qualification_schema(settings)

    def build() -> PostgresPhase16QualificationExecutionLedger:
        return PostgresPhase16QualificationExecutionLedger(settings, hmac_key=_TEST_HMAC_KEY)

    build.settings = settings
    try:
        yield build
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
            connection.commit()


def _init_campaign(ledger: PostgresPhase16QualificationExecutionLedger):
    """初始化 policy/corpus/candidate/campaign 身份并返回依赖项。"""
    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    corpus = load_phase16_qualification_corpus(
        _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        repository_root=_PROJECT_ROOT,
        policy=policy,
    )
    identity = corpus_identity_from_manifest(corpus.manifest)
    bundle = build_phase16_qualification_candidate_bundle(
        policy=policy, repository_root=_PROJECT_ROOT,
    )
    ledger.ensure_policy(policy)
    ledger.ensure_corpus(identity)
    ledger.ensure_candidate(bundle.candidate)
    campaign = QualificationCampaign(
        # fixture 使用模型默认声明组合（gpt-5.6-luna / 无 effort / synapse 单渠道）。
        campaign_id=qualification_campaign_id(
            kind=QualificationCampaignKind.DEVELOPMENT,
            candidate_digest=bundle.candidate.candidate_digest or "",
            declared_model_id="gpt-5.6-luna",
            declared_reasoning_effort=None,
            declared_endpoint_hosts=("synapse-ai.uk",),
        ),
        campaign_kind=QualificationCampaignKind.DEVELOPMENT,
        policy_digest=policy.policy_digest or "",
        corpus_digest=identity.corpus_digest,
        candidate_digest=bundle.candidate.candidate_digest or "",
        manifest_digest=corpus.manifest.manifest_digest or "",
        reservation_cny="1.000000",
    )
    ledger.ensure_campaign(campaign)
    return policy, corpus, identity, bundle, campaign


def _high_conflict_cases(corpus) -> tuple:
    return tuple(
        c for c in corpus.development_cases
        if c.kind is QualificationCaseKind.HIGH_CONFLICT_PAIRED
    )


# ---------------------------------------------------------------------------
# Test: 全 PASS
# ---------------------------------------------------------------------------

def test_runner_all_cases_pass(ledger_factory) -> None:
    """12/12 高冲突全部 Analyst→Planner PASS → status=PASS, E2E=12/12。"""
    ledger = ledger_factory()
    policy, corpus, identity, bundle, campaign = _init_campaign(
        ledger,
    )
    cases = _high_conflict_cases(corpus)
    now = datetime.now(timezone.utc)
    eid: dict[str, list[str]] = {}
    for c in cases:
        eid[c.case_id] = _evidence_ids_from_case(case=c, now=now)

    outcomes: list[ModelSuccess | ModelFailure] = []
    for c in cases:
        outcomes.append(_mk_success(_analyst_output(eid[c.case_id]), f"{c.case_id}-a"))
        outcomes.append(_mk_success(
            _planner_output(eid[c.case_id], ["HUMAN_CONFIRMATION_REQUIRED", "SIDE_EFFECT_UNKNOWN"]),
            f"{c.case_id}-p",
        ))

    runner = Phase16QualificationCampaignRunner(
        policy=policy, corpus=corpus, candidate_bundle=bundle,
        ledger=ledger, model_port=_FakeModelPort(outcomes),
    )
    report = asyncio.run(runner.execute(
        campaign=campaign, run_id="q-runner-all-pass-run-001",
    ))
    assert report.status == "PASS", f"expected PASS got {report.status}: {report.reason_codes}"
    assert report.model_calls == 24
    m = {mt.metric_code: mt for mt in report.metric_facts}
    assert m["E2E_MULTI_AGENT_READY"].numerator == 12
    assert m["E2E_MULTI_AGENT_READY"].denominator == 12
    assert m["ANALYST_SCHEMA_AND_SEMANTIC_VALID"].numerator == 12
    assert m["HARD_SAFETY_CONFORMANCE"].numerator == 12

    # ledger 终态认证
    result = ledger.report(run_id="q-runner-all-pass-run-001")
    assert result.status is QualificationRunStatus.PASS
    assert result.authenticated is True

    # evaluator 读回
    ev = QualificationEvaluator()
    assessment = ev.assess(
        campaign=campaign, policy=policy, corpus=identity,
        candidate=bundle.candidate, metric_facts=report.metric_facts,
    )
    assert assessment.status == "DEVELOPMENT_DIAGNOSTIC"


# ---------------------------------------------------------------------------
# Test: Analyst 语义失败 → 剩余继续
# ---------------------------------------------------------------------------

def test_runner_analyst_semantic_failure_continues(ledger_factory) -> None:
    """Case 0 的 Analyst explanation 超长 → 该 case FAILED, 后 11 继续。"""
    ledger = ledger_factory()
    policy, corpus, identity, bundle, campaign = _init_campaign(
        ledger,
    )
    cases = _high_conflict_cases(corpus)
    now = datetime.now(timezone.utc)
    eid: dict[str, list[str]] = {}
    for c in cases:
        eid[c.case_id] = _evidence_ids_from_case(case=c, now=now)

    outcomes: list[ModelSuccess | ModelFailure] = []
    # Case 0: explanation 超 500 字符
    bad = {
        "kind": "FINAL",
        "final_output": {
            "constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED"],
            "risk_codes": ["HUMAN_CONFIRMATION_REQUIRED"],
            "explanation": "x" * 501,
            "evidence_ids": [eid[cases[0].case_id][0]],
        },
    }
    outcomes.append(_mk_success(bad, f"{cases[0].case_id}-a"))
    # Case 1-11: 正常 PASS
    for c in cases[1:]:
        outcomes.append(_mk_success(_analyst_output(eid[c.case_id]), f"{c.case_id}-a"))
        outcomes.append(_mk_success(
            _planner_output(eid[c.case_id], ["HUMAN_CONFIRMATION_REQUIRED", "SIDE_EFFECT_UNKNOWN"]),
            f"{c.case_id}-p",
        ))

    runner = Phase16QualificationCampaignRunner(
        policy=policy, corpus=corpus, candidate_bundle=bundle,
        ledger=ledger, model_port=_FakeModelPort(outcomes),
    )
    report = asyncio.run(runner.execute(
        campaign=campaign, run_id="q-runner-anlys-fail-run-001",
    ))
    assert report.status == "FAILED"
    assert report.model_calls == 23  # 1 analyst + 11×2
    m = {mt.metric_code: mt for mt in report.metric_facts}
    assert m["E2E_MULTI_AGENT_READY"].numerator == 11
    assert m["E2E_MULTI_AGENT_READY"].denominator == 12
    assert m["ANALYST_SCHEMA_AND_SEMANTIC_VALID"].numerator == 11


# ---------------------------------------------------------------------------
# Test: Planner 语义失败 → 剩余继续
# ---------------------------------------------------------------------------

def test_runner_planner_semantic_failure_continues(ledger_factory) -> None:
    """Case 0 的 Planner 遗漏 risk_flag → 该 case FAILED, 后 11 继续。"""
    ledger = ledger_factory()
    policy, corpus, identity, bundle, campaign = _init_campaign(
        ledger,
    )
    cases = _high_conflict_cases(corpus)
    now = datetime.now(timezone.utc)
    eid: dict[str, list[str]] = {}
    for c in cases:
        eid[c.case_id] = _evidence_ids_from_case(case=c, now=now)

    outcomes: list[ModelSuccess | ModelFailure] = []
    # Case 0: analyst PASS, planner 缺一个 risk_code
    outcomes.append(_mk_success(_analyst_output(eid[cases[0].case_id]), f"{cases[0].case_id}-a"))
    outcomes.append(_mk_success(
        _planner_output(eid[cases[0].case_id], ["HUMAN_CONFIRMATION_REQUIRED"]),
        f"{cases[0].case_id}-p",
    ))
    # Case 1-11: 正常 PASS
    for c in cases[1:]:
        outcomes.append(_mk_success(_analyst_output(eid[c.case_id]), f"{c.case_id}-a"))
        outcomes.append(_mk_success(
            _planner_output(eid[c.case_id], ["HUMAN_CONFIRMATION_REQUIRED", "SIDE_EFFECT_UNKNOWN"]),
            f"{c.case_id}-p",
        ))

    runner = Phase16QualificationCampaignRunner(
        policy=policy, corpus=corpus, candidate_bundle=bundle,
        ledger=ledger, model_port=_FakeModelPort(outcomes),
    )
    report = asyncio.run(runner.execute(
        campaign=campaign, run_id="q-runner-plnr-fail-run-001",
    ))
    assert report.status == "FAILED"
    assert report.model_calls == 24
    m = {mt.metric_code: mt for mt in report.metric_facts}
    assert m["E2E_MULTI_AGENT_READY"].numerator == 11
    assert m["E2E_MULTI_AGENT_READY"].denominator == 12
    assert m["ANALYST_SCHEMA_AND_SEMANTIC_VALID"].numerator == 12
    assert m["PLANNER_RISK_COVERAGE"].numerator == 11
    assert m["PLANNER_RISK_COVERAGE"].denominator == 12


# ---------------------------------------------------------------------------
# Test: Transport 失败阻断后续联网
# ---------------------------------------------------------------------------

def test_runner_transport_failure_blocks_network(ledger_factory) -> None:
    """首个 case ModelFailure(request_sent=False) → BLOCKED, 余下不再发网络。"""
    ledger = ledger_factory()
    policy, corpus, identity, bundle, campaign = _init_campaign(
        ledger,
    )
    cases = _high_conflict_cases(corpus)
    now = datetime.now(timezone.utc)
    eid: dict[str, list[str]] = {}
    for c in cases:
        eid[c.case_id] = _evidence_ids_from_case(case=c, now=now)

    outcomes: list[ModelSuccess | ModelFailure] = []
    # Case 0: transport error, request not sent
    outcomes.append(ModelFailure(
        request_id="placeholder", category=ModelFailureCategory.TRANSPORT_ERROR,
        request_sent=False, latency_ms=Decimal("0"),
    ))
    # Case 1-11 本应需要 outcomes，但 hard_blocked 后会跳过
    # (non-E2E 阻断，不需要额外 outcomes)

    runner = Phase16QualificationCampaignRunner(
        policy=policy, corpus=corpus, candidate_bundle=bundle,
        ledger=ledger, model_port=_FakeModelPort(outcomes),
    )
    report = asyncio.run(runner.execute(
        campaign=campaign, run_id="q-runner-transport-run-001",
    ))
    assert report.status == "BLOCKED"
    assert report.model_calls == 0  # attempt created but model never sent
    # 第一个 case BLOCKED, 剩余 11 也被 CAMPAIGN_HARD_BLOCKED
    m = {mt.metric_code: mt for mt in report.metric_facts}
    assert m["E2E_MULTI_AGENT_READY"].numerator == 0


# ---------------------------------------------------------------------------
# Test: Receipt 不完整 → case FAILED, 余下继续
# ---------------------------------------------------------------------------

def test_runner_incomplete_receipt_continues(ledger_factory) -> None:
    """Case 0 的 receipt 缺 provider_response_id → case FAILED, 后 11 继续。"""
    ledger = ledger_factory()
    policy, corpus, identity, bundle, campaign = _init_campaign(
        ledger,
    )
    cases = _high_conflict_cases(corpus)
    now = datetime.now(timezone.utc)
    eid: dict[str, list[str]] = {}
    for c in cases:
        eid[c.case_id] = _evidence_ids_from_case(case=c, now=now)

    outcomes: list[ModelSuccess | ModelFailure] = []
    # Case 0: analyst 返回但 finish_reason 不是 stop → receipt incomplete
    out0 = _analyst_output(eid[cases[0].case_id])
    outcomes.append(ModelSuccess(
        request_id="placeholder", model_id="gpt-5.6-luna", output=out0,
        usage=ModelUsage(input_tokens=50, output_tokens=50, total_tokens=100),
        provider_response_id="qual-runner-provider-bad", finish_reason="length",
        response_digest=canonical_json_sha256(out0), latency_ms=Decimal("0.500"),
        endpoint_host="synapse-ai.uk",
    ))
    # Case 1-11: 正常 PASS
    for c in cases[1:]:
        outcomes.append(_mk_success(_analyst_output(eid[c.case_id]), f"{c.case_id}-a"))
        outcomes.append(_mk_success(
            _planner_output(eid[c.case_id], ["HUMAN_CONFIRMATION_REQUIRED", "SIDE_EFFECT_UNKNOWN"]),
            f"{c.case_id}-p",
        ))

    runner = Phase16QualificationCampaignRunner(
        policy=policy, corpus=corpus, candidate_bundle=bundle,
        ledger=ledger, model_port=_FakeModelPort(outcomes),
    )
    report = asyncio.run(runner.execute(
        campaign=campaign, run_id="q-runner-receipt-run-001",
    ))
    assert report.status == "FAILED"
    assert report.model_calls == 23  # case0 analyst + 11×2
    m = {mt.metric_code: mt for mt in report.metric_facts}
    assert m["E2E_MULTI_AGENT_READY"].numerator == 11


# ---------------------------------------------------------------------------
# Test: Holdout 被 runner 拒绝
# ---------------------------------------------------------------------------

def test_runner_rejects_holdout_campaign(ledger_factory) -> None:
    """holdout campaign 没有 release loader → runner 返回 BLOCKED。"""
    ledger = ledger_factory()
    policy, corpus, identity, bundle, _campaign = _init_campaign(
        ledger,
    )
    # 创建 holdout campaign（但 runner 的 _cases_for_campaign 会拒绝）
    holdout = QualificationCampaign(
        campaign_id="q-runner-holdout-reject-camp",
        campaign_kind=QualificationCampaignKind.HOLDOUT,
        policy_digest=policy.policy_digest or "",
        corpus_digest=identity.corpus_digest,
        candidate_digest=bundle.candidate.candidate_digest or "",
        manifest_digest=corpus.manifest.manifest_digest or "",
        reservation_cny="1.000000",
        batch_index=1,
    )
    # runner 不需要将 holdout campaign 写入 ledger；
    # execute() 中的 admission 会在 PENDING corpus 时返回 BLOCKED
    runner = Phase16QualificationCampaignRunner(
        policy=policy, corpus=corpus, candidate_bundle=bundle,
        ledger=ledger, model_port=_FakeModelPort([]),
    )
    report = asyncio.run(runner.execute(
        campaign=holdout, run_id="q-runner-holdout-reject-run-001",
    ))
    assert report.status == "BLOCKED"
    assert report.model_calls == 0
    assert "HOLDOUT_COMMITMENT_REQUIRED" in report.reason_codes

    runner = Phase16QualificationCampaignRunner(
        policy=policy, corpus=corpus, candidate_bundle=bundle,
        ledger=ledger, model_port=_FakeModelPort([]),
    )
    report = asyncio.run(runner.execute(
        campaign=holdout, run_id="q-runner-holdout-reject-run-001",
    ))
    assert report.status == "BLOCKED"
    assert report.model_calls == 0
    # admission 会先拒绝 PENDING 状态的 holdout corpus
    assert "HOLDOUT_COMMITMENT_REQUIRED" in report.reason_codes
