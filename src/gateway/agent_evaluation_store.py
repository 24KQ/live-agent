# -*- coding: utf-8 -*-
"""Phase 7A Agent Evaluation 持久化接口。

这里先提供内存 Store 供单元测试、API 注入和本地 demo 使用；PostgreSQL Store
会复用同一套模型，后续通过 SQL 表承载生产任务队列。Store 的核心语义是：
幂等创建、租约抢占、最多三次重试、终态不可覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import Settings


EvaluationStatus = Literal["queued", "running", "completed", "partial", "failed"]


@dataclass(frozen=True)
class EvaluationRunCreate:
    trace_id: str
    evaluator_version: str
    input_fingerprint: str
    profile: str = "production_hybrid"


@dataclass(frozen=True)
class EvaluationRunRecord:
    evaluation_id: str
    trace_id: str
    evaluator_version: str
    input_fingerprint: str
    profile: str
    status: EvaluationStatus = "queued"
    replay_snapshot: dict[str, Any] = field(default_factory=dict)
    overall_score: float | None = None
    coverage_percent: float | None = None
    verdict: str | None = None
    violations: list[str] = field(default_factory=list)
    dimension_scores: list[dict[str, Any]] = field(default_factory=list)
    retry_count: int = 0
    error: str | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def replay_fidelity(self) -> str | None:
        return self.replay_snapshot.get("replay_fidelity") if self.replay_snapshot else None


@dataclass(frozen=True)
class EvaluationReviewRecord:
    review_id: str
    evaluation_id: str
    operator_id: str
    conclusion: str
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EvaluationRunNotFoundError(KeyError):
    """按 evaluation_id 找不到评估任务。"""


class InMemoryAgentEvaluationStore:
    """内存评估任务 Store，模拟生产队列的关键语义。"""

    def __init__(self) -> None:
        self._runs: dict[str, EvaluationRunRecord] = {}
        self._idempotency: dict[tuple[str, str, str, str], str] = {}
        self._reviews: dict[str, list[EvaluationReviewRecord]] = {}

    def create_or_reuse_run(self, request: EvaluationRunCreate) -> EvaluationRunRecord:
        key = (request.trace_id, request.evaluator_version, request.input_fingerprint, request.profile)
        existing_id = self._idempotency.get(key)
        if existing_id:
            return self._runs[existing_id]
        evaluation_id = f"eval-{uuid4()}"
        record = EvaluationRunRecord(evaluation_id=evaluation_id, **request.__dict__)
        self._runs[evaluation_id] = record
        self._idempotency[key] = evaluation_id
        return record

    def get(self, evaluation_id: str) -> EvaluationRunRecord:
        try:
            return self._runs[evaluation_id]
        except KeyError as exc:
            raise EvaluationRunNotFoundError(evaluation_id) from exc

    def get_latest_by_trace_id(self, trace_id: str) -> EvaluationRunRecord | None:
        records = [record for record in self._runs.values() if record.trace_id == trace_id and record.replay_snapshot]
        if not records:
            return None
        return sorted(records, key=lambda item: item.updated_at, reverse=True)[0]

    def claim_next_run(self, worker_id: str, lease_seconds: int = 60) -> EvaluationRunRecord | None:
        now = datetime.now(timezone.utc)
        candidates = sorted(self._runs.values(), key=lambda item: item.created_at)
        for record in candidates:
            if record.status == "queued" or (record.status == "running" and record.lease_until and record.lease_until <= now):
                claimed = replace(
                    record,
                    status="running",
                    lease_owner=worker_id,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                )
                self._runs[record.evaluation_id] = claimed
                return claimed
        return None

    def complete_run(
        self,
        *,
        evaluation_id: str,
        replay_snapshot: dict[str, Any],
        overall_score: float,
        coverage_percent: float,
        verdict: str,
        violations: list[str],
        dimension_scores: list[dict[str, Any]],
        status: EvaluationStatus | None = None,
    ) -> EvaluationRunRecord:
        current = self.get(evaluation_id)
        if current.status in {"completed", "partial", "failed"}:
            return current
        final_status: EvaluationStatus = status or ("completed" if verdict != "WARN" else "partial")
        saved = replace(
            current,
            status=final_status,
            replay_snapshot=replay_snapshot,
            overall_score=overall_score,
            coverage_percent=coverage_percent,
            verdict=verdict,
            violations=violations,
            dimension_scores=dimension_scores,
            lease_owner=None,
            lease_until=None,
            updated_at=datetime.now(timezone.utc),
        )
        self._runs[evaluation_id] = saved
        return saved

    def fail_run(self, evaluation_id: str, error: str) -> EvaluationRunRecord:
        current = self.get(evaluation_id)
        if current.status in {"completed", "partial", "failed"}:
            return current
        retry_count = current.retry_count + 1
        status: EvaluationStatus = "failed" if retry_count >= 3 else "queued"
        saved = replace(
            current,
            status=status,
            retry_count=retry_count,
            error=error,
            lease_owner=None,
            lease_until=None,
            updated_at=datetime.now(timezone.utc),
        )
        self._runs[evaluation_id] = saved
        return saved

    def add_review(self, *, evaluation_id: str, operator_id: str, conclusion: str, reason: str) -> EvaluationReviewRecord:
        self.get(evaluation_id)
        review = EvaluationReviewRecord(
            review_id=f"review-{uuid4()}",
            evaluation_id=evaluation_id,
            operator_id=operator_id,
            conclusion=conclusion,
            reason=reason,
        )
        self._reviews.setdefault(evaluation_id, []).append(review)
        return review

    def list_reviews(self, evaluation_id: str) -> list[EvaluationReviewRecord]:
        self.get(evaluation_id)
        return list(self._reviews.get(evaluation_id, []))


class PostgresAgentEvaluationStore:
    """PostgreSQL 评估任务 Store。

    该实现把 PostgreSQL 同时作为事实源和轻量任务队列。Worker 通过
    `FOR UPDATE SKIP LOCKED` 抢占 queued 任务，避免多个 Worker 重复处理同一
    evaluation_id。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def initialize_schema(self) -> None:
        initialize_agent_evaluation_schema(self._settings)

    def create_or_reuse_run(self, request: EvaluationRunCreate) -> EvaluationRunRecord:
        evaluation_id = f"eval-{uuid4()}"
        sql = """
            INSERT INTO live_agent_evaluation_runs (
                evaluation_id, trace_id, evaluator_version, input_fingerprint, profile, status
            )
            VALUES (
                %(evaluation_id)s, %(trace_id)s, %(evaluator_version)s,
                %(input_fingerprint)s, %(profile)s, 'queued'
            )
            ON CONFLICT (trace_id, evaluator_version, input_fingerprint, profile)
            DO UPDATE SET updated_at = live_agent_evaluation_runs.updated_at
            RETURNING *;
        """
        return self._fetch_one(
            sql,
            {
                "evaluation_id": evaluation_id,
                "trace_id": request.trace_id,
                "evaluator_version": request.evaluator_version,
                "input_fingerprint": request.input_fingerprint,
                "profile": request.profile,
            },
        )

    def get(self, evaluation_id: str) -> EvaluationRunRecord:
        sql = "SELECT * FROM live_agent_evaluation_runs WHERE evaluation_id = %(evaluation_id)s;"
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"evaluation_id": evaluation_id})
                row = cur.fetchone()
        if row is None:
            raise EvaluationRunNotFoundError(evaluation_id)
        return self._row_to_run(dict(row))

    def get_latest_by_trace_id(self, trace_id: str) -> EvaluationRunRecord | None:
        sql = """
            SELECT * FROM live_agent_evaluation_runs
            WHERE trace_id = %(trace_id)s AND replay_snapshot <> '{}'::jsonb
            ORDER BY updated_at DESC
            LIMIT 1;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"trace_id": trace_id})
                row = cur.fetchone()
        return self._row_to_run(dict(row)) if row is not None else None

    def claim_next_run(self, worker_id: str, lease_seconds: int = 60) -> EvaluationRunRecord | None:
        sql = """
            WITH candidate AS (
                SELECT evaluation_id
                FROM live_agent_evaluation_runs
                WHERE status = 'queued'
                   OR (status = 'running' AND lease_until <= NOW())
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE live_agent_evaluation_runs run
            SET status = 'running',
                lease_owner = %(worker_id)s,
                lease_until = NOW() + (%(lease_seconds)s || ' seconds')::interval,
                updated_at = NOW()
            FROM candidate
            WHERE run.evaluation_id = candidate.evaluation_id
            RETURNING run.*;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"worker_id": worker_id, "lease_seconds": lease_seconds})
                row = cur.fetchone()
            conn.commit()
        return self._row_to_run(dict(row)) if row is not None else None

    def complete_run(
        self,
        *,
        evaluation_id: str,
        replay_snapshot: dict[str, Any],
        overall_score: float,
        coverage_percent: float,
        verdict: str,
        violations: list[str],
        dimension_scores: list[dict[str, Any]],
        status: EvaluationStatus | None = None,
    ) -> EvaluationRunRecord:
        current = self.get(evaluation_id)
        if current.status in {"completed", "partial", "failed"}:
            return current
        final_status = status or ("completed" if verdict == "PASS" else "partial")
        # run 汇总和维度明细必须在同一事务内提交，避免页面看到 completed，
        # 但明细表仍为空或停留在旧版本。
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE live_agent_evaluation_runs
                    SET status = %(status)s,
                        replay_snapshot = %(replay_snapshot)s,
                        overall_score = %(overall_score)s,
                        coverage_percent = %(coverage_percent)s,
                        verdict = %(verdict)s,
                        violations = %(violations)s,
                        dimension_scores = %(dimension_scores)s,
                        lease_owner = NULL,
                        lease_until = NULL,
                        updated_at = NOW()
                    WHERE evaluation_id = %(evaluation_id)s
                    RETURNING *;
                    """,
                    {
                        "evaluation_id": evaluation_id,
                        "status": final_status,
                        "replay_snapshot": Jsonb(replay_snapshot),
                        "overall_score": overall_score,
                        "coverage_percent": coverage_percent,
                        "verdict": verdict,
                        "violations": Jsonb(violations),
                        "dimension_scores": Jsonb(dimension_scores),
                    },
                )
                row = cur.fetchone()
                if row is None:
                    raise EvaluationRunNotFoundError(evaluation_id)
                self._replace_dimension_scores_in_cursor(cur, evaluation_id, dimension_scores)
            conn.commit()
        return self._row_to_run(dict(row))

    def fail_run(self, evaluation_id: str, error: str) -> EvaluationRunRecord:
        current = self.get(evaluation_id)
        if current.status in {"completed", "partial", "failed"}:
            return current
        retry_count = current.retry_count + 1
        status = "failed" if retry_count >= 3 else "queued"
        sql = """
            UPDATE live_agent_evaluation_runs
            SET status = %(status)s,
                retry_count = %(retry_count)s,
                error = %(error)s,
                lease_owner = NULL,
                lease_until = NULL,
                updated_at = NOW()
            WHERE evaluation_id = %(evaluation_id)s
            RETURNING *;
        """
        return self._fetch_one(
            sql,
            {
                "evaluation_id": evaluation_id,
                "status": status,
                "retry_count": retry_count,
                "error": error,
            },
        )

    def add_review(self, *, evaluation_id: str, operator_id: str, conclusion: str, reason: str) -> EvaluationReviewRecord:
        self.get(evaluation_id)
        review_id = f"review-{uuid4()}"
        sql = """
            INSERT INTO live_agent_evaluation_reviews (
                review_id, evaluation_id, operator_id, conclusion, reason
            )
            VALUES (
                %(review_id)s, %(evaluation_id)s, %(operator_id)s, %(conclusion)s, %(reason)s
            )
            RETURNING *;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    {
                        "review_id": review_id,
                        "evaluation_id": evaluation_id,
                        "operator_id": operator_id,
                        "conclusion": conclusion,
                        "reason": reason,
                    },
                )
                row = cur.fetchone()
            conn.commit()
        return self._row_to_review(dict(row))

    def list_reviews(self, evaluation_id: str) -> list[EvaluationReviewRecord]:
        self.get(evaluation_id)
        sql = """
            SELECT * FROM live_agent_evaluation_reviews
            WHERE evaluation_id = %(evaluation_id)s
            ORDER BY created_at ASC;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"evaluation_id": evaluation_id})
                rows = cur.fetchall()
        return [self._row_to_review(dict(row)) for row in rows]

    def _replace_dimension_scores(self, evaluation_id: str, scores: list[dict[str, Any]]) -> None:
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                self._replace_dimension_scores_in_cursor(cur, evaluation_id, scores)
            conn.commit()

    @staticmethod
    def _replace_dimension_scores_in_cursor(cur: Any, evaluation_id: str, scores: list[dict[str, Any]]) -> None:
        """在调用方事务内替换维度明细。"""

        cur.execute(
            "DELETE FROM live_agent_evaluation_dimension_scores WHERE evaluation_id = %(evaluation_id)s;",
            {"evaluation_id": evaluation_id},
        )
        for score in scores:
            cur.execute(
                """
                INSERT INTO live_agent_evaluation_dimension_scores (
                    evaluation_id, dimension, score, weight, evidence,
                    evaluator_type, evaluator_version
                )
                VALUES (
                    %(evaluation_id)s, %(dimension)s, %(score)s, %(weight)s,
                    %(evidence)s, %(evaluator_type)s, %(evaluator_version)s
                );
                """,
                {
                    "evaluation_id": evaluation_id,
                    "dimension": score.get("dimension"),
                    "score": score.get("score"),
                    "weight": score.get("weight"),
                    "evidence": Jsonb(score.get("evidence") or []),
                    "evaluator_type": score.get("evaluator_type") or "rule",
                    "evaluator_version": score.get("evaluator_version") or "rules-v1",
                },
            )

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> EvaluationRunRecord:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise EvaluationRunNotFoundError(params.get("evaluation_id", ""))
        return self._row_to_run(dict(row))

    @staticmethod
    def _row_to_run(row: dict[str, Any]) -> EvaluationRunRecord:
        return EvaluationRunRecord(
            evaluation_id=row["evaluation_id"],
            trace_id=row["trace_id"],
            evaluator_version=row["evaluator_version"],
            input_fingerprint=row["input_fingerprint"],
            profile=row["profile"],
            status=row["status"],
            replay_snapshot=dict(row.get("replay_snapshot") or {}),
            overall_score=float(row["overall_score"]) if row.get("overall_score") is not None else None,
            coverage_percent=float(row["coverage_percent"]) if row.get("coverage_percent") is not None else None,
            verdict=row.get("verdict"),
            violations=list(row.get("violations") or []),
            dimension_scores=list(row.get("dimension_scores") or []),
            retry_count=int(row.get("retry_count") or 0),
            error=row.get("error"),
            lease_owner=row.get("lease_owner"),
            lease_until=row.get("lease_until"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_review(row: dict[str, Any]) -> EvaluationReviewRecord:
        return EvaluationReviewRecord(
            review_id=row["review_id"],
            evaluation_id=row["evaluation_id"],
            operator_id=row["operator_id"],
            conclusion=row["conclusion"],
            reason=row["reason"],
            created_at=row["created_at"],
        )


def initialize_agent_evaluation_schema(settings: Settings) -> None:
    """初始化 Phase 7A Agent Evaluation 表结构。"""

    project_root = Path(__file__).resolve().parents[2]
    sql = (project_root / "docker" / "init_phase7a_agent_evaluations.sql").read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
