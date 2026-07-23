"""Phase 16 正式真实模型 smoke 的唯一命令入口。

默认调用只做离线 Manifest/价格/Profile 预检，不读取 ``.env``、不创建 DeepSeek 适配器、
不连接 PostgreSQL，更不会发送网络请求。只有显式 ``--execute`` 才能装配受控 Runner，
并且仍需通过正式账本、二十次调用、回执和结构校验链才能产生可审计结论。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any, Sequence
from urllib.parse import urlparse


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    # 脚本可以从仓库根或任意工作目录启动；只补充本仓库根，不搜索用户目录或插件路径。
    sys.path.insert(0, str(_PROJECT_ROOT))

_RECEIPT_HMAC_ENV = "PHASE16_OFFICIAL_SMOKE_RECEIPT_HMAC_HEX"


class _CliBlocked(RuntimeError):
    """命令入口的非敏感阻断码；消息永不包含 API Key、Prompt 或模型响应正文。"""


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析最小且封闭的模式集合，历史直连开关仅用于给出硬失败说明。"""

    parser = argparse.ArgumentParser(
        description="Run the Phase 16 formal DeepSeek smoke through the audited ledger."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="after offline preflight, allow the single formal networked smoke run",
    )
    parser.add_argument(
        "--legacy-direct-mode",
        "--direct-mode",
        action="store_true",
        dest="legacy_direct_mode",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def _official_price() -> Any:
    """构造 D-168 冻结的公开 cache-miss 价格快照，不读取本机环境或网页。"""

    from decimal import Decimal

    from src.decision_support.official_smoke_evidence import Phase16OfficialPriceEvidence

    return Phase16OfficialPriceEvidence.create(
        model_id="deepseek-v4-flash",
        endpoint_host="api.deepseek.com",
        input_cny_per_million=Decimal("1.000000"),
        output_cny_per_million=Decimal("2.000000"),
    )


def _build_formal_runner(*, credential_configured: bool, endpoint_host: str, model_id: str, ledger: Any, model_port: Any):
    """从冻结资产装配正式 Runner，不生成 case、不注入生产 LIVE 路由或直接请求模型。"""

    from src.decision_support.multi_agent_evaluation import (
        load_phase16_controlled_multi_agent_dataset,
    )
    from src.decision_support.official_smoke_evidence import (
        Phase16OfficialSmokeEnvironment,
        load_phase16_official_smoke_evidence_manifest,
        preflight_phase16_official_smoke_evidence,
    )
    from src.decision_support.official_smoke_runner import Phase16OfficialSmokeRunner

    dataset = load_phase16_controlled_multi_agent_dataset(
        _PROJECT_ROOT / "evaluation" / "phase16_controlled_multi_agent"
    )
    official_price = _official_price()
    manifest = load_phase16_official_smoke_evidence_manifest(repository_root=_PROJECT_ROOT)
    preflight = preflight_phase16_official_smoke_evidence(
        dataset=dataset,
        official_price=official_price,
        environment=Phase16OfficialSmokeEnvironment(
            model_id=model_id,
            endpoint_host=endpoint_host,
            credential_configured=credential_configured,
        ),
    )
    return Phase16OfficialSmokeRunner(
        dataset=dataset,
        manifest=manifest,
        preflight=preflight,
        official_price=official_price,
        ledger=ledger,
        model_port=model_port,
    )


def _configured_endpoint_host(base_url: str) -> str:
    """把本机 OpenAI 格式 BASE URL 收敛为严格 HTTPS host，拒绝路径、端口和用户信息漂移。"""

    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise _CliBlocked("ENDPOINT_CONFIG_INVALID")
    return parsed.hostname.lower()


def _load_receipt_signing_key() -> bytes:
    """读取只供本进程 HMAC 的 256 位以上十六进制密钥，绝不输出、持久化或写入报告。"""

    raw = os.environ.get(_RECEIPT_HMAC_ENV, "").strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError as error:
        raise _CliBlocked("RECEIPT_HMAC_CONFIG_INVALID") from error
    if len(key) < 32:
        raise _CliBlocked("RECEIPT_HMAC_CONFIG_INVALID")
    return key


def _build_execute_runner():
    """只在显式 execute 分支加载凭据并创建适配器/账本，禁止默认 dry-run 触碰这些依赖。"""

    from dotenv import load_dotenv

    # `.env` 只在此时加载。默认入口不读取它，因此普通查看/CI dry-run 不会把任何本机
    # API Key 带入进程内存。
    load_dotenv(_PROJECT_ROOT / ".env")
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    model_id = os.environ.get("LLM_MODEL", "").strip()
    base_url = os.environ.get("LLM_API_BASE_URL", "").strip()
    if not api_key or api_key == "change_me":
        raise _CliBlocked("CREDENTIAL_UNAVAILABLE")
    endpoint_host = _configured_endpoint_host(base_url)
    signing_key = _load_receipt_signing_key()

    from src.config.settings import get_settings
    from src.decision_support.official_smoke_ledger import (
        Phase16OfficialSmokeReceiptAuthenticator,
        PostgresPhase16OfficialSmokeLedger,
    )
    from src.specialist_runtime.deepseek_adapter import DeepSeekAgentModelAdapter

    # 构造对象本身不发送网络；真实请求只能在 Runner 完成所有预检、预算预约和 dispatch
    # append 之后由共享 BoundedSpecialistRunner 触发。
    return _build_formal_runner(
        credential_configured=True,
        endpoint_host=endpoint_host,
        model_id=model_id,
        ledger=PostgresPhase16OfficialSmokeLedger(
            get_settings(),
            receipt_authenticator=Phase16OfficialSmokeReceiptAuthenticator(signing_key),
        ),
        model_port=DeepSeekAgentModelAdapter(api_key=api_key),
    )


def _report_payload(*, mode: str, report: Any) -> dict[str, Any]:
    """把执行结果缩减为白名单摘要；不得打印 Prompt、原始回执、响应正文或运营建议。"""

    value = lambda item: item.value if hasattr(item, "value") else str(item)
    return {
        "mode": mode,
        "run_id": report.run_id,
        "status": value(report.status),
        "evidence_conclusion": value(report.evidence_conclusion),
        "reason_codes": list(report.reason_codes),
        "case_count": len(report.case_executions),
        "model_calls": report.model_calls,
    }


async def main(argv: Sequence[str] | None = None) -> int:
    """执行默认 dry-run 或唯一正式 execute 分支，并以稳定退出码向自动化流程说明结论。"""

    arguments = _parse_arguments(argv)
    if arguments.legacy_direct_mode:
        # 旧直接模式没有合法迁移路径：它绕过冻结 Manifest、共享 Runner 与 append-only
        # ledger，故即使用户显式传参也必须在创建任何外部依赖前硬失败。
        raise SystemExit("LEGACY_DIRECT_MODE_DISABLED: use the audited default dry-run or --execute")

    if not arguments.execute:
        try:
            # dry-run 使用已知正式 model/host 和 false credential flag，只验证离线资产与
            # 发送前契约；它绝不读取环境，因此会诚实报告 CREDENTIAL_UNAVAILABLE。
            runner = _build_formal_runner(
                credential_configured=False,
                endpoint_host="api.deepseek.com",
                model_id="deepseek-v4-flash",
                ledger=None,
                model_port=None,
            )
            report = runner.dry_run()
            payload = _report_payload(mode="DRY_RUN", report=report)
        except Exception:
            payload = {
                "mode": "DRY_RUN",
                "status": "BLOCKED",
                "evidence_conclusion": "INCONCLUSIVE",
                "reason_codes": ["OFFLINE_PREFLIGHT_BUILD_FAILED"],
                "case_count": 0,
                "model_calls": 0,
            }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        # 默认 dry-run 是只读诊断命令；即使发现发送条件未齐，也不把“尚未授权联网”视为
        # shell 错误，方便开发者安全检查当前资产而不触发 CI 误报警。
        return 0

    try:
        runner = _build_execute_runner()
    except _CliBlocked as error:
        print(
            json.dumps(
                {
                    "mode": "EXECUTE",
                    "status": "BLOCKED",
                    "evidence_conclusion": "INCONCLUSIVE",
                    "reason_codes": [str(error)],
                    "case_count": 0,
                    "model_calls": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    report = await runner.execute()
    print(json.dumps(_report_payload(mode="EXECUTE", report=report), ensure_ascii=False, sort_keys=True))
    status = report.status.value if hasattr(report.status, "value") else str(report.status)
    return 0 if status == "PASS" else 2 if status == "BLOCKED" else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
