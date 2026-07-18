"""Phase 15 Task 1 的本地门禁入口骨架。

Task 8/12 会把完整 Golden Runner 和 Acceptance 报告接入这个稳定入口；在此之前
只输出明确的 BLOCKED 事实，绝不把尚未实现的 Release 误报为 PASS，也不触发模型、
数据库或 GitHub Actions。
"""

from __future__ import annotations

import json


def main() -> int:
    """输出当前阶段的诚实占位结果，保持入口可发现且默认 fail-closed。"""

    print(
        json.dumps(
            {
                "phase": "15",
                "technical_release": "BLOCKED",
                "promotion": "BLOCKED",
                "reason": "phase15_release_kernel_pending_tasks",
                "external_calls": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
