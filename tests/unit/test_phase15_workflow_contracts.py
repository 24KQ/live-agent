"""Phase 15 Task 9 GitHub Actions 三层 workflow 的 TDD 契约。

测试只解析仓库中的 YAML 文本，不连接 GitHub、PostgreSQL、Kafka 或真实模型；它
把托管环境的触发、权限、版本、case split 和 artifact 生命周期固定为可审计事实。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = PROJECT_ROOT / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    """加载单个 workflow，并拒绝空文件或非对象根节点。"""

    path = WORKFLOW_ROOT / name
    assert path.is_file(), f"missing workflow: {path}"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _trigger(workflow: dict[str, Any]) -> Any:
    """兼容 YAML 1.1 把未加引号的 on 解析为 bool 的解析器差异。"""

    return workflow.get("on", workflow.get(True))


def _job(workflow: dict[str, Any]) -> dict[str, Any]:
    """获取唯一门禁 job，避免测试放过空 workflow。"""

    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and len(jobs) == 1
    job = next(iter(jobs.values()))
    assert isinstance(job, dict)
    return job


def _all_run_commands(job: dict[str, Any]) -> str:
    """合并所有 shell 片段，检查门禁入口和 case split。"""

    return "\n".join(
        str(step.get("run", ""))
        for step in job.get("steps", [])
        if isinstance(step, dict)
    )


def test_phase15_pr_workflow_uses_python_312_pgvector_kafka_and_36_cases_without_secrets() -> None:
    """PR 只跑非 holdout，且不能把模型 secret 暴露到不受保护的 PR。"""

    workflow = _load("agent-runtime-pr.yml")
    trigger = _trigger(workflow)
    assert isinstance(trigger, dict) and "pull_request" in trigger
    assert "workflow_dispatch" not in trigger
    assert workflow["permissions"] == {"contents": "read"}
    job = _job(workflow)
    assert job["permissions"] == {"contents": "read"}
    services = job["services"]
    # 迁移会创建 pgvector 扩展，PR 容器必须与真实 schema 前提一致。
    assert services["postgres"]["image"] == "pgvector/pgvector:pg16"
    # 解析单测和 Kafka 集成回归都需要真实 broker，不能依赖开发机残留服务。
    assert "zookeeper" in services
    assert "kafka" in services
    assert job["env"]["KAFKA_BOOTSTRAP_SERVERS"] == "localhost:9092"
    steps = job["steps"]
    # Phase 16 的历史闭包审计必须读取一次真实执行提交的 Git blob；浅检出会让
    # `git ls-tree <execution-commit>` 在 CI 中缺对象并 fail-closed，因此 PR 必须保留完整历史。
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    setup = next(step for step in steps if step.get("uses", "").startswith("actions/setup-python@"))
    assert setup["with"]["python-version"] == "3.12"
    commands = _all_run_commands(job)
    assert "socket.create_connection" in commands
    assert "--mode pr" in commands
    assert "36" in commands or "non-holdout" in commands
    assert "secrets." not in workflow.__repr__()
    assert "DEEPSEEK_API_KEY" not in workflow.__repr__()
    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["retention-days"] == 14
    assert "coverage erase" in commands
    assert "--append" in commands
    assert "phase16-coverage-source-closure-v1.json" in commands
    assert "--source-closure-file" in commands
    assert "--source src" in commands
    assert "coverage json --include" in commands
    assert "--docs-only" in commands


def test_phase15_nightly_workflow_has_schedule_postgres_kafka_and_36_cases() -> None:
    """Nightly 执行完整基础设施回归，但仍不解封 holdout 或真实模型。"""

    workflow = _load("agent-runtime-nightly.yml")
    trigger = _trigger(workflow)
    assert isinstance(trigger, dict) and "schedule" in trigger
    assert "pull_request" not in trigger
    assert workflow["permissions"] == {"contents": "read"}
    job = _job(workflow)
    assert job["permissions"] == {"contents": "read"}
    services = job["services"]
    assert services["postgres"]["image"] == "pgvector/pgvector:pg16"
    assert "kafka" in services
    checkout = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0
    commands = _all_run_commands(job)
    assert "--mode nightly" in commands
    assert "36" in commands or "non-holdout" in commands
    assert "REAL_MODEL=0" in commands or "real model" in commands.lower()
    upload = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["retention-days"] == 30


def test_phase15_release_workflow_is_tag_or_manual_only_48_cases_and_180_day_artifact() -> None:
    """Release 只能由 release tag/手动触发，并在保护环境保存长期证据。"""

    workflow = _load("agent-runtime-release.yml")
    trigger = _trigger(workflow)
    assert isinstance(trigger, dict)
    assert "workflow_dispatch" in trigger
    assert trigger["push"]["tags"] == ["phase15-release-*"]
    assert "pull_request" not in trigger
    assert workflow["permissions"] == {"contents": "read"}
    job = _job(workflow)
    assert job["environment"] == "phase15-release"
    assert job["services"]["postgres"]["image"] == "pgvector/pgvector:pg16"
    checkout = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0
    commands = _all_run_commands(job)
    assert "--mode release" in commands
    assert "48" in commands or "full" in commands.lower()
    assert "fetch_github_actions_evidence.py" in commands
    assert "--require-evidence" in commands
    assert "PHASE15_EVIDENCE_JSON" in workflow.__repr__()
    upload = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["retention-days"] == 180
