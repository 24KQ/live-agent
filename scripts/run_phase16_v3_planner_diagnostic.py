"""执行 Phase 16 V3 单 Planner 真实模型诊断。

默认模式只复验父数据和冻结 Profile；只有显式 ``--execute`` 才会读取本机凭据、连接
PostgreSQL 并发送一次 DeepSeek 请求。该脚本不改变生产 `DETERMINISTIC_ONLY` 路由。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    # 支持从任意工作目录调用，但只把本仓库根加入 import path，不能搜索用户目录。
    sys.path.insert(0, str(_PROJECT_ROOT))

# V3 使用同一受保护 HMAC 根密钥，但签名 payload 在实现中带有独立 V3 域，不会与 V2
# receipt 标签互相可替换。密钥只存在于当前进程内存，绝不写入输出或报告。
_HMAC_ENV = "PHASE16_OFFICIAL_SMOKE_V2_RECEIPT_HMAC_HEX"


class _Blocked(RuntimeError):
    """命令入口的稳定、脱敏阻断码，不携带凭据或异常正文。"""


def _arguments() -> argparse.Namespace:
    """解析唯一联网开关；不存在 legacy/direct 绕过模式。"""

    parser = argparse.ArgumentParser(description="Run one audited Phase 16 V3 Planner diagnostic.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="after dry-run identity checks, allow exactly one external Planner call",
    )
    return parser.parse_args()


def _endpoint_host(value: str) -> str:
    """严格收敛 OpenAI BASE URL，防止路径、端口或用户信息绕过冻结 endpoint 身份。"""

    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.deepseek.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise _Blocked("ENDPOINT_CONFIG_INVALID")
    return parsed.hostname


def _hmac_key() -> bytes:
    """读取 256 位以上 HMAC key；解析失败只返回稳定阻断码，不能泄漏原值。"""

    try:
        value = bytes.fromhex(os.environ.get(_HMAC_ENV, "").strip())
    except ValueError as error:
        raise _Blocked("DIAGNOSTIC_HMAC_UNAVAILABLE") from error
    if len(value) < 32:
        raise _Blocked("DIAGNOSTIC_HMAC_UNAVAILABLE")
    return value


def _load_parent_assets():
    """读取 V2 冻结数据与 Manifest，V3 只引用它们而不生成、修改或覆盖历史文件。"""

    from src.decision_support.multi_agent_evaluation import (
        load_phase16_controlled_multi_agent_dataset,
    )
    from src.decision_support.official_smoke_evidence_v2 import (
        Phase16OfficialPriceEvidence,
        load_phase16_official_smoke_v2_evidence_manifest,
    )

    dataset = load_phase16_controlled_multi_agent_dataset(
        _PROJECT_ROOT / "evaluation" / "phase16_controlled_multi_agent"
    )
    manifest = load_phase16_official_smoke_v2_evidence_manifest(repository_root=_PROJECT_ROOT)
    price = Phase16OfficialPriceEvidence.create(
        model_id="deepseek-v4-pro",
        endpoint_host="api.deepseek.com",
        input_cny_per_million=Decimal("3.000000"),
        output_cny_per_million=Decimal("6.000000"),
    )
    return dataset, manifest, price


async def _execute() -> dict[str, object]:
    """只在显式授权分支装配模型、HMAC 和账本；构造对象本身不提前发送网络请求。"""

    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_API_BASE_URL", "").strip()
    if not api_key or api_key == "change_me":
        raise _Blocked("CREDENTIAL_UNAVAILABLE")
    _endpoint_host(base_url)
    signing_key = _hmac_key()
    dataset, manifest, price = _load_parent_assets()

    from src.config.settings import get_settings
    from src.decision_support.official_smoke_ledger_v3 import (
        Phase16V3FailureAuthenticator,
        Phase16V3ReceiptAuthenticator,
        PostgresPhase16V3PlannerDiagnosticLedger,
    )
    from src.decision_support.official_smoke_runner_v3 import Phase16V3PlannerDiagnosticRunner
    from src.specialist_runtime.deepseek_adapter import DeepSeekAgentModelAdapter

    runner = Phase16V3PlannerDiagnosticRunner(
        dataset=dataset,
        parent_manifest=manifest,
        official_price=price,
        ledger=PostgresPhase16V3PlannerDiagnosticLedger(
            get_settings(),
            failure_authenticator=Phase16V3FailureAuthenticator(signing_key),
            receipt_authenticator=Phase16V3ReceiptAuthenticator(signing_key),
        ),
        model_port=DeepSeekAgentModelAdapter(api_key=api_key),
        clock=lambda: datetime.now(timezone.utc),
    )
    report = await runner.execute()
    return {
        "mode": "EXECUTE",
        "run_id": "phase16-v3-planner-diagnostic-001",
        "status": report.status.value,
        "reason_code": report.reason_code,
        "attempt_created": report.attempt_id is not None,
    }


async def main() -> int:
    """输出白名单摘要并使用稳定退出码，外部失败不打印原始异常或模型内容。"""

    arguments = _arguments()
    if not arguments.execute:
        try:
            _load_parent_assets()
            payload: dict[str, object] = {
                "mode": "DRY_RUN",
                "run_id": "phase16-v3-planner-diagnostic-001",
                "status": "READY",
                "reason_code": "OFFLINE_IDENTITY_READY",
                "attempt_created": False,
            }
        except Exception:
            payload = {
                "mode": "DRY_RUN",
                "run_id": "phase16-v3-planner-diagnostic-001",
                "status": "BLOCKED",
                "reason_code": "OFFLINE_IDENTITY_BLOCKED",
                "attempt_created": False,
            }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    try:
        payload = await _execute()
    except _Blocked as error:
        payload = {
            "mode": "EXECUTE",
            "run_id": "phase16-v3-planner-diagnostic-001",
            "status": "BLOCKED",
            "reason_code": str(error),
            "attempt_created": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    except Exception:
        # 账本/schema/装配异常在模型端口前或端口契约之外发生时，只公开稳定 fail-closed
        # 结果。具体的 ModelFailure 由 Runner 写入受签名账本并在报告中读取。
        payload = {
            "mode": "EXECUTE",
            "run_id": "phase16-v3-planner-diagnostic-001",
            "status": "BLOCKED",
            "reason_code": "DIAGNOSTIC_RUNTIME_BLOCKED",
            "attempt_created": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
