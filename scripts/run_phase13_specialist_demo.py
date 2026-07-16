"""输出 Phase 13 无付费、多 Profile 默认关闭的可重复 Demo。"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.specialist_evaluation.demo import build_demo_routes


def main() -> int:
    """只读取冻结结论投影；不访问模型、数据库或任何生产写路径。"""

    payload = {
        "retained_profile_count": 0,
        "routes": [route.__dict__ for route in build_demo_routes(retained_count=0)],
        "candidate_outcomes": {
            "LIVE_OPS": "REJECTED",
            "PLANNER": "INCONCLUSIVE",
            "REVIEW_MEMORY": "INCONCLUSIVE",
        },
        "memory_promotion_boundary": "DETERMINISTIC_PROMOTION_POLICY_ONLY",
        "agent_to_agent": "FORBIDDEN",
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
