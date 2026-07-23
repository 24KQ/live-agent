"""Phase 16 正式真实模型证据 Task 1 的离线冻结契约。

这些测试刻意只验证 Profile、回执、Manifest 与预检身份，不创建数据库连接，也不会构造
可联网的模型端口。正式发送必须等后续账本和 Runner 均完成后才允许发生。
"""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import subprocess

import pytest

from src.decision_support.multi_agent import (
    build_decision_planner_profile,
    build_evidence_analyst_profile,
)
from src.decision_support.official_smoke_evidence import (
    FORMAL_OFFICIAL_SMOKE_EXECUTION_IDENTITY_PATHS,
    FORMAL_OFFICIAL_SMOKE_MANIFEST_PATH,
    PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_AUDIT_PATH,
    PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_PATHS,
    PHASE16_OFFICIAL_SMOKE_EVIDENCE_ANALYST_PROFILE_ID,
    PHASE16_OFFICIAL_SMOKE_EVIDENCE_PLANNER_PROFILE_ID,
    PHASE16_OFFICIAL_SMOKE_RUN_ID,
    Phase16OfficialPriceEvidence,
    Phase16OfficialSmokeReceiptError,
    Phase16OfficialSmokeEnvironment,
    Phase16OfficialSmokeStatus,
    build_phase16_official_smoke_evidence_manifest,
    build_phase16_official_smoke_profile_registry,
    build_phase16_smoke_evidence_analyst_profile,
    build_phase16_smoke_evidence_planner_profile,
    load_phase16_official_smoke_evidence_manifest,
    verify_phase16_official_smoke_historical_closure_audit,
    preflight_phase16_official_smoke_evidence,
    validate_phase16_official_smoke_receipt,
)
from src.decision_support.multi_agent_evaluation import (
    load_phase16_controlled_multi_agent_dataset,
)
from src.specialist_runtime.registry import SpecialistProfileRegistry, SpecialistProfileResolutionError
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage


def _repository_root() -> Path:
    """返回测试使用的仓库根目录，避免把本机工作目录编码进冻结资产。"""

    return Path(__file__).resolve().parents[2]


def _dataset():
    """通过既有严格加载器读取 Task 9 冻结资产，禁止手写自由 smoke case。"""

    return load_phase16_controlled_multi_agent_dataset(
        _repository_root() / "evaluation" / "phase16_controlled_multi_agent"
    )


def _official_price() -> Phase16OfficialPriceEvidence:
    """构造用户已提供的官方 cache-miss 价格证据，不读取密钥或访问网络。"""

    return Phase16OfficialPriceEvidence.create(
        model_id="deepseek-v4-flash",
        endpoint_host="api.deepseek.com",
        input_cny_per_million=Decimal("1.000000"),
        output_cny_per_million=Decimal("2.000000"),
    )


def _execution_blob_sha256(*, execution_commit: str, relative_path: str) -> str:
    """直接读取执行提交中的 Git blob，防止工作树后续整改改写历史审计结论。"""

    result = subprocess.run(
        ["git", "show", f"{execution_commit}:{relative_path}"],
        cwd=_repository_root(),
        check=True,
        capture_output=True,
    )
    return sha256(result.stdout).hexdigest()


def test_live_profiles_are_fixed_and_smoke_profiles_are_isolated() -> None:
    """LIVE 工厂不能再被调用方改写 deadline/token，Smoke 身份也不得混入 LIVE Registry。"""

    with pytest.raises(TypeError):
        build_evidence_analyst_profile(deadline_seconds=30)
    with pytest.raises(TypeError):
        build_decision_planner_profile(deadline_seconds=30)

    live_analyst = build_evidence_analyst_profile()
    live_planner = build_decision_planner_profile()
    smoke_analyst = build_phase16_smoke_evidence_analyst_profile()
    smoke_planner = build_phase16_smoke_evidence_planner_profile()

    assert (live_analyst.deadline_seconds, live_analyst.max_total_tokens) == (2, 1200)
    assert (live_planner.deadline_seconds, live_planner.max_total_tokens) == (2, 2800)
    assert smoke_analyst.profile_id == PHASE16_OFFICIAL_SMOKE_EVIDENCE_ANALYST_PROFILE_ID
    assert smoke_planner.profile_id == PHASE16_OFFICIAL_SMOKE_EVIDENCE_PLANNER_PROFILE_ID
    assert {
        (smoke_analyst.deadline_seconds, smoke_analyst.max_total_tokens, smoke_analyst.max_output_tokens),
        (smoke_planner.deadline_seconds, smoke_planner.max_total_tokens, smoke_planner.max_output_tokens),
    } == {(30, 4000, 2800)}
    assert smoke_analyst.allowed_skill_ids == ()
    assert smoke_planner.allowed_skill_ids == ()
    assert smoke_analyst.max_model_calls == smoke_planner.max_model_calls == 1

    live_registry = SpecialistProfileRegistry((live_analyst, live_planner))
    with pytest.raises(SpecialistProfileResolutionError):
        live_registry.resolve_identity(smoke_analyst.profile_id, smoke_analyst.profile_version)

    smoke_registry = build_phase16_official_smoke_profile_registry()
    assert {
        profile.profile_id for profile in smoke_registry.list_profiles()
    } == {
        PHASE16_OFFICIAL_SMOKE_EVIDENCE_ANALYST_PROFILE_ID,
        PHASE16_OFFICIAL_SMOKE_EVIDENCE_PLANNER_PROFILE_ID,
    }


def test_official_manifest_and_preflight_bind_exact_dataset_profiles_price_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史 run 的当前源码漂移必须阻断；若身份重建一致，环境和价格仍决定发送许可。"""

    dataset = _dataset()
    price = _official_price()
    manifest = build_phase16_official_smoke_evidence_manifest(
        repository_root=_repository_root(),
        dataset=dataset,
        official_price=price,
    )

    assert manifest.run_id == PHASE16_OFFICIAL_SMOKE_RUN_ID
    assert manifest.parent_dataset_id == dataset.manifest.dataset_id
    assert manifest.case_ids == dataset.manifest.smoke_eligible_case_ids
    assert len(manifest.case_ids) == 10
    assert manifest.official_price_digest == price.official_price_digest
    assert manifest.profile_digests["analyst"] == build_phase16_smoke_evidence_analyst_profile().profile_digest
    assert manifest.profile_digests["planner"] == build_phase16_smoke_evidence_planner_profile().profile_digest

    stored = load_phase16_official_smoke_evidence_manifest(
        repository_root=_repository_root(),
        manifest_path=FORMAL_OFFICIAL_SMOKE_MANIFEST_PATH,
    )
    # 正式 v1 已在 a2e70a7 执行，之后的账本安全整改会改变当前源码摘要。允许“当前工作树
    # 重建”覆盖历史 Manifest 会把审计变成可变事实，因此真实预检必须 fail-closed。
    current_source_blocked = preflight_phase16_official_smoke_evidence(
        dataset=dataset,
        official_price=price,
        environment=Phase16OfficialSmokeEnvironment(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )
    assert current_source_blocked.status is Phase16OfficialSmokeStatus.BLOCKED
    assert current_source_blocked.can_send is False
    assert "FORMAL_MANIFEST_MISMATCH" in current_source_blocked.reason_codes

    # 下面的局部替身只模拟“执行前源码尚未漂移”的可信重建结果，用来继续覆盖 READY
    # 分支；真正的历史身份由独立 Git-blob 审计测试复验，不能由此替身替代。
    from src.decision_support import official_smoke_evidence as evidence_module

    monkeypatch.setattr(
        evidence_module,
        "build_phase16_official_smoke_evidence_manifest",
        lambda **_kwargs: stored,
    )

    allowed = preflight_phase16_official_smoke_evidence(
        dataset=dataset,
        official_price=price,
        environment=Phase16OfficialSmokeEnvironment(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )
    assert allowed.status is Phase16OfficialSmokeStatus.READY
    assert allowed.can_send is True

    blocked = preflight_phase16_official_smoke_evidence(
        dataset=dataset,
        official_price=Phase16OfficialPriceEvidence.create(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            input_cny_per_million=Decimal("1.000000"),
            output_cny_per_million=Decimal("2.100000"),
        ),
        environment=Phase16OfficialSmokeEnvironment(
            model_id="another-model",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )
    assert blocked.status is Phase16OfficialSmokeStatus.BLOCKED
    assert blocked.can_send is False
    assert {"MODEL_ID_MISMATCH", "OFFICIAL_PRICE_MISMATCH"} <= set(blocked.reason_codes)


def test_preflight_fails_closed_when_canonical_manifest_cannot_be_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用方传入的对象不是权威 Manifest；磁盘冻结资产不可读时必须阻断发送。"""

    from src.decision_support import official_smoke_evidence as evidence_module

    def _unreadable_manifest(**_kwargs):
        """模拟正式执行前冻结资产丢失或损坏，不向预检泄漏底层异常。"""

        raise ValueError("manifest unavailable")

    monkeypatch.setattr(
        evidence_module,
        "load_phase16_official_smoke_evidence_manifest",
        _unreadable_manifest,
    )
    blocked = preflight_phase16_official_smoke_evidence(
        dataset=_dataset(),
        official_price=_official_price(),
        environment=Phase16OfficialSmokeEnvironment(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )

    assert blocked.status is Phase16OfficialSmokeStatus.BLOCKED
    assert blocked.can_send is False
    assert "FORMAL_MANIFEST_UNREADABLE" in blocked.reason_codes


def test_preflight_marks_only_factory_results_as_trusted_for_future_dispatch() -> None:
    """公共 Pydantic 对象不能手工伪造成可发送的预检许可。"""

    from src.decision_support import official_smoke_evidence as evidence_module

    forged = evidence_module.Phase16OfficialSmokePreflight.model_construct(
        status=Phase16OfficialSmokeStatus.READY,
        can_send=True,
        manifest_digest="a" * 64,
        _verified=True,
    )
    assert forged.provenance_verified is False

    verified = preflight_phase16_official_smoke_evidence(
        dataset=_dataset(),
        official_price=_official_price(),
        environment=Phase16OfficialSmokeEnvironment(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )
    assert verified.provenance_verified is True


def test_formal_smoke_receipt_requires_provider_id_and_finish_reason() -> None:
    """正式账本只接受有完整供应商回执的已发送成功，普通 Runtime 的可选字段不能绕过此门。"""

    common = {
        "request_id": "formal-request-001",
        "model_id": "deepseek-v4-flash",
        "output": {"kind": "FINAL"},
        "usage": ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        "response_digest": "a" * 64,
        "latency_ms": Decimal("1"),
    }
    with pytest.raises(Phase16OfficialSmokeReceiptError, match="provider_response_id"):
        validate_phase16_official_smoke_receipt(ModelSuccess(**common, finish_reason="stop"))
    with pytest.raises(Phase16OfficialSmokeReceiptError, match="finish_reason"):
        validate_phase16_official_smoke_receipt(
            ModelSuccess(**common, provider_response_id="chatcmpl-formal-001")
        )

    validate_phase16_official_smoke_receipt(
        ModelSuccess(
            **common,
            provider_response_id="chatcmpl-formal-001",
            finish_reason="stop",
        )
    )


def test_historical_execution_closure_audit_expands_manifest_identity_without_rewriting_run() -> None:
    """已执行 Manifest 保持不可变，独立审计必须补齐其遗漏的一方输入和验证依赖。"""

    ledger_path = "src/decision_support/official_smoke_ledger.py"
    runner_path = "src/decision_support/official_smoke_runner.py"
    evidence_path = "src/decision_support/evidence.py"
    workspace_path = "src/decision_support/models.py"
    evaluation_path = "src/decision_support/multi_agent_evaluation.py"
    store_path = "src/decision_support/store.py"
    resolver_path = "src/specialist_runtime/evidence.py"
    assert ledger_path in FORMAL_OFFICIAL_SMOKE_EXECUTION_IDENTITY_PATHS
    assert runner_path in FORMAL_OFFICIAL_SMOKE_EXECUTION_IDENTITY_PATHS
    assert {
        ledger_path,
        runner_path,
        evidence_path,
        workspace_path,
        evaluation_path,
        store_path,
        resolver_path,
    } <= set(PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_PATHS)

    manifest = load_phase16_official_smoke_evidence_manifest(
        repository_root=_repository_root(),
        manifest_path=FORMAL_OFFICIAL_SMOKE_MANIFEST_PATH,
    )
    audit = verify_phase16_official_smoke_historical_closure_audit(
        repository_root=_repository_root()
    )

    assert (_repository_root() / PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_AUDIT_PATH).is_file()
    assert audit.execution_manifest_digest == manifest.manifest_digest
    assert audit.execution_identity_paths == FORMAL_OFFICIAL_SMOKE_EXECUTION_IDENTITY_PATHS
    assert dict(audit.execution_identity_file_digests) == dict(manifest.source_file_digests)
    assert set(audit.source_file_digests) == set(PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_PATHS)
    for relative_path, digest in audit.source_file_digests.items():
        assert digest == _execution_blob_sha256(
            execution_commit=audit.execution_commit,
            relative_path=relative_path,
        )
