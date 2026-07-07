"""初始化 Phase 2A 播前演示数据。"""

from pathlib import Path
import sys


# 直接执行 `python scripts/seed_phase2_demo_data.py` 时，Python 默认只把
# scripts 目录加入 sys.path。这里显式加入仓库根目录，保证可以导入 src 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data


def main() -> int:
    """执行 schema 初始化和样例数据写入。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    result = seed_phase2_demo_data(settings)
    print(
        "Phase 2A demo data ready: "
        f"anchors={result.anchor_count}, rooms={result.room_count}, products={result.product_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
