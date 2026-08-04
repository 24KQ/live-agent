"""Phase 17 capture 与安全硬门禁的离线单元测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

import pytest

from src.decision_support.phase17_holdout_capture import (
    Phase17ArtifactCapture,
    Phase17ArtifactCaptureError,
)
from src.decision_support.phase17_holdout_runner import evaluate_phase17_safety_gate
from src.specialist_runtime.deepseek_adapter import AsyncHttpResponse
from src.specialist_runtime.model_port import ModelMessage, ModelRequest
from src.specialist_runtime.phase17_v5_adapter import Phase17V5ControlledE2EAdapter


class _ResponseTransport:
    """只返回固定 HTTP 响应的离线 transport，不产生真实网络流量。"""

    def __init__(self, response: AsyncHttpResponse | None = None) -> None:
        self.response = response
        # 记录 transport 实际收到的请求 payload，验证 reasoning_effort 并非只存在于
        # Phase 17 manifest，而是确实经过 V5 装饰器进入将要发送的 HTTP 请求。
        self.payloads: list[dict[str, object]] = []

    async def post_json(self, **kwargs: object) -> AsyncHttpResponse:
        self.payloads.append(dict(kwargs["payload"]))
        if self.response is None:
            raise RuntimeError("synthetic transport failure")
        return self.response


def _request() -> ModelRequest:
    """构造符合 Phase 17 identity 的最小请求。"""

    return ModelRequest(
        request_id="request-001",
        endpoint_host="synapse-ai.uk",
        model_id="gpt-5.6-terra",
        temperature=Decimal("0"),
        prompt_hash="a" * 64,
        result_schema_hash="b" * 64,
        messages=(
            ModelMessage(role="system", content="phase17 system"),
            ModelMessage(role="user", content="phase17 user"),
        ),
        max_output_tokens=2800,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=90),
    )


def test_phase17_capture_writes_and_rechecks_response_body(tmp_path: Path) -> None:
    """capture 路径、正文和 response digest 必须三者一致。"""

    capture = Phase17ArtifactCapture(repository_root=tmp_path)
    body = b'{"choices":[{"message":{"content":"ok"}}]}'
    with capture.bind_stage(
        run_id="run-001",
        case_id="case-001",
        stage="ANALYST",
    ):
        with capture.begin_attempt(attempt_index=1) as attempt:
            capture.capture_response(body)
            capture.require_captured(attempt)

    assert attempt.artifact_path == "run-001/case-001/ANALYST/attempt-1.body"
    assert attempt.artifact_digest == sha256(body).hexdigest()
    assert (tmp_path / "_probe_artifacts" / attempt.artifact_path).read_bytes() == body


def test_phase17_capture_missing_body_is_blocking(tmp_path: Path) -> None:
    """没有 HTTP response body 时不能把 attempt 当作已审计。"""

    capture = Phase17ArtifactCapture(repository_root=tmp_path)
    with capture.bind_stage(run_id="run-001", case_id="case-001", stage="ANALYST"):
        with capture.begin_attempt(attempt_index=1) as attempt:
            with pytest.raises(Phase17ArtifactCaptureError, match="unavailable"):
                capture.require_captured(attempt)


def test_phase17_adapter_captures_raw_response_before_return(tmp_path: Path, monkeypatch) -> None:
    """真实 adapter seam 必须把 HTTP body 摘要与 artifact 一起返回。"""

    # Phase 17 契约要求 reasoning_effort=high；adapter 构造前显式设置它，模拟
    # 真实 CLI 预检后的进程环境，之后直接断言 transport 看到的最终 payload。
    monkeypatch.setenv("LLM_API_REASONING_EFFORT", "high")
    monkeypatch.delenv("LLM_API_MODEL_ID", raising=False)
    body = json.dumps(
        {
            "id": "response-001",
            "model": "gpt-5.6-terra",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"trigger_codes": ["CONFLICT"], "analysis": {"severity": "HIGH"}}
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode("utf-8")
    capture = Phase17ArtifactCapture(repository_root=tmp_path)
    transport = _ResponseTransport(
        AsyncHttpResponse(status_code=200, headers={}, body=body)
    )
    adapter = Phase17V5ControlledE2EAdapter(
        endpoints=(("synapse-ai.uk", "synthetic-key"),),
        reasoning_effort="high",
        transport=transport,
        capture=capture,
    )
    with capture.bind_stage(run_id="run-001", case_id="case-001", stage="ANALYST"):
        result = asyncio.run(adapter.complete(_request()))

    assert result.capture_failed is False
    assert len(result.attempt_details) == 1
    detail = result.attempt_details[0]
    assert detail.artifact_capture_status == "CAPTURED"
    assert detail.artifact_digest == sha256(body).hexdigest()
    assert detail.response_digest == detail.artifact_digest
    assert transport.payloads[0]["model"] == "gpt-5.6-terra"
    assert transport.payloads[0]["reasoning_effort"] == "high"


def test_phase17_adapter_stops_after_capture_failure(tmp_path: Path, monkeypatch) -> None:
    """没有 body 的 transport 失败不能继续 retry/failover。"""

    monkeypatch.setenv("LLM_API_REASONING_EFFORT", "high")
    monkeypatch.delenv("LLM_API_MODEL_ID", raising=False)
    capture = Phase17ArtifactCapture(repository_root=tmp_path)
    adapter = Phase17V5ControlledE2EAdapter(
        endpoints=(("synapse-ai.uk", "synthetic-key"),),
        reasoning_effort="high",
        transport=_ResponseTransport(),
        capture=capture,
    )
    with capture.bind_stage(run_id="run-002", case_id="case-002", stage="ANALYST"):
        result = asyncio.run(adapter.complete(_request()))

    assert result.capture_failed is True
    assert len(result.attempt_details) == 1
    assert result.attempt_details[0].artifact_capture_status == "FAILED"


def test_phase17_safety_gate_requires_six_reviews_and_matching_artifacts() -> None:
    """六个 hard-safety case 必须逐例有 capture 与 Claude PASS。"""

    case_ids = tuple(f"case-{index}" for index in range(1, 7))
    digest_rows = {
        case_id: f"{index:064x}" for index, case_id in enumerate(case_ids, start=1)
    }
    attempts = tuple(
        {
            "case_id": case_id,
            "artifact_capture_status": "CAPTURED",
            "response_digest": digest_rows[case_id],
            "artifact_digest": digest_rows[case_id],
            "artifact_path": f"run/{case_id}/ANALYST/attempt-1.body",
        }
        for case_id in case_ids
    )
    reviews = tuple(
        {
            "case_id": case_id,
            "artifact_digest": digest_rows[case_id],
            "verdict": "PASS",
            "reviewer": "claude-independent-review",
        }
        for case_id in case_ids
    )
    result = evaluate_phase17_safety_gate(
        hard_safety_case_ids=case_ids,
        attempt_rows=attempts,
        review_rows=reviews,
        artifact_digest_lookup=lambda path: next(
            digest
            for case_id, digest in digest_rows.items()
            if case_id in path
        ),
    )
    assert result.status == "PASS"
    assert result.reviewed_case_ids == tuple(sorted(case_ids))


def test_phase17_safety_gate_distinguishes_fail_and_inconclusive() -> None:
    """Claude FAIL 是 FAILED；INCONCLUSIVE 是 BLOCKED，不能混为通过。"""

    case_ids = tuple(f"case-{index}" for index in range(1, 7))
    attempts = tuple(
        {
            "case_id": case_id,
            "artifact_capture_status": "CAPTURED",
            "response_digest": "a" * 64,
            "artifact_digest": "a" * 64,
            "artifact_path": f"{case_id}/attempt-1.body",
        }
        for case_id in case_ids
    )
    base_reviews = [
        {
            "case_id": case_id,
            "artifact_digest": "a" * 64,
            "verdict": "PASS",
            "reviewer": "claude-independent-review",
        }
        for case_id in case_ids
    ]
    failed_reviews = tuple([{**base_reviews[0], "verdict": "FAIL"}, *base_reviews[1:]])
    failed = evaluate_phase17_safety_gate(
        hard_safety_case_ids=case_ids,
        attempt_rows=attempts,
        review_rows=failed_reviews,
        artifact_digest_lookup=lambda _: "a" * 64,
    )
    assert failed.status == "FAILED"

    inconclusive_reviews = tuple(
        [{**base_reviews[0], "verdict": "INCONCLUSIVE"}, *base_reviews[1:]]
    )
    inconclusive = evaluate_phase17_safety_gate(
        hard_safety_case_ids=case_ids,
        attempt_rows=attempts,
        review_rows=inconclusive_reviews,
        artifact_digest_lookup=lambda _: "a" * 64,
    )
    assert inconclusive.status == "BLOCKED"
