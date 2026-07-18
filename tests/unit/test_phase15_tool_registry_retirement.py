"""Phase 15 Task 10 ToolRegistry Facade 退役的 TDD 契约。

测试先固定生产代码边界：Facade 文件和兼容构造参数都必须消失，而 Catalog 与
SkillPolicyView 继续是唯一能力事实源。旧历史测试随后迁移到该公共契约。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from src.core.agent_tool_executor import AgentToolExecutor
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.policy_view import get_default_skill_policy_view


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"


def test_tool_registry_facade_file_is_retired() -> None:
    """生产包不再保留可导入的 ToolRegistry Facade 文件。"""

    assert not (SRC_ROOT / "config" / "tool_registry.py").exists()


def test_production_source_has_no_tool_registry_facade_symbols() -> None:
    """生产源码不得通过 import、名称或旧工厂重新扩大 Facade 使用面。"""

    forbidden = ("ToolRegistry", "get_default_tool_registry", "src.config.tool_registry")
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        if any(marker in content for marker in forbidden):
            offenders.append(path.relative_to(SRC_ROOT).as_posix())
    assert offenders == []


def test_agent_tool_executor_only_accepts_skill_policy_view() -> None:
    """Executor 构造签名不再接受旧 registry 位置或关键字参数。"""

    parameters = inspect.signature(AgentToolExecutor.__init__).parameters
    assert "registry" not in parameters
    assert "policy_view" in parameters

    with pytest.raises(TypeError):
        AgentToolExecutor(registry=get_default_skill_policy_view(), pre_live_service=object())


def test_catalog_and_policy_view_remain_the_single_governance_projection() -> None:
    """Facade 删除后，Catalog 与 SkillPolicyView 仍必须保留相同 ID/版本快照。"""

    catalog = {manifest.skill_id: manifest.version for manifest in get_default_skill_catalog()}
    policy_view = get_default_skill_policy_view()
    projected = {skill_id: policy_view.get(skill_id).version for skill_id in policy_view.skill_ids()}
    assert projected == catalog


def test_legacy_exception_observation_is_sanitized() -> None:
    """Facade 退役后的兼容入口也不能把底层异常文本泄露给 Agent。"""

    class FailingService:
        def query_products(self, room_id: str, trace_id: str) -> None:
            raise RuntimeError("postgres password=secret internal traceback")

    observation = AgentToolExecutor(pre_live_service=FailingService()).execute(
        "query_products",
        {},
        "room-001",
        "trace-001",
    )

    assert observation.status == "error"
    assert observation.summary == "HANDLER_FAILED: legacy execution failed"
    assert "secret" not in observation.summary
