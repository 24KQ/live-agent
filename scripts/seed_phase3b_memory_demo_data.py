"""初始化 Phase 3B 记忆检索与冲突修正样例数据。"""

from pathlib import Path
import sys


# 直接运行脚本时，把项目根目录加入导入路径，避免要求开发者手动设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings
from src.memory.demo_memory_seed import initialize_phase3_schema
from src.memory.demo_memory_seed_phase3b import initialize_phase3b_demo_data
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data


def main() -> int:
    """初始化 Phase 2A 货盘、Phase 3 表结构和 Phase 3B 独立样例。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    result = initialize_phase3b_demo_data(settings)

    print("Phase 3B memory revision demo data seeded")
    print(f"anchor_id: {result.anchor_id}")
    print(f"room_id: {result.room_id}")
    print(f"reset_memory_key: {result.reset_memory_key}")
    print(f"product_count: {result.product_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
