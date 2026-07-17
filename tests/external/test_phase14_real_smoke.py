"""Phase 14 真实模型 smoke 的显式外部入口。

默认测试配置永远跳过该文件；真实环境必须先由 Task 11 预检构造可信门，再由受控
执行器注入 Model Port。这里不保存 API Key，也不提供绕过预检的测试开关。
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.external


def test_real_smoke_requires_explicit_external_environment() -> None:
    """没有受控外部环境时保持跳过，避免默认回归发送网络请求。"""

    pytest.skip("real Phase 14 smoke requires an externally provisioned, preflighted Model Port")
