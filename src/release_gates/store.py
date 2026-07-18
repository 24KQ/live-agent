"""Phase 15 ReleaseRun 与双轨结论 Store。

内存实现用于本地演练和单元测试，PostgreSQL 实现使用唯一键、行锁和 append-only
JSON 快照保存同一组事实。两种实现共享同一套 Pydantic 模型和状态聚合，避免
测试替身放宽正式 Store 的幂等、缺 case 或 digest 约束。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from psycopg.rows import dict_row

from src.release_gates.decisions import (
    DecisionSupportPromotionDecision,
    FinalReleaseDecision,
    FinalReleaseStatus,
    ReleaseCaseResult,
    ReleaseRun,
    ReleaseRunStatus,
    TechnicalReleaseDecision,
    TechnicalReleaseStatus,
)
from src.release_gates.models import EvaluationCaseStatus
from src.specialist_runtime.models import canonical_json_sha256


class ReleaseInvariantError(RuntimeError):
    """Release 事实冲突、缺失或非法状态转换。"""


def _technical_for(run: ReleaseRun, results: tuple[ReleaseCaseResult, ...]) -> TechnicalReleaseDecision:
    """从完整或不完整的 case 快照重算技术门禁，禁止调用方传入计数。"""

    expected = set(run.expected_case_ids)
    actual = {result.case_id for result in results}
    missing = expected - actual
    passed = sum(result.status is EvaluationCaseStatus.PASS for result in results)
    failed = sum(result.status is EvaluationCaseStatus.FAIL for result in results)
    blocked = sum(result.status is EvaluationCaseStatus.BLOCKED for result in results)
    severe = sum(result.severe_violation for result in results)
    reasons: list[str] = []
    if missing:
        reasons.append("MISSING_CASES")
    if blocked:
        reasons.append("CASE_BLOCKED")
    if failed:
        reasons.append("CASE_FAILED")
    if severe:
        reasons.append("SEVERE_VIOLATION")
    status = (
        TechnicalReleaseStatus.BLOCKED
        if missing or blocked
        else TechnicalReleaseStatus.FAIL
        if failed or severe
        else TechnicalReleaseStatus.PASS
    )
    digest = canonical_json_sha256(
        [result.model_dump(mode="json") for result in sorted(results, key=lambda item: item.case_id)]
    )
    return TechnicalReleaseDecision(
        release_run_id=run.release_run_id,
        status=status,
        expected_case_count=len(expected),
        completed_case_count=len(actual),
        passed_case_count=passed,
        failed_case_count=failed,
        blocked_case_count=blocked,
        severe_violation_count=severe,
        case_results_digest=digest,
        reason_codes=tuple(reasons),
    )


def _final_for(
    run_id: str,
    technical: TechnicalReleaseDecision,
    promotion: DecisionSupportPromotionDecision,
) -> FinalReleaseDecision:
    """按技术发布优先、晋升状态其次的固定映射生成最终状态。"""

    status = (
        FinalReleaseStatus.NOT_RELEASED
        if technical.status is not TechnicalReleaseStatus.PASS
        else FinalReleaseStatus.RELEASED_DECISION_SUPPORT_ENABLED
        if promotion.status.value == "PROMOTE"
        else FinalReleaseStatus.RELEASED_DECISION_SUPPORT_DISABLED
    )
    return FinalReleaseDecision(
        release_run_id=run_id,
        technical_status=technical.status,
        promotion_status=promotion.status,
        status=status,
        reason_codes=tuple(technical.reason_codes) + tuple(promotion.reason_codes),
    )


class InMemoryReleaseStore:
    """锁内维护唯一 ReleaseRun/CaseResult/双轨结论索引。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._runs: dict[str, ReleaseRun] = {}
        self._results: dict[tuple[str, str], ReleaseCaseResult] = {}
        self._technical: dict[str, TechnicalReleaseDecision] = {}
        self._promotions: dict[str, DecisionSupportPromotionDecision] = {}
        self._final: dict[str, FinalReleaseDecision] = {}

    def create_run(self, run: ReleaseRun) -> ReleaseRun:
        """首次创建或重放同一 ReleaseRun；身份冲突直接拒绝。"""

        with self._lock:
            existing = self._runs.get(run.release_run_id)
            if existing is not None:
                if existing != run:
                    raise ReleaseInvariantError("release run identity conflict")
                return existing
            if run.status is not ReleaseRunStatus.RUNNING:
                raise ReleaseInvariantError("new release run must start RUNNING")
            self._runs[run.release_run_id] = run
            return run

    def append_case_result(self, result: ReleaseCaseResult) -> ReleaseCaseResult:
        """追加唯一 case 结果；相同事实重放成功，冲突不可覆盖。"""

        with self._lock:
            run = self._required_run(result.release_run_id)
            if result.manifest_digest != run.manifest_digest:
                raise ReleaseInvariantError("case result manifest digest mismatch")
            if result.case_id not in run.expected_case_ids:
                raise ReleaseInvariantError("case result is outside release run")
            key = (result.release_run_id, result.case_id)
            existing = self._results.get(key)
            if existing is not None:
                if existing != result:
                    raise ReleaseInvariantError("case result identity conflict")
                return existing
            if run.status is not ReleaseRunStatus.RUNNING:
                raise ReleaseInvariantError("cannot append result to terminal release run")
            self._results[key] = result
            return result

    def list_case_results(self, release_run_id: str) -> tuple[ReleaseCaseResult, ...]:
        """按 case ID 稳定读取 append-only 结果。"""

        with self._lock:
            self._required_run(release_run_id)
            return tuple(
                result
                for (_run_id, _case_id), result in sorted(self._results.items())
                if _run_id == release_run_id
            )

    def finalize_technical(self, release_run_id: str) -> TechnicalReleaseDecision:
        """从实际结果重算技术结论并原子结束 ReleaseRun。"""

        with self._lock:
            existing = self._technical.get(release_run_id)
            if existing is not None:
                return existing
            run = self._required_run(release_run_id)
            results = self.list_case_results(release_run_id)
            decision = _technical_for(run, results)
            self._technical[release_run_id] = decision
            self._runs[release_run_id] = ReleaseRun.model_validate(
                {**run.model_dump(mode="json"), "status": decision.status.value}
            )
            return decision

    def save_promotion(
        self,
        release_run_id: str,
        promotion: DecisionSupportPromotionDecision,
    ) -> FinalReleaseDecision:
        """保存唯一 Promotion 结论，并生成不可变最终双轨状态。"""

        with self._lock:
            technical = self._technical.get(release_run_id)
            if technical is None:
                raise ReleaseInvariantError("technical release decision is missing")
            existing = self._promotions.get(release_run_id)
            if existing is not None and existing != promotion:
                raise ReleaseInvariantError("promotion decision identity conflict")
            if existing is None:
                self._promotions[release_run_id] = promotion
            final = _final_for(release_run_id, technical, promotion)
            old_final = self._final.get(release_run_id)
            if old_final is not None and old_final != final:
                raise ReleaseInvariantError("final release decision identity conflict")
            self._final[release_run_id] = final
            return final

    def get_run(self, release_run_id: str) -> ReleaseRun:
        """读取 ReleaseRun 快照。"""

        with self._lock:
            return self._required_run(release_run_id)

    def get_decision(self, release_run_id: str) -> FinalReleaseDecision:
        """读取已合成的最终状态，不创建默认结论。"""

        with self._lock:
            try:
                return self._final[release_run_id]
            except KeyError as error:
                raise ReleaseInvariantError("final release decision is missing") from error

    def snapshot(self) -> Mapping[str, Any]:
        """返回只读快照供 Demo/报告消费，不暴露可写内部索引。"""

        with self._lock:
            return MappingProxyType(
                {
                    "runs": tuple(self._runs.values()),
                    "results": tuple(self._results.values()),
                    "technical": tuple(self._technical.values()),
                    "promotions": tuple(self._promotions.values()),
                    "final": tuple(self._final.values()),
                }
            )

    def _required_run(self, release_run_id: str) -> ReleaseRun:
        try:
            return self._runs[release_run_id]
        except KeyError as error:
            raise ReleaseInvariantError("release run does not exist") from error


class PostgresReleaseStore:
    """使用 PostgreSQL 唯一约束和行锁保存 Release 事实。"""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def create_run(self, run: ReleaseRun) -> ReleaseRun:
        """插入或重放 ReleaseRun，冲突由模型比较归一化。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO phase15_release_runs
                        (release_run_id, mode, manifest_digest, expected_case_ids, status)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (release_run_id) DO NOTHING;
                    """,
                    (run.release_run_id, run.mode.value, run.manifest_digest, Jsonb(list(run.expected_case_ids)), run.status.value),
                )
                cursor.execute("SELECT * FROM phase15_release_runs WHERE release_run_id=%s;", (run.release_run_id,))
                row = cursor.fetchone()
            conn.commit()
        loaded = self._run_from_row(row)
        if loaded != run:
            raise ReleaseInvariantError("release run identity conflict")
        return loaded

    def append_case_result(self, result: ReleaseCaseResult) -> ReleaseCaseResult:
        """在 run 行锁内验证 Manifest/case，再写唯一 JSON 快照。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM phase15_release_runs WHERE release_run_id=%s FOR UPDATE;", (result.release_run_id,))
                run_row = cursor.fetchone()
                if run_row is None:
                    raise ReleaseInvariantError("release run does not exist")
                run = self._run_from_row(run_row)
                if result.manifest_digest != run.manifest_digest or result.case_id not in run.expected_case_ids:
                    raise ReleaseInvariantError("case result identity does not match release run")
                cursor.execute(
                    """
                    INSERT INTO phase15_release_case_results
                        (release_run_id, case_id, manifest_digest, artifact_digest, status,
                         severe_violation, result_snapshot)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (release_run_id, case_id) DO NOTHING;
                    """,
                    (result.release_run_id, result.case_id, result.manifest_digest, result.artifact_digest, result.status.value, result.severe_violation, Jsonb(result.model_dump(mode="json"))),
                )
                cursor.execute("SELECT result_snapshot FROM phase15_release_case_results WHERE release_run_id=%s AND case_id=%s;", (result.release_run_id, result.case_id))
                row = cursor.fetchone()
            conn.commit()
        loaded = ReleaseCaseResult.model_validate(row["result_snapshot"])
        if loaded != result:
            raise ReleaseInvariantError("case result identity conflict")
        return loaded

    def list_case_results(self, release_run_id: str) -> tuple[ReleaseCaseResult, ...]:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT result_snapshot FROM phase15_release_case_results WHERE release_run_id=%s ORDER BY case_id;", (release_run_id,))
                rows = cursor.fetchall()
        return tuple(ReleaseCaseResult.model_validate(row["result_snapshot"]) for row in rows)

    def finalize_technical(self, release_run_id: str) -> TechnicalReleaseDecision:
        """锁定 Run，重算缺失/失败事实并保存唯一技术结论。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM phase15_release_runs WHERE release_run_id=%s FOR UPDATE;", (release_run_id,))
                row = cursor.fetchone()
                if row is None:
                    raise ReleaseInvariantError("release run does not exist")
                cursor.execute("SELECT decision_snapshot FROM phase15_release_technical_decisions WHERE release_run_id=%s;", (release_run_id,))
                existing = cursor.fetchone()
                if existing is not None:
                    return TechnicalReleaseDecision.model_validate(existing["decision_snapshot"])
                run = self._run_from_row(row)
                cursor.execute("SELECT result_snapshot FROM phase15_release_case_results WHERE release_run_id=%s ORDER BY case_id;", (release_run_id,))
                results = tuple(ReleaseCaseResult.model_validate(item["result_snapshot"]) for item in cursor.fetchall())
                decision = _technical_for(run, results)
                cursor.execute(
                    "INSERT INTO phase15_release_technical_decisions (release_run_id, status, decision_snapshot) VALUES (%s,%s,%s);",
                    (release_run_id, decision.status.value, Jsonb(decision.model_dump(mode="json"))),
                )
                cursor.execute("UPDATE phase15_release_runs SET status=%s, completed_at=now() WHERE release_run_id=%s;", (decision.status.value, release_run_id))
            conn.commit()
        return decision

    def save_promotion(self, release_run_id: str, promotion: DecisionSupportPromotionDecision) -> FinalReleaseDecision:
        """锁定技术结论并以唯一行保存 Promotion/Final 双轨事实。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT decision_snapshot FROM phase15_release_technical_decisions WHERE release_run_id=%s;", (release_run_id,))
                technical_row = cursor.fetchone()
                if technical_row is None:
                    raise ReleaseInvariantError("technical release decision is missing")
                technical = TechnicalReleaseDecision.model_validate(technical_row["decision_snapshot"])
                cursor.execute("SELECT decision_snapshot FROM phase15_release_decisions WHERE release_run_id=%s;", (release_run_id,))
                existing = cursor.fetchone()
                final = _final_for(release_run_id, technical, promotion)
                if existing is not None:
                    # 数据库快照同时保存 Promotion 和 Final 两条审计事实；恢复时
                    # 只重载 Final 投影，外层对象本身不是 FinalReleaseDecision。
                    loaded = FinalReleaseDecision.model_validate(existing["decision_snapshot"]["final"])
                    if loaded != final:
                        raise ReleaseInvariantError("final release decision identity conflict")
                    return loaded
                cursor.execute(
                    "INSERT INTO phase15_release_decisions (release_run_id, technical_status, promotion_status, final_status, decision_snapshot) VALUES (%s,%s,%s,%s,%s);",
                    (release_run_id, technical.status.value, promotion.status.value, final.status.value, Jsonb({"promotion": promotion.model_dump(mode="json"), "final": final.model_dump(mode="json")})),
                )
            conn.commit()
        return final

    @staticmethod
    def _run_from_row(row: Mapping[str, Any]) -> ReleaseRun:
        return ReleaseRun(
            release_run_id=row["release_run_id"],
            mode=row["mode"],
            manifest_digest=row["manifest_digest"],
            expected_case_ids=tuple(row["expected_case_ids"]),
            status=row["status"],
        )


def initialize_release_gate_schema(settings: Any) -> None:
    """执行 Phase 15 Task 4 幂等 DDL，供集成测试和迁移入口复用。"""

    sql_path = Path(__file__).resolve().parents[2] / "docker" / "init_phase15_release_gates.sql"
    sql = sql_path.read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
        conn.commit()
