"""Phase 16 唯一正式 smoke CLI 的离线安全契约。"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "run_phase16_real_smoke.py"


def _load_cli_module() -> object:
    """以唯一动态模块名加载 CLI，避免多个测试共享模块级状态或执行 ``__main__`` 分支。"""

    module_name = f"phase16_official_smoke_cli_{uuid4().hex}"
    specification = importlib.util.spec_from_file_location(module_name, _SCRIPT_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


def test_default_cli_dry_run_never_loads_dotenv_or_execute_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """无参数入口只做离线预检，不得加载 `.env`、适配器、HMAC key 或 PostgreSQL。"""

    import dotenv

    def _forbid_dotenv(*_args, **_kwargs):
        """任何默认路径的 dotenv 调用都是将凭据暴露给非联网操作的安全回归。"""

        raise AssertionError("default dry-run must not load dotenv")

    monkeypatch.setattr(dotenv, "load_dotenv", _forbid_dotenv)
    module = _load_cli_module()
    monkeypatch.setattr(
        module,
        "_build_execute_runner",
        lambda: pytest.fail("default dry-run must not build execute dependencies"),
        raising=False,
    )

    exit_code = asyncio.run(module.main([]))

    assert exit_code == 0
    assert "DRY_RUN" in capsys.readouterr().out


def test_execute_flag_is_the_only_path_that_builds_formal_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有显式 `--execute` 才能进入可联网的正式 Runner 装配边界。"""

    module = _load_cli_module()
    calls: list[str] = []

    class _PassingRunner:
        """最小替身只验证命令分支，不重放真实 API、账本或模型正文。"""

        async def execute(self):
            """返回脱敏 PASS 汇总，模拟正式 Runner 已完成其受控链。"""

            return SimpleNamespace(
                run_id="phase16-official-smoke-v1",
                status="PASS",
                evidence_conclusion="PASS",
                reason_codes=(),
                case_executions=(),
                model_calls=20,
            )

    def _build_runner():
        """记录唯一允许创建真实依赖的 execute 分支。"""

        calls.append("execute")
        return _PassingRunner()

    monkeypatch.setattr(module, "_build_execute_runner", _build_runner, raising=False)

    assert asyncio.run(module.main(["--execute"])) == 0
    assert calls == ["execute"]


def test_legacy_direct_mode_is_hard_disabled_before_dependency_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史直连模式保留可解释错误，但永远不能成为绕过正式账本的联网路径。"""

    module = _load_cli_module()
    monkeypatch.setattr(
        module,
        "_build_execute_runner",
        lambda: pytest.fail("legacy direct mode must not build execute dependencies"),
        raising=False,
    )

    with pytest.raises(SystemExit, match="LEGACY_DIRECT_MODE_DISABLED"):
        asyncio.run(module.main(["--legacy-direct-mode"]))
