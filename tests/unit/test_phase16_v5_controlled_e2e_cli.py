"""Phase 16 V5 命令入口的离线安全契约。

默认入口只能执行本地 Manifest 预检；本文件通过动态加载脚本和替身执行器验证显式开关，
不会加载用户凭据、连接 PostgreSQL 或发起真实模型请求。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "run_phase16_v5_controlled_e2e.py"


def _load_cli_module() -> object:
    """使用唯一模块名加载 CLI，避免测试之间共享导入状态或运行 ``__main__`` 分支。"""

    module_name = f"phase16_v5_controlled_e2e_cli_{uuid4().hex}"
    specification = importlib.util.spec_from_file_location(module_name, _SCRIPT_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


def test_v5_default_cli_dry_run_never_loads_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """无参数默认 dry-run 不得读取 ``.env``，否则离线身份检查会错误扩大凭据暴露面。"""

    import dotenv

    module = _load_cli_module()
    monkeypatch.setattr(
        dotenv,
        "load_dotenv",
        lambda *_args, **_kwargs: pytest.fail("V5 default dry-run must not load dotenv"),
    )

    exit_code = asyncio.run(module.main([]))

    assert exit_code == 0
    assert '"network_enabled": false' in capsys.readouterr().out


def test_v5_execute_calibration_returns_success_without_leaking_local_import_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式校准路径只依据脱敏执行摘要判定退出码，不能依赖 ``_execute`` 内的局部导入。"""

    module = _load_cli_module()
    calls: list[bool] = []

    async def _passing_execute(*, formal: bool) -> dict[str, object]:
        """替身执行器只验证 CLI 分支，不装配 Adapter、账本或任何外部依赖。"""

        calls.append(formal)
        return {
            "mode": "EXECUTE_CALIBRATION",
            "status": "PASS",
            "evidence_conclusion": "INCONCLUSIVE",
            "reason_codes": [],
            "model_calls": 2,
            "attempt_created": True,
            "network_enabled": True,
        }

    monkeypatch.setattr(module, "_execute", _passing_execute)

    assert asyncio.run(module.main(["--execute-calibration"])) == 0
    assert calls == [False]
