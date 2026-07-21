"""Phase 16 Task 10 真实 smoke 预检与独立账本的 TDD 契约。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.decision_support.multi_agent_evaluation import (
    load_phase16_controlled_multi_agent_dataset,
)
from src.decision_support.multi_agent import (
    build_decision_planner_profile,
    build_evidence_analyst_profile,
)
from src.decision_support.multi_agent_smoke import (
    PHASE16_MULTI_AGENT_SMOKE,
    Phase16OfficialPriceEvidence,
    Phase16SmokeBudgetStore,
    Phase16SmokeConfig,
    Phase16SmokeRunner,
    Phase16SmokeStatus,
    phase16_smoke_runtime_digest,
    preflight_phase16_multi_agent_smoke,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelSuccess,
    ModelUsage,
)


def _dataset():
    """加载 Task 9 的不可变资产，测试不能手写另一个可绕过 Manifest 的 smoke 数据集。"""

    repository_root = Path(__file__).resolve().parents[2]
    return load_phase16_controlled_multi_agent_dataset(
        repository_root / "evaluation" / "phase16_controlled_multi_agent"
    )


def _config(**updates) -> Phase16SmokeConfig:
    """按两个冻结 Profile 和 Task 9 Manifest 构造完整 smoke 身份。"""

    dataset = _dataset()
    analyst = dataset.manifest.profile_digests["evidence_analyst"]
    planner = dataset.manifest.profile_digests["decision_planner"]
    values = {
        "manifest_id": dataset.manifest.dataset_id,
        "manifest_digest": dataset.manifest.manifest_digest,
        "dataset_digest": dataset.manifest.dataset_digest,
        "source_code_digest": dataset.manifest.source_code_digest,
        "evidence_analyst_profile_digest": analyst,
        "decision_planner_profile_digest": planner,
        "official_price_digest": "a" * 64,
        "smoke_runtime_digest": phase16_smoke_runtime_digest(),
        "model_id": "deepseek-v4-flash",
        "endpoint_host": "api.deepseek.com",
        "max_smoke_cases": 10,
        "budget_cny": Decimal("1.00"),
        "reserved_case_budget_cny": Decimal("0.10"),
        "usage_required": True,
    }
    values.update(updates)
    return Phase16SmokeConfig(**values)


def _official_price() -> Phase16OfficialPriceEvidence:
    """使用冻结官方价格摘要，而非让测试或运行时猜测模型计费。"""

    return Phase16OfficialPriceEvidence(
        model_id="deepseek-v4-flash",
        endpoint_host="api.deepseek.com",
        input_cny_per_million=Decimal("1.000000"),
        output_cny_per_million=Decimal("2.000000"),
        official_price_digest="a" * 64,
    )


def _preflight(config: Phase16SmokeConfig | None = None, **updates):
    """集中构造可信预检，调用方不能通过直接实例化结果伪造发送许可。"""

    values = {
        "dataset": _dataset(),
        "official_price": _official_price(),
        "endpoint_available": True,
        "usage_contract_available": True,
    }
    values.update(updates)
    return preflight_phase16_multi_agent_smoke(config or _config(), **values)


class _RecordingModelPort:
    """无网络 Port 记录真实 ModelRequest 形状，证明预检位于发送之前。"""

    _DEFAULT_USAGE = object()

    def __init__(self, *, usage: ModelUsage | None | object = _DEFAULT_USAGE) -> None:
        self.requests = []
        self._usage = ModelUsage(
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
        ) if usage is self._DEFAULT_USAGE else usage

    async def complete(self, request):
        """返回精确身份的最小成功响应；测试不会打开任何网络连接。"""

        self.requests.append(request)
        return ModelSuccess(
            request_id=request.request_id,
            model_id=request.model_id,
            output={},
            usage=self._usage,
            response_digest="b" * 64,
            latency_ms=Decimal("1"),
        )


class _AnalystSuccessPlannerNotSentPort:
    """模拟 Analyst 已返回 usage、但 Planner 在发送前明确失败的外部边界。"""

    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        """第一调用可计价成功，第二调用确认未发送，验证不能释放第一调用成本。"""

        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelSuccess(
                request_id=request.request_id,
                model_id=request.model_id,
                output={},
                usage=ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20),
                response_digest="d" * 64,
                latency_ms=Decimal("1"),
            )
        return ModelFailure(
            request_id=request.request_id,
            category=ModelFailureCategory.DEADLINE_EXCEEDED,
            request_sent=False,
            latency_ms=Decimal("1"),
        )


class _SequenceModelPort:
    """按调用顺序返回固定结果，覆盖 smoke 两段模型调用的 fail-closed 分支。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.requests = []

    async def complete(self, request):
        """记录请求；异常对象显式抛出，其余对象原样返回给 smoke Runner。"""

        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(request)
        return outcome


def test_preflight_blocks_every_missing_external_or_frozen_fact_before_model_send() -> None:
    """缺少价格、usage、endpoint、Manifest、代码或 Profile 事实时一律不得发送。"""

    port = _RecordingModelPort()
    runner = Phase16SmokeRunner(
        config=_config(source_code_digest="0" * 64),
        preflight=preflight_phase16_multi_agent_smoke(
            _config(source_code_digest="0" * 64),
            dataset=_dataset(),
            official_price=Phase16OfficialPriceEvidence(
                model_id="another-model",
                endpoint_host="api.deepseek.com",
                input_cny_per_million=Decimal("1.008000"),
                output_cny_per_million=Decimal("2.016000"),
                official_price_digest="c" * 64,
            ),
            endpoint_available=False,
            usage_contract_available=False,
        ),
        budget_store=Phase16SmokeBudgetStore(),
        model_port=port,
    )

    report = asyncio.run(runner.run((_dataset().manifest.smoke_eligible_case_ids[0],)))

    assert report.status is Phase16SmokeStatus.BLOCKED
    assert report.scope_id.startswith(PHASE16_MULTI_AGENT_SMOKE)
    assert report.model_request_count == 0
    assert port.requests == []
    assert {
        "ENDPOINT_UNAVAILABLE",
        "USAGE_CONTRACT_UNAVAILABLE",
        "MODEL_ID_MISMATCH",
        "SOURCE_CODE_DIGEST_MISMATCH",
    } <= set(report.reason_codes)


def test_smoke_runner_reserves_one_complete_dual_agent_case_before_exact_profile_requests() -> None:
    """每个 smoke case 先原子预约 0.10 元，再按冻结 Analyst/Planner 身份各发送一次。"""

    dataset = _dataset()
    port = _RecordingModelPort()
    store = Phase16SmokeBudgetStore()
    runner = Phase16SmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=store,
        model_port=port,
    )

    report = asyncio.run(runner.run((dataset.manifest.smoke_eligible_case_ids[0],)))

    assert report.status is Phase16SmokeStatus.PASS
    assert report.smoke_case_count == 1
    assert report.model_request_count == 2
    assert len(port.requests) == 2
    assert [request.messages[0].content for request in port.requests] == [
        build_evidence_analyst_profile().prompt_text,
        build_decision_planner_profile().prompt_text,
    ]
    assert [request.prompt_hash for request in port.requests] == [
        build_evidence_analyst_profile().prompt_hash,
        build_decision_planner_profile().prompt_hash,
    ]
    assert [request.result_schema_hash for request in port.requests] == [
        build_evidence_analyst_profile().result_schema_hash,
        build_decision_planner_profile().result_schema_hash,
    ]
    assert all(
        request.model_id == "deepseek-v4-flash"
        and request.endpoint_host == "api.deepseek.com"
        and request.temperature == Decimal("0")
        and request.deadline_at.tzinfo is not None
        for request in port.requests
    )
    assert store.snapshot().committed_cny <= Decimal("1.00")


def test_unknown_usage_after_analyst_send_settles_whole_case_and_blocks_planner() -> None:
    """外部请求一旦发出但 usage 不明，必须保守消耗 case reservation 且不继续第二个 Agent。"""

    port = _RecordingModelPort(usage=None)
    store = Phase16SmokeBudgetStore()
    runner = Phase16SmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=store,
        model_port=port,
    )

    report = asyncio.run(runner.run((_dataset().manifest.smoke_eligible_case_ids[0],)))

    assert report.status is Phase16SmokeStatus.INCONCLUSIVE
    assert report.unknown_usage_case_count == 1
    assert report.model_request_count == 1
    assert len(port.requests) == 1
    assert store.snapshot().committed_cny == Decimal("0.10")
    assert "USAGE_UNKNOWN_SETTLED_AT_RESERVATION" in report.reason_codes


def test_runner_rejects_non_smoke_case_and_more_than_frozen_ten_cases() -> None:
    """只有 Manifest 明确标注的十个高冲突 case 可以进入真实 smoke，不能临时扩大样本。"""

    dataset = _dataset()
    runner = Phase16SmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=Phase16SmokeBudgetStore(),
        model_port=_RecordingModelPort(),
    )

    with pytest.raises(ValueError, match="smoke eligible"):
        asyncio.run(runner.run((dataset.manifest.case_ids["development"][0],)))
    with pytest.raises(ValueError, match="10"):
        asyncio.run(
            runner.run(
                (*dataset.manifest.smoke_eligible_case_ids, dataset.manifest.case_ids["holdout"][0])
            )
        )


def test_manually_constructed_preflight_cannot_open_model_port() -> None:
    """公共 Pydantic 对象即使声明 can_send 也没有模块内部 provenance，不能成为旁路。"""

    from src.decision_support.multi_agent_smoke import Phase16SmokePreflight

    config = _config()
    scope_id = PHASE16_MULTI_AGENT_SMOKE
    forged = Phase16SmokePreflight(
        status=Phase16SmokeStatus.PASS,
        can_send=True,
        config_digest="f" * 64,
        scope_id=scope_id,
        max_smoke_cases=10,
        reserved_case_budget_cny=Decimal("0.10"),
    )
    port = _RecordingModelPort()
    runner = Phase16SmokeRunner(
        config=config,
        preflight=forged,
        budget_store=Phase16SmokeBudgetStore(scope_id=scope_id),
        model_port=port,
    )

    report = asyncio.run(runner.run((_dataset().manifest.smoke_eligible_case_ids[0],)))

    assert report.status is Phase16SmokeStatus.BLOCKED
    assert report.model_request_count == 0
    assert port.requests == []


def test_smoke_scope_is_singleton_and_ten_low_cost_settlements_cannot_open_eleventh_case() -> None:
    """真实 smoke 只有唯一一元池，十例即使实际成本很低也不能再预约第十一例。"""

    from src.decision_support.multi_agent_smoke import (
        Phase16SmokeBudgetInvariantError,
        Phase16SmokeBudgetLimitExceeded,
    )

    with pytest.raises(Phase16SmokeBudgetInvariantError, match="scope"):
        Phase16SmokeBudgetStore(scope_id=f"{PHASE16_MULTI_AGENT_SMOKE}:bypass")

    store = Phase16SmokeBudgetStore()
    for index in range(10):
        request_id = f"settled-low-cost-{index:02d}"
        store.reserve(request_id, Decimal("0.10"))
        store.settle(request_id, Decimal("0.000001"))

    with pytest.raises(Phase16SmokeBudgetLimitExceeded):
        store.reserve("settled-low-cost-eleventh", Decimal("0.10"))


def test_planner_not_sent_after_analyst_success_keeps_analyst_cost_and_consumes_case_slot() -> None:
    """第二次调用未发送也不能回滚已经发生的 Analyst 消费或重新开放该 smoke case。"""

    store = Phase16SmokeBudgetStore()
    port = _AnalystSuccessPlannerNotSentPort()
    runner = Phase16SmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=store,
        model_port=port,
    )

    report = asyncio.run(runner.run((_dataset().manifest.smoke_eligible_case_ids[0],)))

    assert report.status is Phase16SmokeStatus.FAIL
    assert report.model_request_count == 2
    assert store.snapshot().committed_cny > Decimal("0")
    assert store.snapshot().reserved_cny == Decimal("0")


def test_preflight_revalidates_task9_dataset_before_caching_model_visible_facts() -> None:
    """同进程嵌套字典被修改后，预检必须在 Model Port 前拒绝旧 Manifest 身份。"""

    dataset = _dataset()
    dataset.cases[0].input["backup_inventory"] = 999_999

    preflight = preflight_phase16_multi_agent_smoke(
        _config(),
        dataset=dataset,
        official_price=_official_price(),
        endpoint_available=True,
        usage_contract_available=True,
    )

    assert preflight.status is Phase16SmokeStatus.BLOCKED
    assert preflight.can_send is False
    assert "TASK9_DATASET_INVALID" in preflight.reason_codes


def test_replay_preserves_unknown_usage_inconclusive_result_without_second_model_send() -> None:
    """重启后的同一 case 必须恢复持久化 INCONCLUSIVE，不能把 settled 误报为 PASS。"""

    store = Phase16SmokeBudgetStore()
    dataset = _dataset()
    preflight = preflight_phase16_multi_agent_smoke(
        _config(),
        dataset=dataset,
        official_price=_official_price(),
        endpoint_available=True,
        usage_contract_available=True,
    )
    first_port = _RecordingModelPort(usage=None)
    first = Phase16SmokeRunner(
        config=_config(),
        preflight=preflight,
        budget_store=store,
        model_port=first_port,
    )
    case_id = dataset.manifest.smoke_eligible_case_ids[0]
    assert asyncio.run(first.run((case_id,))).status is Phase16SmokeStatus.INCONCLUSIVE

    replay_port = _RecordingModelPort()
    replay = Phase16SmokeRunner(
        config=_config(),
        preflight=preflight,
        budget_store=store,
        model_port=replay_port,
    )
    report = asyncio.run(replay.run((case_id,)))

    assert report.status is Phase16SmokeStatus.INCONCLUSIVE
    assert report.replayed_case_count == 1
    assert report.model_request_count == 0
    assert replay_port.requests == []


def test_runner_revalidates_dataset_again_after_preflight_before_any_model_send() -> None:
    """预检签发后即使同进程修改可变 case.input，Runner 也必须 BLOCKED 且零发送。"""

    dataset = _dataset()
    preflight = preflight_phase16_multi_agent_smoke(
        _config(),
        dataset=dataset,
        official_price=_official_price(),
        endpoint_available=True,
        usage_contract_available=True,
    )
    dataset.cases[0].input["backup_inventory"] = 777_777
    port = _RecordingModelPort()
    runner = Phase16SmokeRunner(
        config=_config(),
        preflight=preflight,
        budget_store=Phase16SmokeBudgetStore(),
        model_port=port,
    )

    report = asyncio.run(runner.run((dataset.manifest.smoke_eligible_case_ids[0],)))

    assert report.status is Phase16SmokeStatus.BLOCKED
    assert "TASK9_DATASET_INVALID" in report.reason_codes
    assert report.model_request_count == 0
    assert port.requests == []


def test_missing_task9_asset_is_blocked_before_model_port(monkeypatch) -> None:
    """生成器或源码闭包文件不可读时也必须稳定 BLOCKED，不能把文件异常泄漏到发送路径。"""

    dataset = _dataset()
    preflight = preflight_phase16_multi_agent_smoke(
        _config(),
        dataset=dataset,
        official_price=_official_price(),
        endpoint_available=True,
        usage_contract_available=True,
    )

    def missing_asset(_dataset_value):
        """模拟冻结 generator/source closure 在运行时被删除。"""

        raise FileNotFoundError("phase16 frozen asset is unavailable")

    monkeypatch.setattr("src.decision_support.multi_agent_smoke._validate_dataset_for_run", missing_asset)
    port = _RecordingModelPort()
    runner = Phase16SmokeRunner(
        config=_config(),
        preflight=preflight,
        budget_store=Phase16SmokeBudgetStore(),
        model_port=port,
    )

    report = asyncio.run(runner.run((dataset.manifest.smoke_eligible_case_ids[0],)))

    assert report.status is Phase16SmokeStatus.BLOCKED
    assert report.reason_codes == ("TASK9_DATASET_INVALID",)
    assert port.requests == []


def test_memory_ledger_rejects_outcomes_not_representable_by_postgresql() -> None:
    """内存 Store 也只能记录 DDL 允许的结果，未发送 release 不能伪造 PASS。"""

    from src.decision_support.multi_agent_smoke import Phase16SmokeBudgetInvariantError

    store = Phase16SmokeBudgetStore()
    store.reserve("invalid-outcome-settle", Decimal("0.10"))
    with pytest.raises(Phase16SmokeBudgetInvariantError, match="outcome"):
        store.settle(
            "invalid-outcome-settle",
            Decimal("0.01"),
            outcome_status=Phase16SmokeStatus.BLOCKED,
        )

    store.reserve("invalid-outcome-release", Decimal("0.10"))
    with pytest.raises(Phase16SmokeBudgetInvariantError, match="release"):
        store.release(
            "invalid-outcome-release",
            outcome_status=Phase16SmokeStatus.PASS,
        )


def test_smoke_unknown_response_or_identity_mismatch_is_inconclusive_and_settles_reservation() -> None:
    """未知结果、request_id 漂移和 model_id 漂移都不能继续 Planner，必须保守结算整例。"""

    dataset = _dataset()
    case_id = dataset.manifest.smoke_eligible_case_ids[0]
    outcomes = [
        lambda current: ModelSuccess(
            request_id="foreign-request-id",
            model_id=current.model_id,
            output={},
            usage=ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20),
            response_digest="b" * 64,
            latency_ms=Decimal("1"),
        ),
        lambda current: ModelSuccess(
            request_id=current.request_id,
            model_id="foreign-model-id",
            output={},
            usage=ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20),
            response_digest="b" * 64,
            latency_ms=Decimal("1"),
        ),
        object(),
    ]

    for outcome in outcomes:
        port = _SequenceModelPort([outcome])
        report = asyncio.run(
            Phase16SmokeRunner(
                config=_config(),
                preflight=_preflight(),
                budget_store=Phase16SmokeBudgetStore(),
                model_port=port,
            ).run((case_id,))
        )

        assert report.status is Phase16SmokeStatus.INCONCLUSIVE
        assert report.reason_codes == ("MODEL_IDENTITY_OR_RESPONSE_MISMATCH",)
        assert report.model_request_count == 1
        assert report.unknown_usage_case_count == 1
        assert len(port.requests) == 1


def test_smoke_unsent_model_failure_releases_case_but_sent_failure_is_degraded_to_inconclusive() -> None:
    """未发送失败可释放 reservation；已发送失败无法证明成本，必须保持 INCONCLUSIVE。"""

    dataset = _dataset()
    case_id = dataset.manifest.smoke_eligible_case_ids[0]
    for request_sent, expected_status, expected_reason, expected_committed in (
        (False, Phase16SmokeStatus.FAIL, "MODEL_REQUEST_NOT_SENT", Decimal("0")),
        (True, Phase16SmokeStatus.INCONCLUSIVE, "MODEL_FAILURE_USAGE_UNKNOWN", Decimal("0.10")),
    ):
        port = _SequenceModelPort(
            [
                lambda request: ModelFailure(
                    request_id=request.request_id,
                    category=ModelFailureCategory.TRANSPORT_ERROR,
                    request_sent=request_sent,
                    latency_ms=Decimal("1"),
                )
            ]
        )
        store = Phase16SmokeBudgetStore()
        report = asyncio.run(
            Phase16SmokeRunner(
                config=_config(),
                preflight=_preflight(),
                budget_store=store,
                model_port=port,
            ).run((case_id,))
        )

        assert report.status is expected_status
        assert report.reason_codes == (expected_reason,)
        assert report.settled_cost_cny == expected_committed


def test_smoke_model_port_exception_and_usage_overrun_are_conservatively_degraded() -> None:
    """Port 异常或 usage 超出 case reservation 时，不能释放或继续第二个模型。"""

    dataset = _dataset()
    case_id = dataset.manifest.smoke_eligible_case_ids[0]
    for outcome, expected_reason in (
        (RuntimeError("transport unavailable"), "MODEL_PORT_EXCEPTION_USAGE_UNKNOWN"),
        (asyncio.TimeoutError(), "MODEL_PORT_EXCEPTION_USAGE_UNKNOWN"),
        (
            lambda request: ModelSuccess(
                request_id=request.request_id,
                model_id=request.model_id,
                output={},
                usage=ModelUsage(input_tokens=110_000, output_tokens=0, total_tokens=110_000),
                response_digest="b" * 64,
                latency_ms=Decimal("1"),
            ),
            "USAGE_EXCEEDS_CASE_RESERVATION",
        ),
    ):
        port = _SequenceModelPort([outcome])
        report = asyncio.run(
            Phase16SmokeRunner(
                config=_config(),
                preflight=_preflight(),
                budget_store=Phase16SmokeBudgetStore(),
                model_port=port,
            ).run((case_id,))
        )

        assert report.status is Phase16SmokeStatus.INCONCLUSIVE
        assert report.reason_codes == (expected_reason,)
        assert report.model_request_count == 1
        assert len(port.requests) == 1


def test_smoke_cancellation_propagates_and_keeps_pending_reservation_for_recovery() -> None:
    """调用方取消不能伪造失败或释放费用，未决 reservation 留给恢复扫描处理。"""

    dataset = _dataset()
    case_id = dataset.manifest.smoke_eligible_case_ids[0]
    store = Phase16SmokeBudgetStore()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            Phase16SmokeRunner(
                config=_config(),
                preflight=_preflight(),
                budget_store=store,
                model_port=_SequenceModelPort([asyncio.CancelledError()]),
            ).run((case_id,))
        )

    assert store.snapshot().reserved_cny == Decimal("0.10")
    assert store.snapshot().committed_cny == Decimal("0")
