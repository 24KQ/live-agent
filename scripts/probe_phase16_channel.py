"""Phase 16 渠道探针:单独验证指定渠道的连通性、稳定性与内容质量。

三层从小到大:
- L1 连通性:3 次最小请求(max_output_tokens=1),验证 HTTP 200 / API key 认证 / 延迟。
- L2 稳定性:10 次固定 schema 化请求(analyst 风格),校验响应 JSON 结构,
  每个请求独立 60s deadline —— 超过即记为挂死嫌疑(实测 imagebridge 曾挂 180s)。
- L3 语义:2 个冻结 qualification corpus 真实 case(analyst + planner 链),
  使用正式 candidate profile 的 prompt_text 与 result_schema 校验。

设计约束:
- 真实调用,预算约 0.3 CNY/渠道;key 只从 .env 读取,输出打码,绝不打印 key。
- 不写执行账本、不触碰 campaign digest —— 这是渠道体检,不是正式 campaign。
- 思考强度必须为白名单内值(与正式 campaign 同参数,否则内容质量结论失真)。
- 对照组:同一脚本 --channel synapse 重跑,数据直接可比。

用法:
    python -u scripts/probe_phase16_channel.py --channel api.imagebridge.top --levels 1
    python -u scripts/probe_phase16_channel.py --channel api.imagebridge.top --levels 1,2,3
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

sys.path.insert(0, str(_PROJECT_ROOT))

from src.decision_support.controlled_e2e_adapter_v5 import (  # noqa: E402
    DeepSeekV5ControlledE2EAdapter,
)
from src.decision_support.phase16_qualification import (  # noqa: E402
    PHASE16_QUALIFICATION_ASSET_DIRECTORY,
    load_phase16_qualification_corpus,
    load_phase16_qualification_policy,
)
from src.decision_support.phase16_qualification_evaluator import (  # noqa: E402
    build_phase16_qualification_candidate_bundle,
)
from src.specialist_runtime.model_port import (  # noqa: E402
    ModelMessage,
    ModelRequest,
    ModelSuccess,
)
from src.specialist_runtime.profiles import (  # noqa: E402
    FORMAL_REASONING_EFFORTS,
    normalize_endpoint_host,
)

#: 每个请求的独立绝对 deadline;超过即记挂死嫌疑(正式 campaign 挂死过 180s)。
_PROBE_DEADLINE_SECONDS = 60.0

#: L3 使用正式 profile prompt(大 context + xhigh 推理),正式 campaign 实测单次
#: 调用 56-73s;60s 探针 deadline 会误杀正常调用,故 L3 放宽到 120s。
_L3_DEADLINE_SECONDS = 120.0

#: L2 固定请求数。
_L2_COUNT = 10

#: L3 使用的真实 case 数量。
_L3_CASE_COUNT = 2


def _channel_key(host: str) -> str:
    """从 .env 渠道有序列表取指定 host 的 API key;不打码不打印。"""

    hosts = [
        normalize_endpoint_host(h.strip())
        for h in os.environ.get("LLM_API_CHANNEL_HOSTS", "").split(",")
        if h.strip()
    ]
    keys = [
        k.strip()
        for k in os.environ.get("LLM_API_CHANNEL_KEYS", "").split(",")
        if k.strip()
    ]
    if len(hosts) != len(keys):
        raise SystemExit(
            "LLM_API_CHANNEL_HOSTS / LLM_API_CHANNEL_KEYS length mismatch"
        )
    try:
        return keys[hosts.index(normalize_endpoint_host(host))]
    except ValueError as exc:
        raise SystemExit(f"host {host} not present in LLM_API_CHANNEL_HOSTS") from exc


def _assert_probe_config() -> str:
    """断言探针与正式 campaign 同参数;model / effort 缺失即退出。"""

    model = os.environ.get("LLM_API_MODEL_ID", "").strip()
    effort = os.environ.get("LLM_API_REASONING_EFFORT", "").strip()
    if not model:
        raise SystemExit("LLM_API_MODEL_ID is not set in .env")
    if not effort:
        raise SystemExit("LLM_API_REASONING_EFFORT is not set in .env")
    if effort not in FORMAL_REASONING_EFFORTS:
        # 探针必须与正式 campaign 同思考强度(白名单内),否则内容质量结论失真。
        raise SystemExit(
            f"LLM_API_REASONING_EFFORT must be one of "
            f"{sorted(FORMAL_REASONING_EFFORTS)}, got {effort!r}"
        )
    return model


def _make_request(
    *,
    request_id: str,
    host: str,
    model_id: str,
    system: str,
    user: str,
    max_output_tokens: int,
    now: datetime,
) -> ModelRequest:
    messages = (
        ModelMessage(role="system", content=system),
        ModelMessage(role="user", content=user),
    )
    prompt_hash = sha256(
        json.dumps(
            [m.model_dump(mode="json") for m in messages],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return ModelRequest(
        request_id=request_id,
        endpoint_host=normalize_endpoint_host(host),
        model_id=model_id,
        temperature=0,
        prompt_hash=prompt_hash,
        result_schema_hash=sha256(b"probe").hexdigest(),
        messages=messages,
        max_output_tokens=max_output_tokens,
        deadline_at=now + timedelta(seconds=_PROBE_DEADLINE_SECONDS),
    )


def _classify(outcome) -> tuple[str, float | None, int | None]:
    """把 outcome 归为 (label, latency_ms, status_code) 用于统计。"""

    if isinstance(outcome, ModelSuccess):
        return "SUCCESS", float(outcome.latency_ms), 200
    category = outcome.category.value
    latency = getattr(outcome, "latency_ms", None)
    status = getattr(outcome, "http_status", None)
    return category, float(latency) if latency is not None else None, status


async def _run_l1(adapter, *, host: str, model_id: str) -> list[dict]:
    """L1 连通性:3 次最小请求。"""

    print(f"[L1] connectivity ping x3 (host={host}, model={model_id})")
    results = []
    for i in range(3):
        request = _make_request(
            request_id=f"probe-l1-{host}-{i}",
            host=host,
            model_id=model_id,
            # adapter 固定使用 response_format=json_object;OpenAI 兼容规范要求
            # prompt 必须含 "json" 字样,否则端点返回 400。
            system="You are a connectivity probe that replies in JSON.",
            user="Return the JSON object: {\"reply\": \"pong\"}",
            max_output_tokens=32,
            now=datetime.now(timezone.utc),
        )
        outcome = await adapter.complete(request)
        label, latency, status = _classify(outcome)
        results.append({"label": label, "latency_ms": latency, "http_status": status})
        print(f"  L1-{i + 1}: {label} latency={latency}ms status={status}")
    return results


_L2_SCHEMA = {
    "type": "object",
    "required": ["options"],
    "properties": {
        "options": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "required": ["option_id", "title", "risk_level"],
                "properties": {
                    "option_id": {"type": "string"},
                    "title": {"type": "string"},
                    "risk_level": {"type": "string"},
                },
            },
        }
    },
}

_L2_SYSTEM = (
    "You are a structured-decision analyst. Given a conflict scenario, reply with "
    "ONLY a JSON object following this exact schema: "
    '{"options": [{"option_id": string, "title": string, "risk_level": string}]}'
    " with at least two options. No prose outside the JSON."
)

_L2_USER = (
    "A sold-out live event: fan at gate reports seat double-sold; the crowd is "
    "growing impatient and the manager requests options. Return the JSON."
)


def _to_plain(obj):
    """把 FrozenDict 嵌套结构递归归一为纯 dict/list(JSON 往返等价)。"""

    from collections.abc import Mapping

    if isinstance(obj, Mapping):
        return {key: _to_plain(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(value) for value in obj]
    return obj


def _unwrap_final_envelope(parsed: dict) -> dict:
    """正式管线要求模型输出 FINAL envelope:{"kind":"FINAL","final_output":<RESULT>}
    (multi_agent.py:333, controlled_e2e_v5.py:237);evaluator 只校验 final_output 内部。
    探针对齐正式解壳语义,否则带壳输出会被误判为 schema 违规。
    """

    if parsed.get("kind") == "FINAL" and "final_output" in parsed:
        return parsed["final_output"]
    return parsed


def _validate_json_structure(payload: str | dict, schema: dict) -> tuple[bool, str]:
    """校验模型输出是否为符合 schema 的 JSON;用于 L2 与 L3。

    V5 adapter 返回的 ``ModelSuccess.output`` 已是解析后的 FrozenDict —— 它不是
    ``dict`` 的子类但实现 Mapping 协议,因此用 ``Mapping`` 判断而非 ``isinstance(dict)``。
    """

    import jsonschema
    from collections.abc import Mapping

    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            return False, f"invalid JSON: {exc}"
    else:
        parsed = payload
    if not isinstance(parsed, Mapping):
        return False, "JSON is not an object"
    parsed = _unwrap_final_envelope(dict(parsed))
    try:
        # profile 的 result_schema 是 FrozenDict,jsonschema 要求纯 dict —— 归一后再校验。
        jsonschema.validate(_to_plain(parsed), _to_plain(schema))
    except jsonschema.ValidationError as exc:
        return False, f"schema violation: {exc.message}"
    return True, "ok"


async def _run_l2(adapter, *, host: str, model_id: str) -> list[dict]:
    """L2 稳定性:10 次固定 schema 化请求 + JSON 结构校验。"""

    print(f"[L2] fixed schema requests x{_L2_COUNT} (host={host}, model={model_id})")
    results = []
    for i in range(_L2_COUNT):
        request = _make_request(
            request_id=f"probe-l2-{host}-{i}",
            host=host,
            model_id=model_id,
            system=_L2_SYSTEM,
            user=_L2_USER,
            max_output_tokens=1024,
            now=datetime.now(timezone.utc),
        )
        started = datetime.now(timezone.utc)
        outcome = await adapter.complete(request)
        wall_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        label, latency, status = _classify(outcome)
        entry = {
            "label": label,
            "latency_ms": latency,
            "wall_ms": wall_ms,
            "http_status": status,
        }
        if isinstance(outcome, ModelSuccess):
            ok, why = _validate_json_structure(outcome.output, _L2_SCHEMA)
            entry["schema_ok"] = ok
            entry["schema_error"] = None if ok else why
            entry["output_tokens"] = outcome.usage.output_tokens
        results.append(entry)
        print(
            f"  L2-{i + 1}: {label} latency={latency}ms wall={wall_ms}ms "
            f"schema={entry.get('schema_ok', 'n/a')} tokens={entry.get('output_tokens', 'n/a')}"
        )
    return results


async def _run_l3(adapter, *, host: str, model_id: str) -> list[dict]:
    """L3 语义:冻结 corpus 真实 case + 正式 profile prompt/schema 校验。"""

    print(f"[L3] frozen corpus cases x{_L3_CASE_COUNT} (host={host}, model={model_id})")
    policy = load_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    corpus = load_phase16_qualification_corpus(
        _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        repository_root=_PROJECT_ROOT,
        policy=policy,
    )
    bundle = build_phase16_qualification_candidate_bundle(
        policy=policy, repository_root=_PROJECT_ROOT
    )
    analyst_profile = bundle.analyst_profile
    planner_profile = bundle.planner_profile

    cases = [c for c in corpus.development_cases if c.split.value == "development"][
        : _L3_CASE_COUNT
    ]
    results = []

    for idx, case in enumerate(cases, start=1):
        # ANALYST 阶段:正式 profile prompt + 真实 case 输入。
        analyst_user = json.dumps(
            {"case_id": case.case_id, "input": case.input},
            ensure_ascii=False,
            sort_keys=True,
        )
        analyst_request = ModelRequest(
            request_id=f"probe-l3-{host}-{case.case_id}-analyst",
            endpoint_host=normalize_endpoint_host(host),
            model_id=model_id,
            temperature=0,
            prompt_hash=analyst_profile.prompt_hash,
            result_schema_hash=analyst_profile.result_schema_hash,
            messages=(
                ModelMessage(role="system", content=analyst_profile.prompt_text),
                ModelMessage(role="user", content=analyst_user),
            ),
            max_output_tokens=analyst_profile.max_output_tokens or 2048,
            deadline_at=datetime.now(timezone.utc)
            + timedelta(seconds=_L3_DEADLINE_SECONDS),
        )
        started = datetime.now(timezone.utc)
        outcome = await adapter.complete(analyst_request)
        wall_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        label, latency, status = _classify(outcome)
        analyst_entry = {
            "case_id": case.case_id,
            "stage": "ANALYST",
            "label": label,
            "latency_ms": latency,
            "wall_ms": wall_ms,
            "http_status": status,
        }
        if isinstance(outcome, ModelSuccess):
            ok, why = _validate_json_structure(outcome.output, analyst_profile.result_schema)
            analyst_entry["schema_ok"] = ok
            analyst_entry["schema_error"] = None if ok else why
            analyst_entry["output_tokens"] = outcome.usage.output_tokens
        results.append(analyst_entry)
        print(
            f"  L3-{idx} ANALYST {case.case_id}: {label} latency={latency}ms "
            f"schema={analyst_entry.get('schema_ok', 'n/a')}"
        )
        if not isinstance(outcome, ModelSuccess) or not analyst_entry.get("schema_ok"):
            continue

        # PLANNER 阶段:analyst 真实输出(解壳后)作为输入,对齐正式管线语义。
        # 注意 output 是 FrozenDict(实现 Mapping,不是 dict 子类),解壳后
        # 仍有嵌套 FrozenDict,json.dumps 前必须归一为纯 dict。
        from collections.abc import Mapping

        analysis = (
            _to_plain(_unwrap_final_envelope(dict(outcome.output)))
            if isinstance(outcome.output, Mapping)
            else json.loads(outcome.output)
        )
        planner_user = json.dumps(
            {
                "analysis": analysis,
                "case_id": case.case_id,
                "input": case.input,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        planner_request = ModelRequest(
            request_id=f"probe-l3-{host}-{case.case_id}-planner",
            endpoint_host=normalize_endpoint_host(host),
            model_id=model_id,
            temperature=0,
            prompt_hash=planner_profile.prompt_hash,
            result_schema_hash=planner_profile.result_schema_hash,
            messages=(
                ModelMessage(role="system", content=planner_profile.prompt_text),
                ModelMessage(role="user", content=planner_user),
            ),
            max_output_tokens=planner_profile.max_output_tokens or 2048,
            deadline_at=datetime.now(timezone.utc)
            + timedelta(seconds=_L3_DEADLINE_SECONDS),
        )
        started = datetime.now(timezone.utc)
        outcome = await adapter.complete(planner_request)
        wall_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        label, latency, status = _classify(outcome)
        planner_entry = {
            "case_id": case.case_id,
            "stage": "PLANNER",
            "label": label,
            "latency_ms": latency,
            "wall_ms": wall_ms,
            "http_status": status,
        }
        if isinstance(outcome, ModelSuccess):
            ok, why = _validate_json_structure(outcome.output, planner_profile.result_schema)
            planner_entry["schema_ok"] = ok
            planner_entry["schema_error"] = None if ok else why
            planner_entry["output_tokens"] = outcome.usage.output_tokens
        results.append(planner_entry)
        print(
            f"  L3-{idx} PLANNER {case.case_id}: {label} latency={latency}ms "
            f"schema={planner_entry.get('schema_ok', 'n/a')}"
        )
    return results


async def _probe(*, host: str, levels: set[int]) -> None:
    model_id = _assert_probe_config()
    api_key = _channel_key(host)
    # 单端点探针:只测目标渠道,不配置 failover。
    adapter = DeepSeekV5ControlledE2EAdapter(endpoints=((host, api_key),))
    host = normalize_endpoint_host(host)
    print(f"probe host={host} model={model_id} effort={os.environ.get('LLM_API_REASONING_EFFORT')}")
    all_results: dict[str, list[dict]] = {}
    if 1 in levels:
        all_results["L1"] = await _run_l1(adapter, host=host, model_id=model_id)
    if 2 in levels:
        all_results["L2"] = await _run_l2(adapter, host=host, model_id=model_id)
    if 3 in levels:
        all_results["L3"] = await _run_l3(adapter, host=host, model_id=model_id)

    print("\n=== summary ===")
    for level, items in all_results.items():
        labels = [item["label"] for item in items]
        ok = labels.count("SUCCESS")
        hangs = sum(1 for i in items if i.get("wall_ms", 0) >= _PROBE_DEADLINE_SECONDS * 1000)
        schema_ok = sum(1 for i in items if i.get("schema_ok") is True)
        latency = [i.get("latency_ms") for i in items if i.get("latency_ms") is not None]
        stats = {
            "attempts": len(items),
            "success": ok,
            "labels": {l: labels.count(l) for l in sorted(set(labels))},
            "hang_candidates": hangs,
            "schema_ok": schema_ok,
            "latency_ms_min": min(latency) if latency else None,
            "latency_ms_max": max(latency) if latency else None,
            "latency_ms_avg": round(sum(latency) / len(latency)) if latency else None,
        }
        print(f"  {level}: {stats}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    report_path = _PROJECT_ROOT / "docs" / "superpowers" / "reports" / (
        f"probe-channel-{host}-{stamp}.json"
    )
    # Windows 下 Path.write_text 默认把 \\n 转成 \\r\\n;项目约束为 UTF-8 LF 无 BOM。
    with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {
                "host": host,
                "model_id": model_id,
                "reasoning_effort": os.environ.get("LLM_API_REASONING_EFFORT"),
                "deadline_seconds": _PROBE_DEADLINE_SECONDS,
                "results": all_results,
            },
            fh,
            indent=2,
            ensure_ascii=False,
        )
    print(f"report: {report_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 16 channel probe")
    parser.add_argument("--channel", default="api.imagebridge.top", help="host to probe")
    parser.add_argument(
        "--levels", default="1,2,3", help="comma-separated levels, e.g. 1 or 1,2,3"
    )
    args = parser.parse_args()
    levels = {int(v.strip()) for v in args.levels.split(",") if v.strip()}
    asyncio.run(_probe(host=args.channel, levels=levels))


if __name__ == "__main__":
    main()
