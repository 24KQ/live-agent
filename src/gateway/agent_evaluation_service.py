# -*- coding: utf-8 -*-
"""Phase 7A Agent Evaluation 服务与 Worker。"""

from __future__ import annotations

import hashlib
from typing import Any

from src.core.agent_evaluation import AgentRuleEvaluator
from src.core.agent_replay import AgentReplayService
from src.gateway.agent_evaluation_store import (
    EvaluationRunCreate,
    InMemoryAgentEvaluationStore,
)


class AgentEvaluationService:
    """API 使用的评估门面，负责幂等创建任务和读取结果。"""

    def __init__(self, *, store: Any | None = None, evaluator_version: str = "rules-v1") -> None:
        self._store = store or InMemoryAgentEvaluationStore()
        self._evaluator_version = evaluator_version

    @property
    def store(self) -> Any:
        return self._store

    def create_evaluation(self, *, trace_id: str, profile: str = "production_hybrid") -> dict[str, Any]:
        fingerprint = _input_fingerprint(trace_id=trace_id, evaluator_version=self._evaluator_version, profile=profile)
        record = self._store.create_or_reuse_run(
            EvaluationRunCreate(
                trace_id=trace_id,
                evaluator_version=self._evaluator_version,
                input_fingerprint=fingerprint,
                profile=profile,
            )
        )
        return _run_to_payload(record)

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return _run_to_payload(self._store.get(evaluation_id))

    def get_latest_replay(self, trace_id: str) -> dict[str, Any] | None:
        record = self._store.get_latest_by_trace_id(trace_id)
        if record is None:
            return None
        return {"evaluation_id": record.evaluation_id, "replay": record.replay_snapshot}

    def add_review(self, *, evaluation_id: str, operator_id: str, conclusion: str, reason: str) -> dict[str, Any]:
        review = self._store.add_review(
            evaluation_id=evaluation_id,
            operator_id=operator_id,
            conclusion=conclusion,
            reason=reason,
        )
        return {"review": review.__dict__}


class AgentEvaluationWorker:
    """同步 Worker，可由 CLI 或测试用 `run_once()` 驱动。"""

    def __init__(
        self,
        *,
        store: Any,
        replay_service: AgentReplayService,
        evaluator: AgentRuleEvaluator,
        llm_judge: Any | None = None,
        worker_id: str = "agent-evaluation-worker",
    ) -> None:
        self._store = store
        self._replay_service = replay_service
        self._evaluator = evaluator
        self._llm_judge = llm_judge
        self._worker_id = worker_id

    def run_once(self) -> bool:
        """抢占并处理一个 queued 任务；无任务时返回 False。"""

        run = self._store.claim_next_run(self._worker_id)
        if run is None:
            return False
        try:
            replay = self._replay_service.build_replay(run.trace_id)
            result = self._evaluator.evaluate(replay)
            dimension_scores = [score.model_dump(mode="json") for score in result.dimension_scores]
            dimension_scores = self._apply_llm_judge_if_available(replay, dimension_scores)
            self._store.complete_run(
                evaluation_id=run.evaluation_id,
                replay_snapshot=replay.model_dump(mode="json"),
                overall_score=result.overall_score,
                coverage_percent=result.coverage_percent,
                verdict=result.verdict,
                violations=result.violations,
                dimension_scores=dimension_scores,
                status="completed" if result.verdict == "PASS" else "partial",
            )
            return True
        except Exception as exc:  # noqa: BLE001 - Worker 必须把任务失败写回队列，不能吞掉。
            self._store.fail_run(run.evaluation_id, str(exc))
            return True

    def _apply_llm_judge_if_available(self, replay: Any, dimension_scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """用 LLM Judge 补充“建议语义质量”维度。

        Judge 的结果只替换语义质量维度，绝不改写安全、人审、工具合规分数。
        模型不可用时保留规则评估的 `score=None`，使任务仍可完成为 partial/complete。
        """

        if self._llm_judge is None:
            return dimension_scores
        suggestion, context_summary = _extract_judge_inputs(replay)
        if not suggestion:
            return dimension_scores
        judge_result = self._llm_judge.judge(
            final_suggestion=suggestion,
            context_summary=context_summary,
        )
        judge_payload = judge_result.model_dump(mode="json") if hasattr(judge_result, "model_dump") else dict(judge_result)
        updated: list[dict[str, Any]] = []
        for score in dimension_scores:
            if score.get("dimension") == "建议语义质量":
                updated.append(
                    {
                        **score,
                        "score": judge_payload.get("score"),
                        "evidence": [judge_payload],
                        "evaluator_type": "llm_judge",
                        "evaluator_version": "judge-v1",
                    }
                )
            else:
                updated.append(score)
        return updated


def _input_fingerprint(*, trace_id: str, evaluator_version: str, profile: str) -> str:
    raw = f"{trace_id}|{evaluator_version}|{profile}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _run_to_payload(record: Any) -> dict[str, Any]:
    return {
        "evaluation_id": record.evaluation_id,
        "trace_id": record.trace_id,
        "status": record.status,
        "profile": record.profile,
        "evaluator_version": record.evaluator_version,
        "input_fingerprint": record.input_fingerprint,
        "overall_score": record.overall_score,
        "coverage_percent": record.coverage_percent,
        "verdict": record.verdict,
        "violations": record.violations,
        "dimension_scores": record.dimension_scores,
        "replay_fidelity": record.replay_fidelity,
        "error": record.error,
        "retry_count": record.retry_count,
    }


def _extract_judge_inputs(replay: Any) -> tuple[str, str]:
    """从回放快照中提取 Judge 所需的最终建议和上下文摘要。"""

    for item in reversed(replay.timeline):
        state = item.state_delta or {}
        suggestion = state.get("final_suggestion")
        if suggestion:
            return str(suggestion), str(state.get("context_summary") or "")
    return "", ""
