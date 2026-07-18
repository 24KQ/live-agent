"""生成 Phase 15 48 例 Golden Dataset 与冻结 Manifest。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    # 允许从仓库根目录直接运行生成器，不依赖外部 PYTHONPATH。
    sys.path.insert(0, str(PROJECT_ROOT))

from src.release_gates.dataset import generate_phase15_dataset


def main(argv: list[str] | None = None) -> int:
    """生成指定 evaluation 根目录的确定性资产。"""

    parser = argparse.ArgumentParser(description="generate Phase 15 Golden Dataset")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "evaluation")
    args = parser.parse_args(argv)
    manifest = generate_phase15_dataset(args.output_root, source_root=PROJECT_ROOT)
    print(manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
