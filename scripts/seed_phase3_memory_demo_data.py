"""初始化 Phase 3A 记忆与信任样例数据。"""

from pathlib import Path
import sys


# 直接运行脚本时，把项目根目录加入导入路径，避免要求开发者手动设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings
from src.memory.demo_memory_seed import initialize_phase3_schema, seed_phase3_memory_demo_data
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data


def main() -> int:
    """初始化 Phase 2 货盘和 Phase 3A 记忆数据，保证外键依赖完整。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    result = seed_phase3_memory_demo_data(settings)

    print("Phase 3A memory demo data seeded")
    print(f"memory_count: {result.memory_count}")
    print(f"default_trust_score: {result.trust_score}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
