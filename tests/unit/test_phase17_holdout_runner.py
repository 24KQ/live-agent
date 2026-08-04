"""Phase 17 runner 的 envelope/schema/prompt 一致性单元测试。"""

from __future__ import annotations

import json

import pytest

from src.decision_support.phase16_qualification_candidate import (
    build_phase17_holdout_profiles,
)
from src.decision_support.phase17_holdout_runner import Phase17HoldoutCampaignRunner


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
    assert Phase17HoldoutCampaignRunner._structure_valid(stage=stage, output=example)
    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage=stage, output=final_output
    )


def test_phase17_analyst_validator_respects_frozen_optional_empty_code_lists() -> None:
    """冻结 schema 没有给 constraint/risk codes 设置 minItems，校验器不能擅自加严。"""

    analyst = build_phase17_holdout_profiles()[0]
    example = _prompt_shape_example(analyst.prompt_text)
    assert example["final_output"]["constraint_codes"] == []
    assert example["final_output"]["risk_codes"] == []
    assert Phase17HoldoutCampaignRunner._structure_valid(
        stage="ANALYST", output=example
    )


def test_phase17_runner_rejects_legacy_v2_shapes_and_wrong_envelopes() -> None:
    """旧 v2 手写字段和缺少 FINAL envelope 的对象都必须继续 fail-closed。"""

    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage="ANALYST",
        output={"trigger_codes": ["PRICE_CONFLICT"], "analysis": {"severity": "HIGH"}},
    )
    assert not Phase17HoldoutCampaignRunner._structure_valid(
        stage="PLANNER",
        output={"risk_codes": ["SIDE_EFFECT_UNKNOWN"], "proposal": {"action": "HOLD"}},
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
