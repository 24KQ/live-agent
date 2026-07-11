from __future__ import annotations

import json

import pytest

from src.skills.agent_llm_judge import AgentLLMJudge, AgentLLMJudgeResult, build_judge_prompt


def test_build_judge_prompt_contains_rubric_and_suggestion() -> None:
    prompt = build_judge_prompt(
        final_suggestion="请主播强调优惠价",
        context_summary="弹幕集中询问价格",
    )

    assert "相关性" in prompt
    assert "请主播强调优惠价" in prompt
    assert "弹幕集中询问价格" in prompt


def test_llm_judge_parses_valid_json_response() -> None:
    def fake_http(_payload: dict) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "relevance": 0.9,
                                "actionability": 0.8,
                                "context_consistency": 0.85,
                                "reason": "建议贴合上下文",
                                "confidence": 0.7,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 123},
        }

    result = AgentLLMJudge(http_post=fake_http).judge(
        final_suggestion="请主播强调优惠价",
        context_summary="弹幕集中询问价格",
    )

    assert isinstance(result, AgentLLMJudgeResult)
    assert result.score == pytest.approx(85.0)
    assert result.token_usage == {"total_tokens": 123}


def test_llm_judge_returns_partial_on_invalid_json() -> None:
    def fake_http(_payload: dict) -> dict:
        return {"choices": [{"message": {"content": "not json"}}]}

    result = AgentLLMJudge(http_post=fake_http).judge(
        final_suggestion="请主播强调优惠价",
        context_summary="弹幕集中询问价格",
    )

    assert result.status == "partial"
    assert result.score is None
    assert "invalid" in result.error.lower()
