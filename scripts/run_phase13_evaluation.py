"""Phase 13 正式评估的预检入口。"""

from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # 允许从仓库根直接执行 python scripts/run_phase13_evaluation.py，避免依赖外部 PYTHONPATH。
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings
from src.specialist_evaluation.runner import evaluate_preflight_only


def main() -> int:
    """只输出预检事实；真实模型执行必须由后续已签发的正式运行入口显式解封。"""

    settings = get_settings()
    project_root = Path(__file__).parents[1]
    pricing_snapshot = (
        project_root / "evaluation" / "pricing" / "deepseek-v4-flash-2026-07-16.json"
    )
    report = evaluate_preflight_only(
        api_key=settings.llm_api_key,
        # 预检必须接收完整 URL，才能拒绝 HTTP 降级、用户信息和意外 path/query。
        endpoint_host=settings.llm_api_base_url,
        model_id=settings.llm_model,
        pricing_snapshot_present=pricing_snapshot.is_file(),
    )
    # 输出中只保留门禁代码和候选结论，严禁将 API key、请求正文或模型响应写入终端。
    print(
        json.dumps(
            {
                "real_model_preflight": {
                    "allowed": report.real_model_preflight.allowed,
                    "reason_code": report.real_model_preflight.reason_code,
                },
                "candidate_outcomes": {
                    candidate.value: {
                        "decision": None
                        if outcome.decision is None
                        else outcome.decision.value,
                        "reason_code": outcome.reason_code,
                    }
                    for candidate, outcome in report.outcomes.items()
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
