"""生成冻结的 Phase 16 受控双 Agent 离线评估资产。"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.decision_support.multi_agent_evaluation import (
    generate_phase16_controlled_multi_agent_dataset,
)


def main() -> None:
    """只接受明确输出目录，避免误写 Phase 13-15 的历史评估资产。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    manifest = generate_phase16_controlled_multi_agent_dataset(arguments.output)
    print(manifest.manifest_digest)


if __name__ == "__main__":
    main()
