"""Phase 17 runner 的 envelope/schema/prompt 一致性单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.decision_support.phase16_qualification_candidate import (
    build_phase17_holdout_profiles,
)
from src.decision_support.phase17_holdout_runner import Phase17HoldoutCampaignRunner


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _case_input(*evidence_ids: str) -> str:
    """构造只含公开合成证据 ID 的原始 case 输入，供结构门单元测试使用。"""

    return "合成 case 输入：" + "、".join(evidence_ids)


def _analyst_output(evidence_id: str) -> dict[str, object]:
    """构造与冻结 Analyst schema 对齐、且引用指定证据的 FINAL envelope。"""

    return {
        "kind": "FINAL",
        "final_output": {
            "constraint_codes": [],
            "risk_codes": [],
            "explanation": "基于输入证据的受限分析。",
            "evidence_ids": [evidence_id],
        },
    }


def _planner_output(evidence_id: str) -> dict[str, object]:
    """构造与冻结 Planner schema 的 evidence_ids 绑定最小 FINAL envelope。"""

    return {
        "kind": "FINAL",
        "final_output": {"options": [{"evidence_ids": [evidence_id]}]},
    }


def _prompt_shape_example(prompt_text: str) -> dict[str, object]:
    """从冻结 prompt 中读取人工写入的 FINAL 形状示例，不复制第二份协议。"""

    marker = '{"kind":"FINAL","final_output":'
    start = prompt_text.find(marker)
    assert start >= 0, "Phase 17 prompt must contain a FINAL envelope shape example"
    value, _end = json.JSONDecoder().raw_decode(prompt_text[start:])
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("stage_index,stage", ((0, "ANALYST"), (1, "PLANNER")))
def test_phase17_prompt_schema_and_validator_share_the_same_envelope(
    stage_index: int, stage: str
) -> None:
    """冻结 prompt 示例、result schema 与 runner 校验必须闭合到同一层结构。"""

    profile = build_phase17_holdout_profiles()[stage_index]
    example = _prompt_shape_example(profile.prompt_text)
    final_output = example["final_output"]

    assert example["kind"] == "FINAL"
    assert isinstance(final_output, dict)
    assert set(final_output) == set(profile.result_schema["required"])
    # prompt 里的形状示例只能说明字段层级；元占位符不能伪装成输入证据，
    # 所以绑定到任意真实输入时应由新的硬校验拒绝。
    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage=stage,
        output=example,
        case_input=_case_input("SYN-P-4001"),
    )
    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage=stage,
        output=final_output,
        case_input=_case_input("SYN-P-4001"),
    )


def test_phase17_analyst_validator_respects_frozen_optional_empty_code_lists() -> None:
    """冻结 schema 没有给 constraint/risk codes 设置 minItems，校验器不能擅自加严。"""

    analyst = build_phase17_holdout_profiles()[0]
    example = _prompt_shape_example(analyst.prompt_text)
    assert example["final_output"]["constraint_codes"] == []
    assert example["final_output"]["risk_codes"] == []
    example["final_output"]["evidence_ids"] = ["SYN-P-4001"]
    assert Phase17HoldoutCampaignRunner._structure_valid(
        stage="ANALYST",
        output=example,
        case_input=_case_input("SYN-P-4001"),
    )


def test_phase17_runner_rejects_legacy_v2_shapes_and_wrong_envelopes() -> None:
    """旧 v2 手写字段和缺少 FINAL envelope 的对象都必须继续 fail-closed。"""

    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage="ANALYST",
        output={"trigger_codes": ["PRICE_CONFLICT"], "analysis": {"severity": "HIGH"}},
        case_input=_case_input("SYN-P-4001"),
    )
    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage="PLANNER",
        output={"risk_codes": ["SIDE_EFFECT_UNKNOWN"], "proposal": {"action": "HOLD"}},
        case_input=_case_input("SYN-P-4001"),
    )
    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage="ANALYST",
        output={
            "kind": "CALL_SKILL",
            "final_output": {
                "constraint_codes": [],
                "risk_codes": [],
                "explanation": "not a final action",
                "evidence_ids": ["evidence-001"],
            },
        },
        case_input=_case_input("SYN-P-4001"),
    )


def test_phase17_runner_rejects_prompt_meta_placeholder_as_evidence_id() -> None:
    """形状示例的元占位符不能被模型原样抄回并当作真实证据。"""

    output = _analyst_output("<evidence-id-from-input>")
    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage="ANALYST",
        output=output,
        case_input=_case_input("SYN-P-4101"),
    )


def test_phase17_runner_rejects_evidence_id_not_visible_in_case_input() -> None:
    """格式合法但不在原始输入中的 ID 也必须 fail-closed，覆盖两个阶段。"""

    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage="ANALYST",
        output=_analyst_output("SYN-P-4999"),
        case_input=_case_input("SYN-P-4101"),
    )
    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage="PLANNER",
        output=_planner_output("SYN-P-4999"),
        case_input=_case_input("SYN-P-4101"),
    )


def test_phase17_runner_accepts_postlive_evidence_id_after_input_repair() -> None:
    """postlive 修复后的可见证据 ID 可以通过 Analyst/Planner 两阶段绑定。"""

    case_input = (
        _PROJECT_ROOT / "evaluation/phase17_holdout/inputs/phase17-holdout-postlive-001.txt"
    ).read_text(encoding="utf-8")
    assert "SYN-P-4601" in case_input
    assert Phase17HoldoutCampaignRunner._structure_valid(
        stage="ANALYST",
        output=_analyst_output("SYN-P-4601"),
        case_input=case_input,
    )
    assert Phase17HoldoutCampaignRunner._structure_valid(
        stage="PLANNER",
        output=_planner_output("SYN-P-4601"),
        case_input=case_input,
    )


@pytest.mark.parametrize(
    ("case_filename", "evidence_id"),
    (
        ("phase17-holdout-danmu-001.txt", "SYN-P-4101"),
        ("phase17-holdout-soldout-001.txt", "SYN-P-4302"),
        ("phase17-holdout-price-001.txt", "SYN-P-4404"),
    ),
)
def test_phase17_hard_safety_cases_keep_real_evidence_binding(
    case_filename: str, evidence_id: str
) -> None:
    """原三例 hard-safety 的真实输入证据绑定必须保持可用，避免修复误伤安全回归。"""

    case_input = (_PROJECT_ROOT / "evaluation/phase17_holdout/inputs" / case_filename).read_text(
        encoding="utf-8"
    )
    assert evidence_id in case_input
    assert Phase17HoldoutCampaignRunner._structure_valid(
        stage="ANALYST",
        output=_analyst_output(evidence_id),
        case_input=case_input,
    )
    assert Phase17HoldoutCampaignRunner._structure_valid(
        stage="PLANNER",
        output=_planner_output(evidence_id),
        case_input=case_input,
    )


def test_phase17_planner_projection_unwraps_analyst_final_output() -> None:
    """Planner 应看到内层 analysis 字段，而不是重复嵌套的 FINAL envelope。"""

    analyst = {
        "kind": "FINAL",
        "final_output": {"risk_codes": ["RECONCILIATION_REQUIRED"]},
    }
    assert Phase17HoldoutCampaignRunner._final_output_mapping(analyst) == {
        "risk_codes": ["RECONCILIATION_REQUIRED"]
    }
    assert Phase17HoldoutCampaignRunner._final_output_mapping(
        analyst["final_output"]
    ) is None
