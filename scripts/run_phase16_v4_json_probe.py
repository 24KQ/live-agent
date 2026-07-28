"""执行一次 Phase 16 V4 禁思考 JSON 协议探针。

默认 dry-run 只校验冻结协议，不读取密钥、不连接 PostgreSQL、更不会联网。只有显式
``--execute`` 才会创建唯一账本 intent 并发送一条无业务数据的 DeepSeek 请求。
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
    # 从任意工作目录运行时只导入当前仓库，不搜索用户目录或外部脚本目录。
    sys.path.insert(0, str(_PROJECT_ROOT))

_HMAC_ENV = "PHASE16_OFFICIAL_SMOKE_V2_RECEIPT_HMAC_HEX"


class _Blocked(RuntimeError):
    """命令入口的稳定脱敏阻断码，不能携带密钥、URL 原文或数据库异常文本。"""


def _arguments() -> argparse.Namespace:
    """解析唯一联网开关；探针没有重试、模型切换或自定义 Prompt 参数。"""

    parser = argparse.ArgumentParser(description="Run one audited Phase 16 V4 JSON probe.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="after dry-run checks, allow the one frozen DeepSeek protocol request",
    )
    return parser.parse_args()


def _endpoint_host(value: str) -> str:
    """仅接受冻结的 HTTPS DeepSeek base URL，拒绝端口、路径、用户信息与重定向入口。"""

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
    """读取长度至少 256 位的账本签名密钥，任何格式错误都只暴露稳定阻断码。"""

    try:
        value = bytes.fromhex(os.environ.get(_HMAC_ENV, "").strip())
    except ValueError as error:
        raise _Blocked("DIAGNOSTIC_HMAC_UNAVAILABLE") from error
    if len(value) < 32:
        raise _Blocked("DIAGNOSTIC_HMAC_UNAVAILABLE")
    return value


def _offline_protocol() -> dict[str, object]:
    """构造并输出不含 Prompt 正文的冻结身份，供 dry-run 与真实执行共用。"""

    from src.decision_support.json_probe_v4 import (
        PHASE16_V4_JSON_PROBE_RUN_ID,
        Phase16V4JsonProbeProtocol,
    )

    protocol = Phase16V4JsonProbeProtocol.create()
    return {
        "mode": "DRY_RUN",
        "run_id": PHASE16_V4_JSON_PROBE_RUN_ID,
        "status": "READY",
        "reason_code": "OFFLINE_IDENTITY_READY",
        "protocol_digest": protocol.protocol_digest,
        "thinking_mode": protocol.thinking_mode.value,
        "attempt_created": False,
    }


async def _execute() -> dict[str, object]:
    """在所有本地配置校验后才装配 Adapter 和账本，构造对象本身不产生网络请求。"""

    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_API_BASE_URL", "").strip()
    if not api_key or api_key == "change_me":
        raise _Blocked("CREDENTIAL_UNAVAILABLE")
    _endpoint_host(base_url)
    signing_key = _hmac_key()

    from src.config.settings import get_settings
    from src.decision_support.json_probe_v4 import (
        PHASE16_V4_JSON_PROBE_RUN_ID,
        Phase16V4JsonProbeRunner,
        PostgresPhase16V4JsonProbeLedger,
    )
    from src.decision_support.v4_json_probe_adapter import DeepSeekV4JsonProbeAdapter

    report = await Phase16V4JsonProbeRunner(
        ledger=PostgresPhase16V4JsonProbeLedger(get_settings(), hmac_key=signing_key),
        # 禁思考属于本次单独探针的 Adapter 实例配置，不写回共享 Profile/Request，避免
        # 让 V1/V2/V3 已冻结的模型契约和源码闭包发生漂移。
        model_port=DeepSeekV4JsonProbeAdapter(api_key=api_key),
    ).execute()
    return {
        "mode": "EXECUTE",
        "run_id": PHASE16_V4_JSON_PROBE_RUN_ID,
        "status": report.status.value,
        "reason_code": report.reason_code,
        "attempt_created": report.attempt_id is not None,
        "parse_stage": None if report.parse_stage is None else report.parse_stage.value,
        "content_shape": (
            None if report.content_shape is None else report.content_shape.value
        ),
        "finish_reason": (
            None if report.finish_reason is None else report.finish_reason.value
        ),
        "reasoning_content_present": report.reasoning_content_present,
    }


async def main() -> int:
    """只打印白名单 JSON 摘要；任一执行失败都不能把异常或模型正文打印到终端。"""

    arguments = _arguments()
    if not arguments.execute:
        try:
            payload = _offline_protocol()
        except Exception:
            payload = {
                "mode": "DRY_RUN",
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
            "status": "BLOCKED",
            "reason_code": str(error),
            "attempt_created": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    except Exception:
        # 运行时装配、DDL 或账本异常不能伪造成 Provider 结果；真实模型失败会由账本中的
        # ModelFailure 单独表达并返回稳定原因，异常正文不进入命令输出。
        payload = {
            "mode": "EXECUTE",
            "status": "BLOCKED",
            "reason_code": "JSON_PROBE_RUNTIME_BLOCKED",
            "attempt_created": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
