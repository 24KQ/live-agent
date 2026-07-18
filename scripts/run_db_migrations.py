# -*- coding: utf-8 -*-
"""Phase 7B 统一数据库迁移入口。

按依赖顺序执行所有 Phase 的 DDL 初始化脚本。
支持 --dry-run 参数预览将要执行的 SQL 文件列表。
required=True 的迁移失败会标记为 failed，非 required 标记为 warning。

用法：
    python scripts/run_db_migrations.py          # 执行全部迁移
    python scripts/run_db_migrations.py --dry-run # 预览，不实际执行
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config.settings import get_settings


@dataclass
class MigrationStep:
    """单步迁移定义。"""

    phase: str                  # 阶段标识，如 "phase0", "phase7b"
    sql_file: str               # docker/ 目录下的 SQL 文件名
    required: bool = True       # 必需迁移：失败标记为 failed；非必需标记为 warning
    description: str = ""       # 中文说明


# 迁移清单（按依赖顺序排列）
MIGRATIONS: list[MigrationStep] = [
    MigrationStep(
        phase="phase0",
        sql_file="init_postgres.sql",
        required=True,
        description="基础 PostgreSQL 表结构（核心基础设施）",
    ),
    MigrationStep(
        phase="phase1",
        sql_file="init_phase1_audit.sql",
        required=False,
        description="Phase 1 审计表（AuditEvent + DecisionTrace）",
    ),
    MigrationStep(
        phase="phase2",
        sql_file="init_phase2_pre_live.sql",
        required=False,
        description="Phase 2 播前数据表（预演、商品、手卡）",
    ),
    MigrationStep(
        phase="phase3",
        sql_file="init_phase3_memory.sql",
        required=False,
        description="Phase 3 记忆表（MemoryStore + TrustScore）",
    ),
    MigrationStep(
        phase="phase3c",
        sql_file="alter_phase3c_embedding_dim.sql",
        required=False,
        description="Phase 3C 向量维度调整",
    ),
    MigrationStep(
        phase="phase4",
        sql_file="init_phase4_danmaku_aggregates.sql",
        required=False,
        description="Phase 4 弹幕聚合持久化表",
    ),
    MigrationStep(
        phase="phase6c",
        sql_file="init_phase6c_harness_sessions.sql",
        required=True,
        description="Phase 6C Harness Agent Web 会话表（副屏审批 + 人审入口）",
    ),
    MigrationStep(
        phase="phase7a",
        sql_file="init_phase7a_agent_evaluations.sql",
        required=True,
        description="Phase 7A Agent 评估表（回放、评估、评分、复核）",
    ),
    MigrationStep(
        phase="phase7b",
        sql_file="init_phase7b_production_hardening.sql",
        required=True,
        description="Phase 7B 生产硬化扩展（Harness 会话扩展 + 运维告警表）",
    ),
    MigrationStep(
        phase="phase11b",
        sql_file="init_phase11b_skill_attempts.sql",
        required=True,
        description="Phase 11B Skill 执行尝试事实表（Operation + Attempt）",
    ),
    MigrationStep(
        phase="phase12a",
        sql_file="init_phase12a_plan_engine.sql",
        required=True,
        description="Phase 12A DAG PlanEngine 权威事实表（Plan + NodeRun + Command）",
    ),
    MigrationStep(
        phase="phase12b",
        sql_file="init_phase12b_preemption.sql",
        required=True,
        description="Phase 12B 售罄事件事实、投递记录与计划 lineage",
    ),
    MigrationStep(
        phase="phase13",
        sql_file="init_phase13_specialist_evaluations.sql",
        required=True,
        description="Phase 13 Specialist 评估、预算与模型调用事实",
    ),
    MigrationStep(
        phase="phase13_memory",
        sql_file="init_phase13_memory_candidates.sql",
        required=True,
        description="Phase 13 播后记忆候选与受控晋升事实",
    ),
    MigrationStep(
        phase="phase14_decision_support",
        sql_file="init_phase14_decision_support.sql",
        required=True,
        description="Phase 14 人机协同 Workspace、Proposal 与 OperatorDecision 事实",
    ),
    MigrationStep(
        phase="phase14_memory_feedback",
        sql_file="init_phase14_memory_feedback.sql",
        required=True,
        description="Phase 14 记忆资格与人工确认事实",
    ),
    MigrationStep(
        phase="phase15",
        sql_file="init_phase15_release_gates.sql",
        required=True,
        description="Phase 15 Golden Release 与双轨结论基础事实",
    ),
]


def _run_sql_file(settings: Any, sql_path: Path, *, dry_run: bool) -> list[str]:
    """执行单个 SQL 文件，返回执行过程中的错误信息列表。"""
    errors: list[str] = []

    if not sql_path.exists():
        errors.append(f"SQL 文件不存在: {sql_path}")
        return errors

    sql = sql_path.read_text(encoding="utf-8-sig")
    if not sql.strip():
        errors.append(f"SQL 文件为空: {sql_path}")
        return errors

    if dry_run:
        return errors  # 预览模式不执行

    try:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
    except psycopg.Error as exc:
        errors.append(f"SQL 执行失败: {exc}")
        return errors

    return errors


def run_migrations(settings: Any | None = None, *, dry_run: bool = False) -> None:
    """按顺序执行全部迁移。"""
    if settings is None:
        settings = get_settings()

    project_root = Path(__file__).resolve().parents[1]
    docker_dir = project_root / "docker"

    if dry_run:
        print("=" * 60)
        print("  迁移预览模式（--dry-run）")
        print("=" * 60)

    passed: list[str] = []
    warnings: list[str] = []
    failed: list[str] = []

    for step in MIGRATIONS:
        sql_path = docker_dir / step.sql_file
        label = f"[{step.phase}] {step.description}"

        if dry_run:
            status = "SKIP(required)" if step.required else "SKIP(optional)"
            exists = "EXISTS" if sql_path.exists() else "MISSING"
            print(f"  {label:50s} {status:15s} {exists}")
            continue

        errors = _run_sql_file(settings, sql_path, dry_run=False)

        if not errors:
            passed.append(step.phase)
            print(f"  {label:50s} [PASS]")
        else:
            for err in errors:
                print(f"  {label:50s} [FAIL] {err}")
            if step.required:
                failed.append(step.phase)
            else:
                warnings.append(step.phase)

    # 汇总报告
    if not dry_run:
        print()
        print("=" * 60)
        print(f"  迁移完成：{len(passed)} passed, {len(warnings)} warnings, {len(failed)} failed")
        print("=" * 60)
        if warnings:
            for w in warnings:
                print(f"  [WARNING] {w} — 非必需迁移失败，不影响核心功能")
        if failed:
            for f in failed:
                print(f"  [FAILED]  {f} — 必需迁移失败，系统可能无法正常运行")
            sys.exit(1)

    if dry_run:
        print()
        print(f"  共 {len(MIGRATIONS)} 个迁移步骤（required={sum(1 for m in MIGRATIONS if m.required)}）")
        print("  使用 --dry-run 移除以实际执行迁移。")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run_migrations(dry_run=dry_run)
