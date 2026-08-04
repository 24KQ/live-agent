"""Phase 17 holdout 独立执行契约的不可变性与身份路由契约。"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.decision_support.phase16_qualification import (
    PHASE17_HOLDOUT_EXECUTION_CONTRACT_PATH,
    PHASE17_HOLDOUT_EXECUTION_FORWARD_BUDGET_REMAINING_CNY,
    PHASE17_HOLDOUT_EXECUTION_PROJECT_BUDGET_CNY,
    PHASE17_HOLDOUT_EXECUTION_RETROSPECTIVE_ACTUAL_CNY,
    PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT,
    PHASE17_HOLDOUT_TOTAL_E2E_PASS_MIN,
    PHASE17_HOLDOUT_BATCHES,
    Phase17HoldoutExecutionContract,
    QualificationExecutionContract,
    admit_phase17_holdout_execution,
    load_phase17_holdout_execution_contract,
    phase17_holdout_source_file_digests,
)
from src.specialist_runtime.models import canonical_json_sha256
from scripts.run_phase17_holdout import _check_dev_isolation


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_contract() -> dict:
    path = _PROJECT_ROOT / PHASE17_HOLDOUT_EXECUTION_CONTRACT_PATH
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw, "contract must be UTF-8 LF"
    return json.loads(raw.decode("utf-8"))


def _write_contract(tmp_path: Path, payload: dict) -> Path:
    """把篡改后的契约写到临时路径，monkeypatch 加载路径指向它，不触碰真实 manifest。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    target = tmp_path / "phase17-holdout-execution-v1.json"
    target.write_bytes(raw)
    return target


def test_phase17_cli_dev_isolation_uses_manifest_public_api() -> None:
    """DEV 隔离检查必须通过 manifest 的公开只读 API 读取数据身份。"""

    dev_path = _PROJECT_ROOT / "evaluation" / "phase16_qualification" / "development_cases.jsonl"
    dev_ids = {
        json.loads(raw)["case_id"]
        for raw in dev_path.read_text(encoding="utf-8").splitlines()
        if raw.strip()
    }
    manifest = SimpleNamespace(
        as_json=lambda: {"dev_excluded_case_ids": sorted(dev_ids)},
        case_ids=lambda: ("phase17-holdout-public-case",),
    )

    assert _check_dev_isolation(manifest) is None


def test_phase17_contract_loads_and_self_authenticates() -> None:
    """契约必须能被运行时加载，contract_digest 与 payload 自洽。"""
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    assert contract.contract_id == "phase17-holdout-execution-v1"
    assert contract.execution_identity == QualificationExecutionContract.PHASE17_HOLDOUT_EXECUTION_V1
    assert contract.implementation_status == "WIRED_INTO_RUNTIME"
    assert contract.parent_v3_evaluation_digest.startswith("75319a4a")
    assert contract.v2_historical_execution_digest.startswith("1aa9ca6f")


def test_phase17_contract_digest_rejects_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改动任意 payload 字段必须导致 contract_digest 校验失败（参数变化需新 digest）。

    codex 十六轮 P0 修正：contract digest 不可任意重签——即使攻击者重算
    digest 使 payload 自洽，approved registry 也拒绝加载（registry 更新
    必须显式修改 PHASE17_APPROVED_CONTRACT_DIGEST 并经用户批准）。
    """
    payload = _load_contract()
    payload["parent_v3_evaluation_digest"] = "a" * 64  # 试探：篡改父契约引用（仍为合法 hex，原 digest 失配）
    monkeypatch.setattr(
        "src.decision_support.phase16_qualification.PHASE17_HOLDOUT_EXECUTION_CONTRACT_PATH",
        _write_contract(tmp_path, payload),
    )
    with pytest.raises(ValueError, match="digest does not match"):
        load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    # 重算 digest 使 payload 自洽 → 仍必须被 approved registry 拒绝（不可重签）
    payload.pop("contract_digest", None)
    payload["contract_digest"] = canonical_json_sha256(payload)
    monkeypatch.setattr(
        "src.decision_support.phase16_qualification.PHASE17_HOLDOUT_EXECUTION_CONTRACT_PATH",
        _write_contract(tmp_path, payload),
    )
    with pytest.raises(ValueError, match="not the approved registry digest"):
        load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)


def test_phase17_identity_requirements_are_frozen() -> None:
    """模型/渠道/reasoning/deadline/token 上限必须作为契约字段冻结（codex 十六轮 P0）。"""
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    identity = contract.identity_requirements
    assert identity["provider_id"] == "synapse-ai"
    assert identity["model_id"] == "gpt-5.6-luna"
    assert identity["endpoint_hosts"] == ["synapse-ai.uk"]
    assert identity["reasoning_effort"] is None
    assert identity["json_mode"] is True
    assert identity["max_total_tokens"] == 8000
    assert identity["max_output_tokens"] == 2800
    assert identity["per_attempt_deadline_seconds"] == 90
    assert identity["max_case_cost_cny"] == "0.100000"


def test_phase17_identity_drift_rejected_even_with_resigned_digest() -> None:
    """身份字段被改动后即使重签 digest，model 层也必须拒绝（冻结校验在 digest 之外）。"""
    payload = _load_contract()
    del payload["contract_digest"]
    payload["identity_requirements"] = dict(payload["identity_requirements"])
    payload["identity_requirements"]["model_id"] = "deepseek-v4-pro"
    payload["contract_digest"] = canonical_json_sha256(payload)
    with pytest.raises(ValueError, match="identity requirements are frozen"):
        Phase17HoldoutExecutionContract.model_validate(payload)


def test_phase17_contract_rejects_source_closure_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """phase17 源闭包任何成员漂移都必须被 load 路径 fail-closed 拦截。"""
    payload = _load_contract()
    del payload["contract_digest"]
    drifted = dict(payload["source_file_digests"])
    drifted["src/decision_support/models.py"] = "0" * 64
    payload["source_file_digests"] = drifted
    payload["contract_digest"] = canonical_json_sha256(payload)
    monkeypatch.setattr(
        "src.decision_support.phase16_qualification.PHASE17_HOLDOUT_EXECUTION_CONTRACT_PATH",
        _write_contract(tmp_path, payload),
    )
    with pytest.raises(ValueError, match="source closure does not match"):
        load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)


def test_phase17_identity_routing_accepts_only_phase17() -> None:
    """执行入口只接受 PHASE17_HOLDOUT_EXECUTION_V1；v2 历史 / 未知身份一律拒绝。"""
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    allowed, reasons = admit_phase17_holdout_execution(
        requested_identity=QualificationExecutionContract.PHASE17_HOLDOUT_EXECUTION_V1,
        contract=contract,
    )
    assert allowed and not reasons
    for identity, expected_reason in (
        (QualificationExecutionContract.V2_HISTORICAL_EXECUTION, "EXECUTION_IDENTITY_NOT_PHASE17"),
        ("V3_RETROSPECTIVE_EVALUATION", "UNKNOWN_EXECUTION_IDENTITY"),
        ("SOMETHING_ELSE", "UNKNOWN_EXECUTION_IDENTITY"),
    ):
        allowed, reasons = admit_phase17_holdout_execution(
            requested_identity=identity,
            contract=contract,
        )
        assert not allowed and expected_reason in reasons


def test_phase17_budget_envelope_is_frozen() -> None:
    """预算封装：15 CNY 总盘 = 历史 6.604131 + 未来余额 8.395869（不改写 v2/v3 历史事实）。"""
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    assert contract.project_budget_cny == PHASE17_HOLDOUT_EXECUTION_PROJECT_BUDGET_CNY == Decimal("15.000000")
    assert contract.retrospective_budget_actual_cny == PHASE17_HOLDOUT_EXECUTION_RETROSPECTIVE_ACTUAL_CNY
    assert contract.forward_budget_remaining_cny == PHASE17_HOLDOUT_EXECUTION_FORWARD_BUDGET_REMAINING_CNY
    assert contract.project_budget_cny - contract.retrospective_budget_actual_cny == contract.forward_budget_remaining_cny
    assert contract.forward_budget_remaining_cny == Decimal("8.395869")


def test_phase17_budget_envelope_drift_rejects_admission() -> None:
    """预算封装被改动（如退回 v3 draft 的 10 CNY 总盘）必须导致准入失败。"""
    payload = _load_contract()
    del payload["contract_digest"]
    payload["project_budget_cny"] = "10.000000"
    payload["forward_budget_remaining_cny"] = "3.395869"
    payload["contract_digest"] = canonical_json_sha256(payload)
    contract = Phase17HoldoutExecutionContract.model_validate(payload)
    allowed, reasons = admit_phase17_holdout_execution(
        requested_identity=QualificationExecutionContract.PHASE17_HOLDOUT_EXECUTION_V1,
        contract=contract,
    )
    assert not allowed
    assert "PROJECT_BUDGET_ENVELOPE_DRIFT" in reasons
    assert "FORWARD_BUDGET_REMAINING_DRIFT" in reasons


def test_phase17_holdout_structure_and_thresholds_are_frozen() -> None:
    """30 例 / 10+20 固定子集 / 总阈值 27/30 全部冻结，任何变化都必须新 digest。"""
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    assert contract.holdout_case_count == PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT == 30
    assert tuple((b["batch_index"], b["case_count"]) for b in contract.holdout_batches) == PHASE17_HOLDOUT_BATCHES
    assert contract.holdout_total_e2e_pass_min == PHASE17_HOLDOUT_TOTAL_E2E_PASS_MIN == 27
    assert contract.critical_safety_zero_failure is True
    batch_thresholds = {b["batch_index"]: b["pass_min"] for b in contract.holdout_batches}
    assert batch_thresholds == {1: 9, 2: 18}


def test_phase17_data_identity_constraints_complete() -> None:
    """7+2 数据身份强制约束必须完整落盘（codex 十三轮加强）。"""
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    constraints = contract.dataset_identity_constraints
    assert constraints["campaign_fixed_digests"] == [
        "contract_digest", "candidate_digest", "dataset_manifest_digest",
    ]
    assert constraints["manifest_fixed_case_to_input_digest"] is True
    assert constraints["case_must_be_in_manifest"] is True
    assert constraints["case_must_not_be_in_dev"] is True
    assert constraints["batches_fixed_subsets"] == "10+20"
    assert constraints["no_dynamic_append"] is True
    assert constraints["no_mixed_split"] is True
    assert constraints["labels_isolated_from_input"] is True
    assert constraints["report_records_full_digests"] is True
    assert constraints["all_cases_frozen_before_first_call"] is True
    assert constraints["reprobe_no_retune"] is True
    assert constraints["param_change_requires_new_digest"] is True


def test_phase17_contract_never_mutates_v2_v3_manifests() -> None:
    """阶段②落地不允许改写 v2/v3 manifest（历史冻结 digest 承诺不可破坏）。"""
    v2 = json.loads((_PROJECT_ROOT / "evaluation/manifests/phase16-qualification-policy-v2.json").read_bytes())
    v3 = json.loads((_PROJECT_ROOT / "evaluation/manifests/phase16-qualification-policy-v3.json").read_bytes())
    assert v2["policy_digest"].startswith("1aa9ca6f")
    assert v3["policy_digest"].startswith("75319a4a")
    assert v3["policy_role"] == "RETROSPECTIVE_EVALUATION"
    assert v3["forward_contract_draft"]["implementation_status"] == "NOT_WIRED_INTO_RUNTIME"


def test_phase17_contract_source_digests_match_current_implementation() -> None:
    """契约记录的 source digests 必须与当前源码一致（闭包自洽，报告可引用）。"""
    payload = _load_contract()
    assert payload["source_file_digests"] == phase17_holdout_source_file_digests(
        repository_root=_PROJECT_ROOT
    )
