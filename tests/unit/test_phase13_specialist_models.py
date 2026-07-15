"""Phase 13 Task 1 Specialist 协议、Profile Registry 与确定性路由测试。"""

from __future__ import annotations

from decimal import Decimal
import hashlib

import pytest
from pydantic import ValidationError

from src.specialist_runtime.models import (
    AgentAction,
    AgentActionKind,
    AgentFailure,
    AgentResult,
    AgentResultStatus,
    AgentTask,
    EvidenceKind,
    EvidenceRef,
    SpecialistTaskKind,
    canonical_json_sha256,
)
from src.specialist_runtime.profiles import SpecialistProfile
from src.specialist_runtime.registry import (
    SpecialistOrchestrator,
    SpecialistProfileConflictError,
    SpecialistProfileRegistry,
    SpecialistProfileResolutionError,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
PROMPT_TEXT = "Return one governed action from resolved evidence."


def _evidence() -> EvidenceRef:
    """返回可被多个任务复用的固定事件证据引用。"""

    return EvidenceRef(
        kind=EvidenceKind.EVENT,
        evidence_id="event-001",
        source_version="2.0.0",
        digest=HASH_A,
        room_id="room-001",
    )


def _task(
    *,
    task_kind: SpecialistTaskKind = SpecialistTaskKind.LIVE_OPS_ADVICE,
    profile_id: str = "live-ops",
    profile_version: str = "1.0.0",
    input_snapshot: dict | None = None,
) -> AgentTask:
    """构造严格冻结的最小任务，避免测试共享可变输入。"""

    return AgentTask(
        task_id="task-001",
        task_kind=task_kind,
        profile_id=profile_id,
        profile_version=profile_version,
        room_id="room-001",
        trace_id="trace-001",
        objective="生成安全的播中建议",
        input_snapshot=(
            input_snapshot
            if input_snapshot is not None
            else {"alerts": [{"type": "SOLD_OUT", "product_id": "p001"}]}
        ),
        initial_evidence_refs=(_evidence(),),
        evaluation_case_id="live-ops-development-001",
    )


def _profile(
    *,
    profile_id: str = "live-ops",
    profile_version: str = "1.0.0",
    task_kind: SpecialistTaskKind = SpecialistTaskKind.LIVE_OPS_ADVICE,
    prompt_text: str = PROMPT_TEXT,
) -> SpecialistProfile:
    """构造启动冻结 Profile，所有执行限制都来自 Profile 而非 AgentTask。"""

    result_schema = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
        "additionalProperties": False,
    }
    return SpecialistProfile(
        profile_id=profile_id,
        profile_version=profile_version,
        task_kind=task_kind,
        model_id="deepseek-v4-flash",
        endpoint_host="api.deepseek.com",
        temperature=Decimal("0"),
        prompt_text=prompt_text,
        prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        result_schema_hash=canonical_json_sha256(result_schema),
        result_schema=result_schema,
        allowed_skill_ids=("generate_on_live_prompt",),
        skill_versions={"generate_on_live_prompt": "1.0.0"},
        max_model_calls=2,
        max_skill_calls=3,
        max_total_tokens=4000,
        deadline_seconds=5,
        max_case_cost_cny=Decimal("0.01"),
    )


def test_agent_task_is_strict_deeply_frozen_and_digest_stable() -> None:
    """任务的嵌套 JSON 与摘要都不能被调用方在构造后改写。"""

    payload = {"alerts": [{"type": "SOLD_OUT", "product_id": "p001"}]}
    task = _task(input_snapshot=payload)
    same = AgentTask.model_validate(task.model_dump(mode="json"))

    payload["alerts"][0]["product_id"] = "tampered"
    with pytest.raises(TypeError):
        task.input_snapshot["alerts"][0]["product_id"] = "tampered"
    assert task.input_snapshot["alerts"][0]["product_id"] == "p001"
    assert task.task_digest == same.task_digest
    with pytest.raises(ValidationError):
        AgentTask.model_validate({**task.model_dump(mode="json"), "unknown": True})


def test_frozen_protocol_cannot_be_rebound_through_copy_or_dict_base_method() -> None:
    """公共复制 API 与 dict 基类方法都不能生成摘要未更新的可变事实。"""

    task = _task()
    profile = _profile()

    with pytest.raises(TypeError, match="update"):
        task.model_copy(update={"input_snapshot": {"tampered": True}})
    with pytest.raises(TypeError):
        dict.__setitem__(task.input_snapshot, "tampered", True)
    with pytest.raises(TypeError, match="update"):
        profile.model_copy(update={"result_schema": {"type": "integer"}})

    assert task.model_copy(deep=True) is task
    assert profile.model_copy(deep=True) is profile


def test_profile_binds_prompt_content_and_exact_skill_versions() -> None:
    """Profile 身份必须同时冻结真实 Prompt 正文与每个白名单 Skill 的精确版本。"""

    profile = _profile()
    assert profile.prompt_text == PROMPT_TEXT
    assert profile.skill_versions == {"generate_on_live_prompt": "1.0.0"}

    prompt_tampered = profile.model_dump(mode="json")
    prompt_tampered["prompt_text"] = "tampered prompt"
    prompt_tampered.pop("profile_digest")
    with pytest.raises(ValidationError, match="prompt_hash"):
        SpecialistProfile.model_validate(prompt_tampered)

    version_missing = profile.model_dump(mode="json")
    version_missing["skill_versions"] = {}
    version_missing.pop("profile_digest")
    with pytest.raises(ValidationError, match="skill_versions"):
        SpecialistProfile.model_validate(version_missing)


@pytest.mark.parametrize(
    ("action", "error"),
    [
        (
            {
                "kind": "CALL_SKILL",
                "arguments": {},
            },
            "skill_id",
        ),
        (
            {
                "kind": "FINAL",
                "skill_id": "generate_on_live_prompt",
                "arguments": {},
                "final_output": {"action": "NO_ACTION"},
            },
            "FINAL",
        ),
        (
            {
                "kind": "ABSTAIN",
                "reason_code": "",
            },
            "reason_code",
        ),
    ],
)
def test_agent_action_rejects_non_exclusive_shapes(action: dict, error: str) -> None:
    """三种动作只能携带各自允许的字段，防止模型夹带执行意图。"""

    with pytest.raises(ValidationError, match=error):
        AgentAction.model_validate(action)


def test_agent_action_and_result_accept_closed_valid_shapes() -> None:
    """合法 Skill 动作与最终结果保留结构化摘要和证据，不保存思维链。"""

    action = AgentAction(
        kind=AgentActionKind.CALL_SKILL,
        skill_id="generate_on_live_prompt",
        arguments={"product_id": "p001"},
        evidence_refs=(_evidence(),),
        reason_summary="需要生成主播提示",
    )
    result = AgentResult(
        task_id="task-001",
        profile_id="live-ops",
        profile_version="1.0.0",
        status=AgentResultStatus.SUCCEEDED,
        output={"action": "HUMAN_ATTENTION"},
        actions=(action,),
        evidence_refs=(_evidence(),),
        summary="已生成安全提示",
        model_calls=1,
        skill_calls=1,
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        latency_ms=Decimal("12.5"),
        cost_cny=Decimal("0.001"),
    )

    assert result.status is AgentResultStatus.SUCCEEDED
    assert result.actions[0].arguments["product_id"] == "p001"
    with pytest.raises(ValidationError):
        AgentResult.model_validate({**result.model_dump(mode="json"), "chain_of_thought": "secret"})


@pytest.mark.parametrize("invalid_json", [float("nan"), float("inf"), ("not", "json"), {1: "value"}])
def test_protocol_rejects_non_json_values(invalid_json: object) -> None:
    """任务快照只接受有限 JSON，不能隐式接纳 Python 专有值或非有限数字。"""

    with pytest.raises(ValidationError, match="JSON|unsupported"):
        _task(input_snapshot={"value": invalid_json})


def test_action_and_result_nested_json_are_deeply_frozen() -> None:
    """动作参数和结果输出不能在校验后被调用方或执行器原地改写。"""

    action = AgentAction(
        kind=AgentActionKind.CALL_SKILL,
        skill_id="generate_on_live_prompt",
        arguments={"items": [{"product_id": "p001"}]},
    )
    result = AgentResult(
        task_id="task-001",
        profile_id="live-ops",
        profile_version="1.0.0",
        status=AgentResultStatus.SUCCEEDED,
        output={"items": [{"action": "NO_ACTION"}]},
        summary="无需动作",
    )

    with pytest.raises(TypeError):
        action.arguments["items"][0]["product_id"] = "tampered"
    with pytest.raises(TypeError):
        result.output["items"][0]["action"] = "tampered"


def test_agent_result_requires_mutually_exclusive_success_or_failure_shape() -> None:
    """成功结果只携带业务输出，失败结果只携带稳定结构化失败。"""

    failure = AgentFailure(
        code="MODEL_TIMEOUT",
        retryable=True,
        details={"deadline_at": "2026-07-15T00:00:00Z"},
    )

    with pytest.raises(ValidationError, match="failure"):
        AgentResult(
            task_id="task-001",
            profile_id="live-ops",
            profile_version="1.0.0",
            status=AgentResultStatus.MODEL_ERROR,
            summary="模型调用超时",
        )
    with pytest.raises(ValidationError, match="output"):
        AgentResult(
            task_id="task-001",
            profile_id="live-ops",
            profile_version="1.0.0",
            status=AgentResultStatus.MODEL_ERROR,
            output={"action": "NO_ACTION"},
            failure=failure,
            summary="模型调用超时",
        )
    with pytest.raises(ValidationError, match="failure"):
        AgentResult(
            task_id="task-001",
            profile_id="live-ops",
            profile_version="1.0.0",
            status=AgentResultStatus.SUCCEEDED,
            output={"action": "NO_ACTION"},
            failure=failure,
            summary="错误地混入失败",
        )


def test_profile_is_strict_deeply_frozen_and_digest_stable() -> None:
    """Profile 的 Schema、白名单和执行预算必须形成稳定启动快照。"""

    profile = _profile()
    replay = SpecialistProfile.model_validate(profile.model_dump(mode="json"))

    assert profile.profile_digest == replay.profile_digest
    with pytest.raises(TypeError):
        profile.result_schema["properties"]["action"]["type"] = "integer"
    with pytest.raises(ValidationError):
        SpecialistProfile.model_validate(
            {**profile.model_dump(mode="json"), "allowed_skill_ids": ["same", "same"]}
        )


def test_profile_rejects_result_schema_hash_rebinding() -> None:
    """结果 Schema 内容变化时必须同时产生新哈希，不能沿用旧审计身份。"""

    payload = _profile().model_dump(mode="json")
    payload["result_schema"]["properties"]["action"]["type"] = "integer"
    payload.pop("profile_digest")

    with pytest.raises(ValidationError, match="result_schema_hash"):
        SpecialistProfile.model_validate(payload)


@pytest.mark.parametrize(
    "endpoint_host",
    [
        "api.deepseek.com@evil.example",
        "api.deepseek.com?redirect=evil",
        "api.deepseek.com#evil",
        "api deepseek.com",
        "-api.deepseek.com",
        "api.déepseek.com",
    ],
)
def test_profile_rejects_non_hostname_endpoint_authority(endpoint_host: str) -> None:
    """Profile 只保存 DNS hostname，避免 Task 2 拼接 URL 时泄露凭据。"""

    payload = _profile().model_dump(mode="json")
    payload["endpoint_host"] = endpoint_host
    payload.pop("profile_digest")

    with pytest.raises(ValidationError, match="endpoint_host"):
        SpecialistProfile.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [("endpoint_host", "example.com"), ("model_id", "other-model")],
)
def test_profile_rejects_non_formal_model_identity(field: str, value: str) -> None:
    """Phase 13 正式 Profile 只能绑定冻结 Design 指定的 endpoint 与模型。"""

    payload = _profile().model_dump(mode="json")
    payload[field] = value
    payload.pop("profile_digest")

    with pytest.raises(ValidationError, match=field):
        SpecialistProfile.model_validate(payload)


def test_registry_is_idempotent_and_rejects_identity_conflict() -> None:
    """同身份同摘要可重放，同身份不同事实必须在启动装配时失败。"""

    registry = SpecialistProfileRegistry()
    first = registry.register(_profile())
    replay = registry.register(_profile())

    assert first is replay
    with pytest.raises(SpecialistProfileConflictError):
        registry.register(_profile(prompt_text="different frozen prompt"))


def test_profile_skill_whitelist_has_order_independent_identity() -> None:
    """Skill 白名单是集合语义，配置顺序变化不能制造虚假的 Profile 冲突。"""

    payload = _profile().model_dump(mode="json")
    payload["allowed_skill_ids"] = ["skill-b", "skill-a"]
    payload["skill_versions"] = {"skill-b": "2.0.0", "skill-a": "1.0.0"}
    payload.pop("profile_digest")
    first = SpecialistProfile.model_validate(payload)
    payload["allowed_skill_ids"] = ["skill-a", "skill-b"]
    payload["skill_versions"] = {"skill-a": "1.0.0", "skill-b": "2.0.0"}
    second = SpecialistProfile.model_validate(payload)

    assert first.allowed_skill_ids == ("skill-a", "skill-b")
    assert first.profile_digest == second.profile_digest


def test_orchestrator_resolves_exact_profile_and_task_kind() -> None:
    """多个 Profile 可并存，但一个任务只能确定性解析一个精确版本。"""

    registry = SpecialistProfileRegistry(
        profiles=(
            _profile(),
            _profile(
                profile_id="planner",
                task_kind=SpecialistTaskKind.PLAN_PROPOSAL,
            ),
        )
    )
    orchestrator = SpecialistOrchestrator(registry)

    assert orchestrator.resolve_profile(_task()).profile_id == "live-ops"
    with pytest.raises(SpecialistProfileResolutionError, match="task_kind"):
        orchestrator.resolve_profile(
            _task(
                task_kind=SpecialistTaskKind.PLAN_PROPOSAL,
                profile_id="live-ops",
            )
        )
    with pytest.raises(SpecialistProfileResolutionError, match="未知"):
        orchestrator.resolve_profile(_task(profile_version="9.9.9"))


def test_orchestrator_uses_frozen_task_kind_route_not_caller_profile_choice() -> None:
    """同生命周期存在多个 Profile 时，只能由启动冻结路由选择精确身份。"""

    registry = SpecialistProfileRegistry(
        profiles=(
            _profile(profile_version="1.0.0"),
            _profile(profile_version="2.0.0", prompt_text="different frozen prompt"),
        )
    )
    orchestrator = SpecialistOrchestrator(
        registry,
        routes={SpecialistTaskKind.LIVE_OPS_ADVICE: ("live-ops", "1.0.0")},
    )

    assert orchestrator.resolve_profile(_task(profile_version="1.0.0")).profile_version == "1.0.0"
    with pytest.raises(SpecialistProfileResolutionError, match="frozen route"):
        orchestrator.resolve_profile(_task(profile_version="2.0.0"))


@pytest.mark.parametrize(
    "routes",
    [
        {"LIVE_OPS_ADVICE": ("live-ops", "1.0.0")},
        {SpecialistTaskKind.LIVE_OPS_ADVICE: ["live-ops", "1.0.0"]},
        {SpecialistTaskKind.LIVE_OPS_ADVICE: ("live-ops",)},
        {SpecialistTaskKind.LIVE_OPS_ADVICE: ("live-ops", 1)},
    ],
)
def test_orchestrator_rejects_malformed_explicit_routes(routes: dict) -> None:
    """启动路由 key 和身份必须是精确枚举及二元字符串 tuple，不能保留可变对象。"""

    with pytest.raises(SpecialistProfileResolutionError, match="route"):
        SpecialistOrchestrator(SpecialistProfileRegistry((_profile(),)), routes=routes)


def test_orchestrator_fails_closed_when_task_kind_route_is_ambiguous() -> None:
    """未显式配置且同类 Profile 多于一个时，路由不得猜测最新版本。"""

    registry = SpecialistProfileRegistry(
        profiles=(
            _profile(profile_version="1.0.0"),
            _profile(profile_version="2.0.0", prompt_text="different frozen prompt"),
        )
    )

    with pytest.raises(SpecialistProfileResolutionError, match="ambiguous"):
        SpecialistOrchestrator(registry)


def test_orchestrator_implicit_routes_are_frozen_at_startup() -> None:
    """装配后的 Registry 变化不能改写已有路由，也不能解封原本缺失的生命周期。"""

    registry = SpecialistProfileRegistry(profiles=(_profile(),))
    orchestrator = SpecialistOrchestrator(registry)

    registry.register(_profile(profile_version="2.0.0", prompt_text="different frozen prompt"))
    registry.register(
        _profile(
            profile_id="planner",
            task_kind=SpecialistTaskKind.PLAN_PROPOSAL,
        )
    )

    assert orchestrator.resolve_profile(_task()).profile_version == "1.0.0"
    with pytest.raises(SpecialistProfileResolutionError, match="未知 task_kind route"):
        orchestrator.resolve_profile(
            _task(
                task_kind=SpecialistTaskKind.PLAN_PROPOSAL,
                profile_id="planner",
            )
        )
