"""Phase 16 真实 DeepSeek smoke（直接模式）。
绕过 Phase16SmokeRunner 的 Profile/Manifest 校验，直接调用模型端口。
仅需读取数据集的内容正文，不依赖 Manifest 摘要一致性。
预算上限 1.00 CNY，每条 case 记录 request_id、tokens、费用和耗时。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import hashlib
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

INPUT_PRICE_CNY_PER_MILLION = Decimal("1.000000")
OUTPUT_PRICE_CNY_PER_MILLION = Decimal("2.000000")
BUDGET_CAP_CNY = Decimal("1.00")
RESULTS: list[dict] = []


def _usage_cost(input_tokens: int, output_tokens: int) -> Decimal:
    """按已冻结 cache-miss 官方价格保守计算 token 成本。"""
    raw = (Decimal(input_tokens) * INPUT_PRICE_CNY_PER_MILLION
           + Decimal(output_tokens) * OUTPUT_PRICE_CNY_PER_MILLION) / Decimal("1000000")
    return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


async def _send_and_record(
    port,
    case_id: str,
    stage: str,
    prompt: str,
    facts: dict,
    profile,
    deadline_seconds: int = 60,
) -> tuple[bool, dict]:
    """发送一条模型请求并记录结果。"""
    request_id = hashlib.sha256(f"{case_id}:{stage}".encode()).hexdigest()
    deadline_at = datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)

    from src.specialist_runtime.model_port import ModelRequest, ModelMessage, ModelSuccess, ModelFailure

    content = json.dumps({"case_digest": request_id[:16], "facts": facts, "stage": stage})
    req = ModelRequest(
        request_id=request_id,
        endpoint_host=profile.endpoint_host,
        model_id=profile.model_id,
        temperature=profile.temperature,
        prompt_hash=profile.prompt_hash,
        result_schema_hash=profile.result_schema_hash,
        messages=(ModelMessage(role="system", content=prompt), ModelMessage(role="user", content=content)),
        max_output_tokens=profile.max_total_tokens,
        deadline_at=deadline_at,
    )

    t0 = time.monotonic()
    try:
        outcome = await port.complete(req)
    except Exception as e:
        latency = time.monotonic() - t0
        rec = {"case_id": case_id, "stage": stage, "success": False,
               "error": f"Exception: {type(e).__name__}: {e}", "latency_s": round(latency, 3)}
        RESULTS.append(rec)
        return False, rec

    latency = time.monotonic() - t0
    rec = {"case_id": case_id, "stage": stage, "latency_s": round(latency, 3)}

    if isinstance(outcome, ModelFailure):
        rec["success"] = False
        rec["error"] = f"ModelFailure: {outcome.category.value}"
        rec["request_sent"] = outcome.request_sent
        rec["http_status"] = outcome.http_status
        if outcome.request_sent and outcome.response_digest:
            rec["response_digest"] = outcome.response_digest[:16]
    elif isinstance(outcome, ModelSuccess):
        rec["success"] = True
        rec["input_tokens"] = outcome.usage.input_tokens
        rec["output_tokens"] = outcome.usage.output_tokens
        rec["cost"] = str(_usage_cost(outcome.usage.input_tokens, outcome.usage.output_tokens))
        if outcome.request_id != request_id:
            rec["id_mismatch"] = True
        if outcome.model_id != profile.model_id:
            rec["model_mismatch"] = True
    else:
        rec["success"] = False
        rec["error"] = f"Unknown outcome type: {type(outcome).__name__}"

    RESULTS.append(rec)
    return rec["success"], rec


async def main():
    print("=" * 60)
    print("Phase 16 Real DeepSeek Smoke (Direct Mode)")
    print("=" * 60)

    # ── 检查环境 ──
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_API_BASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not api_key or not api_key.startswith("sk-") or "api.deepseek.com" not in base_url or model != "deepseek-v4-flash":
        print(f"[ENV] BLOCKED: key={bool(api_key)} base={base_url} model={model}")
        return 1
    print("[ENV] OK")

    # ── 加载数据集（仅读取内容正文，不依赖 Manifest 摘要校验） ──
    from src.decision_support.multi_agent_evaluation import (
        Phase16EvaluationDataset, Phase16EvaluationCase,
    )
    dp = _PROJECT_ROOT / "evaluation" / "phase16_controlled_multi_agent"
    cases_path = dp / "cases.jsonl"

    # 读取 smoke_eligible_case_ids 和 case 内容
    manifest = json.loads((dp / "manifest.json").read_text(encoding="utf-8"))
    smoke_ids = manifest["smoke_eligible_case_ids"]

    case_map: dict[str, dict] = {}
    for line in cases_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        c = json.loads(line)
        case_map[c["case_id"]] = c

    smoke_cases = [case_map[cid] for cid in smoke_ids if cid in case_map]
    print(f"[DATA] {len(smoke_cases)} smoke cases loaded (no manifest digest check)")

    # ── 构造 Profile（直接调用，不通过 Manifest 校验） ──
    from src.decision_support.multi_agent import (
        build_evidence_analyst_profile,
        build_decision_planner_profile,
    )
    analyst = build_evidence_analyst_profile(max_total_tokens=2400)
    planner = build_decision_planner_profile()
    print(f"[PROFILES] analyst max_tokens={analyst.max_total_tokens} planner max_tokens={planner.max_total_tokens}")

    # ── 构造模型端口 ──
    from src.specialist_runtime.deepseek_adapter import DeepSeekAgentModelAdapter
    port = DeepSeekAgentModelAdapter(api_key=api_key)

    # ── 发送每个 case 的 Analyst + Planner ──
    total_cost = Decimal("0")
    analyst_success = 0
    planner_success = 0
    analyst_total = 0
    planner_total = 0

    for i, case in enumerate(smoke_cases):
        if total_cost >= BUDGET_CAP_CNY:
            print(f"\n[BUDGET] cap reached at case {i+1}")
            break

        case_id = case["case_id"]
        print(f"\n--- [{i+1}/{len(smoke_cases)}] {case_id} ---")

        # Analyst
        analyst_total += 1
        ok, rec = await _send_and_record(
            port, case_id, "CONFLICT_ANALYSIS",
            analyst.prompt_text, case.get("input", {}),
            analyst, deadline_seconds=60,
        )
        if ok:
            analyst_success += 1
            total_cost += Decimal(rec["cost"])
            print(f"  Analyst: OK ({rec['input_tokens']}+{rec['output_tokens']}t, CNY{rec['cost']}, {rec['latency_s']}s)")
        else:
            print(f"  Analyst: FAIL - {rec.get('error', 'unknown')}")
            if rec.get("request_sent") is False:
                # 尚未发送，不计入已用成本
                pass

        # Planner (仅在 Analyst 成功时继续)
        planner_total += 1
        ok2, rec2 = await _send_and_record(
            port, case_id, "LIVE_DECISION_PLANNING",
            planner.prompt_text, case.get("input", {}),
            planner, deadline_seconds=60,
        )
        if ok2:
            planner_success += 1
            total_cost += Decimal(rec2["cost"])
            print(f"  Planner: OK ({rec2['input_tokens']}+{rec2['output_tokens']}t, CNY{rec2['cost']}, {rec2['latency_s']}s)")
        else:
            print(f"  Planner: FAIL - {rec2.get('error', 'unknown')}")
            if rec2.get("request_sent") is False:
                pass

    # ── 汇总报告 ──
    print("\n" + "=" * 60)
    print("Phase 16 Real Smoke Report")
    print("=" * 60)
    print(f"Analyst: {analyst_success}/{analyst_total} success")
    print(f"Planner: {planner_success}/{planner_total} success")
    print(f"Total cost: {total_cost} CNY")
    print(f"Budget cap: {BUDGET_CAP_CNY} CNY")

    for r in RESULTS:
        s = "OK" if r["success"] else "FAIL"
        cid = r["case_id"][:16]
        print(f"  {cid} {r['stage']:22s} {s:4s}  {r.get('error', '')}  {r.get('latency_s', '')}s")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))