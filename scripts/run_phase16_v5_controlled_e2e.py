"""执行 Phase 16 V5 禁思考受控双 Agent E2E campaign。

默认模式只重建 Manifest 身份，不读取 ``.env``、不连接 PostgreSQL、更不会向模型联网。
只有 ``--execute-calibration`` 或 ``--execute-formal`` 才允许装配真实 Provider Adapter；
正式 run 还必须由数据库中已认证的校准 PASS 显式解锁。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    # 保证无论从哪里启动都只导入当前工作树的受控模块，避免用户目录中的同名脚本被加载。
    sys.path.insert(0, str(_PROJECT_ROOT))

_HMAC_ENV = "PHASE16_OFFICIAL_SMOKE_V2_RECEIPT_HMAC_HEX"


class _Blocked(RuntimeError):
    """CLI 仅输出稳定阻断码，不能把端点原文、密钥或数据库异常打印到终端。"""


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """V5 不提供自由模型、Prompt、case、预算或重试参数，所有协议均由 Manifest 冻结。"""

    parser = argparse.ArgumentParser(description="Run Phase 16 V5 controlled E2E evidence campaign.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--execute-calibration",
        action="store_true",
        help="after offline checks, send the one synthetic Analyst -> Planner calibration",
    )
    group.add_argument(
        "--execute-formal",
        action="store_true",
        help="after offline checks and calibration PASS, send the frozen ten-case formal run",
    )
    return parser.parse_args(argv)


def _endpoint_host(value: str) -> str:
    """仅接受冻结 HTTPS DeepSeek 根地址，拒绝路径、端口、凭据和重定向入口。"""

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
    """复用现有受保护账本密钥材料但使用 V5 独立签名 domain，不输出或持久化其内容。"""

    try:
        key = bytes.fromhex(os.environ.get(_HMAC_ENV, "").strip())
    except ValueError as error:
        raise _Blocked("RECEIPT_HMAC_UNAVAILABLE") from error
    if len(key) < 32:
        raise _Blocked("RECEIPT_HMAC_UNAVAILABLE")
    return key


def _dry_run_payload() -> dict[str, object]:
    """在离线模式只输出 Manifest 和校准门的安全摘要。"""

    from src.decision_support.controlled_e2e_v5 import preflight_phase16_v5

    manifest, reasons = preflight_phase16_v5(repository_root=_PROJECT_ROOT)
    return {
        "mode": "DRY_RUN",
        "status": "READY" if not reasons else "BLOCKED",
        "reason_codes": list(reasons),
        "manifest_digest": None if manifest is None else manifest.manifest_digest,
        "attempt_created": False,
        "network_enabled": False,
    }


async def _execute(*, formal: bool) -> dict[str, object]:
    """只在显式联网开关后装配环境、Adapter、账本和 V5 Runner。"""

    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key or api_key == "change_me":
        raise _Blocked("CREDENTIAL_UNAVAILABLE")
    _endpoint_host(os.environ.get("LLM_API_BASE_URL", "").strip())
    if os.environ.get("LLM_MODEL", "").strip() != "deepseek-v4-pro":
        raise _Blocked("MODEL_CONFIG_MISMATCH")
    key = _hmac_key()

    from src.config.settings import get_settings
    from src.decision_support.controlled_e2e_ledger_v5 import (
        Phase16V5RunKind,
        PostgresPhase16V5CampaignLedger,
    )
    from src.decision_support.controlled_e2e_v5 import (
        Phase16V5ControlledE2ERunner,
        Phase16V5ExecutionStatus,
        load_phase16_v5_parent_dataset,
        preflight_phase16_v5,
    )
    from src.decision_support.controlled_e2e_adapter_v5 import DeepSeekV5ControlledE2EAdapter

    manifest, reasons = preflight_phase16_v5(repository_root=_PROJECT_ROOT)
    if manifest is None or reasons:
        raise _Blocked("OFFLINE_PREFLIGHT_BLOCKED")
    run_kind = Phase16V5RunKind.FORMAL if formal else Phase16V5RunKind.CALIBRATION
    # V4 专属 Adapter 显式注入 thinking.disabled，V5 不修改任何被历史 Manifest 绑定的
    # 共享 Adapter 源码，也不会回传或保存 reasoning_content。V9 渠道链签名下保留单端点
    # 链：host 与共享 runner 的 env 覆写（LLM_API_ENDPOINT_HOST）同源，行为与旧签名一致。
    endpoint_host = os.environ.get("LLM_API_ENDPOINT_HOST", "").strip() or "api.deepseek.com"
    runner = Phase16V5ControlledE2ERunner(
        dataset=load_phase16_v5_parent_dataset(repository_root=_PROJECT_ROOT),
        manifest=manifest,
        ledger=PostgresPhase16V5CampaignLedger(get_settings(), hmac_key=key),
        model_port=DeepSeekV5ControlledE2EAdapter(
            endpoints=((endpoint_host, api_key),),
        ),
    )
    report = await runner.execute(run_kind=run_kind)
    return {
        "mode": "EXECUTE_FORMAL" if formal else "EXECUTE_CALIBRATION",
        "run_id": report.run_id,
        "status": report.status.value,
        "evidence_conclusion": report.evidence_conclusion.value,
        "reason_codes": list(report.reason_codes),
        "model_calls": report.model_calls,
        "attempt_created": report.model_calls > 0,
        "network_enabled": True,
        "thinking_mode": "disabled",
        "case_count": len(report.case_executions),
    }


async def main(argv: list[str] | None = None) -> int:
    """仅打印脱敏 JSON，且任何运行时装配异常都只表示未能安全开始而非模型成功。"""

    arguments = _arguments(argv)
    if not arguments.execute_calibration and not arguments.execute_formal:
        try:
            payload = _dry_run_payload()
        except Exception:
            payload = {
                "mode": "DRY_RUN",
                "status": "BLOCKED",
                "reason_codes": ["OFFLINE_IDENTITY_BLOCKED"],
                "attempt_created": False,
                "network_enabled": False,
            }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload["status"] == "READY" else 2
    try:
        payload = await _execute(formal=arguments.execute_formal)
    except _Blocked as error:
        payload = {
            "mode": "EXECUTE_FORMAL" if arguments.execute_formal else "EXECUTE_CALIBRATION",
            "status": "BLOCKED",
            "reason_codes": [str(error)],
            "attempt_created": False,
            "network_enabled": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    except Exception:
        payload = {
            "mode": "EXECUTE_FORMAL" if arguments.execute_formal else "EXECUTE_CALIBRATION",
            "status": "BLOCKED",
            "reason_codes": ["V5_RUNTIME_BLOCKED"],
            "attempt_created": False,
            "network_enabled": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    # CLI 的退出码仅依赖已经脱敏的 JSON 摘要。不要引用 ``_execute`` 内的局部导入，
    # 否则真实 PASS 会在打印结果后触发 NameError 并被错误标记为命令失败。
    return 0 if payload["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
