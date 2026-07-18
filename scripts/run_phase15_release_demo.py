"""Phase 15 本地 Golden Release 门禁演示。

Demo 只调用统一 ``run_release_gate`` 内核，使用临时 artifact 目录避免污染仓库；
它展示 48 个冻结 case 的技术结果和缺少真实模型/真人证据时的禁用结论。
"""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # 直接执行脚本时补入项目根，确保能够加载 src 和 scripts 包。
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_release_gate import main as release_main


def main() -> int:
    """使用统一本地 Release CLI 展示确定性技术 PASS 和 Copilot BLOCKED。"""

    with TemporaryDirectory(prefix="phase15-demo-") as directory:
        return release_main(
            ["--mode", "pr", "--subject", "all", "--output-dir", directory]
        )


if __name__ == "__main__":
    raise SystemExit(main())
