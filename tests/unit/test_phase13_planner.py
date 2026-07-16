"""Phase 13 Task 8 PlannerAgent 与只读记忆 Skill 测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from src.memory.models import (
    AnchorMemoryEntry,
    MemoryLayer,
    MemorySource,
    MemoryStatus,
)
from src.specialist_evaluation.planner import (
    PlannerCaseLabel,
    PlannerCaseScore,
    PlannerValidationGate,
    PlannerValidationGateStatus,
    score_planner_proposal,
)
from src.specialist_runtime.planner import (
    CandidatePlannerProposal,
    PlannerAgentAdapter,
    PlannerProposalCompiler,
    RankedProductPlannerPolicy,
    build_planner_profile,
)
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers
from src.skill_runtime.models import SkillExecutionContext, SkillExecutionRoute
from src.state.models import LifecycleStage, RiskLevel


class _UnusedPlatform:
    """本组测试不允许记忆读取退回商品或直播平台。"""


class _MemoryPort:
    """记录严格作用域，并返回包含敏感字段和 suppressed 记录的 Store 快照。"""

    def __init__(self, memories: list[AnchorMemoryEntry]) -> None:
        self._memories = memories
        self.calls: list[tuple[str, str | None]] = []

    def list_memories(
        self,
        anchor_id: str,
        room_id: str | None = None,
        layer: MemoryLayer | None = None,
    ) -> list[AnchorMemoryEntry]:
        self.calls.append((anchor_id, room_id))
        return list(self._memories)


def _context(room_id: str = "room-001") -> SkillExecutionContext:
    return SkillExecutionContext(
        room_id=room_id,
        trace_id="trace-planner-memory",
        lifecycle=LifecycleStage.PRE_LIVE,
        execution_route=SkillExecutionRoute.SKILL_RUNTIME,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15),
    )


def _memory(
    memory_id: str,
    *,
    anchor_id: str = "anchor-001",
    room_id: str | None = "room-001",
    status: MemoryStatus = MemoryStatus.ACTIVE,
    created_at: datetime | None = None,
) -> AnchorMemoryEntry:
    return AnchorMemoryEntry(
        memory_id=memory_id,
        memory_key=f"key-{memory_id}",
        anchor_id=anchor_id,
        room_id=room_id,
        layer=MemoryLayer.L1,
        content=f"private free text for {memory_id}",
        metadata={
            "preferred_category": "beauty",
            "preferred_tags": ["pace"],
            "preferred_product_ids": ["sim-product-001"],
            "private_note": "must never leave store",
        },
        confidence=Decimal("0.80"),
        evidence_weight=Decimal("0.70"),
        source=MemorySource.SYSTEM_OBSERVED,
        status=status,
        suppressed_reason="conflict secret" if status is MemoryStatus.SUPPRESSED else None,
        embedding=[0.1, 0.2],
        created_at=created_at or datetime(2026, 7, 16, tzinfo=timezone.utc),
        updated_at=created_at or datetime(2026, 7, 16, tzinfo=timezone.utc),
    )


def test_retrieve_anchor_memory_manifest_is_strict_read_only_contract() -> None:
    """Catalog 必须只有 14 个单活 Skill，记忆查询参数不能夹带执行控制字段。"""

    catalog = tuple(get_default_skill_catalog())
    assert len(catalog) == 17
    manifest = next(item for item in catalog if item.skill_id == "retrieve_anchor_memory")
    assert manifest.version == "1.0.0"
    assert manifest.lifecycle == {LifecycleStage.PRE_LIVE}
    assert manifest.risk_level is RiskLevel.LOW
    assert manifest.requires_idempotency_key is False
    assert manifest.parameter_schema == {
        "type": "object",
        "required": ["anchor_id", "room_id", "limit"],
        "properties": {
            "anchor_id": {"type": "string", "minLength": 1},
            "room_id": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "additionalProperties": False,
    }
    Draft202012Validator(manifest.parameter_schema).validate(
        {"anchor_id": "anchor-001", "room_id": "room-001", "limit": 5}
    )
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(manifest.parameter_schema).validate(
            {
                "anchor_id": "anchor-001",
                "room_id": "room-001",
                "limit": 5,
                "trace_id": "forged",
            }
        )


def test_retrieve_anchor_memory_filters_scope_status_limit_and_sensitive_fields() -> None:
    """Handler 必须在 Port 返回后再次校验作用域，并只投影脱敏白名单字段。"""

    port = _MemoryPort(
        [
            _memory("active-new", created_at=datetime(2026, 7, 16, 2, tzinfo=timezone.utc)),
            _memory("anchor-level", room_id=None),
            _memory("suppressed", status=MemoryStatus.SUPPRESSED),
            _memory("other-anchor", anchor_id="anchor-999"),
            _memory("other-room", room_id="room-999"),
        ]
    )
    handlers = build_skill_handlers(
        SkillRuntimeDependencies(platform=_UnusedPlatform(), memory_port=port)
    )

    result = asyncio.run(
        handlers["retrieve_anchor_memory"].execute(
            "retrieve_anchor_memory",
            {"anchor_id": "anchor-001", "room_id": "room-001", "limit": 2},
            _context(),
        )
    )

    assert port.calls == [("anchor-001", "room-001")]
    assert [item["memory_id"] for item in result["memory_refs"]] == [
        "active-new",
        "anchor-level",
    ]
    encoded = repr(result)
    assert "private free text" not in encoded
    assert "private_note" not in encoded
    assert "embedding" not in encoded
    assert "suppressed_reason" not in encoded
    assert result["memory_refs"][0]["preferred_category"] == "beauty"
    assert result["memory_refs"][0]["preferred_tags"] == ["pace"]


def test_retrieve_anchor_memory_rejects_argument_room_before_port_call() -> None:
    """业务 room_id 与可信 Context 不一致时，不得访问 MemoryStore。"""

    port = _MemoryPort([_memory("active")])
    handler = build_skill_handlers(
        SkillRuntimeDependencies(platform=_UnusedPlatform(), memory_port=port)
    )["retrieve_anchor_memory"]

    with pytest.raises(ValueError, match="room_id"):
        asyncio.run(
            handler.execute(
                "retrieve_anchor_memory",
                {"anchor_id": "anchor-001", "room_id": "room-forged", "limit": 5},
                _context("room-001"),
            )
        )
    assert port.calls == []


def _planner_case(index: int = 1) -> dict:
    path = (
        __import__("pathlib").Path(__file__).parents[2]
        / "evaluation"
        / "cases"
        / "phase13"
        / "planner-development.jsonl"
    )
    return __import__("json").loads(path.read_text(encoding="utf-8").splitlines()[index - 1])


def _planner_label(index: int = 1) -> PlannerCaseLabel:
    path = (
        __import__("pathlib").Path(__file__).parents[2]
        / "evaluation"
        / "labels"
        / "phase13"
        / "planner-development.jsonl"
    )
    payload = __import__("json").loads(path.read_text(encoding="utf-8").splitlines()[index - 1])
    return PlannerCaseLabel.model_validate(payload["label"])


def test_candidate_planner_proposal_rejects_execution_controls_and_forbidden_capabilities() -> None:
    """模型只能声明白名单业务能力，不能选择版本、资源、重试、建播或查询。"""

    valid = RankedProductPlannerPolicy().propose(_planner_case(1)["input"])
    replay = CandidatePlannerProposal.model_validate(valid.model_dump(mode="json"))
    assert replay == valid
    payload = valid.model_dump(mode="json")
    payload["nodes"][0]["skill_version"] = "99.0.0"
    with pytest.raises(ValidationError):
        CandidatePlannerProposal.model_validate(payload)
    payload = valid.model_dump(mode="json")
    payload["nodes"][0]["capability"] = "setup_live_session"
    with pytest.raises(ValidationError):
        CandidatePlannerProposal.model_validate(payload)
    payload["nodes"][0]["capability"] = "query_products"
    with pytest.raises(ValidationError):
        CandidatePlannerProposal.model_validate(payload)


def test_candidate_planner_proposal_rejects_cycles_unknown_bindings_and_controls() -> None:
    """循环、未知节点引用和伪造执行控制绑定必须在 Compiler 前拒绝。"""

    proposal = RankedProductPlannerPolicy().propose(_planner_case(1)["input"])
    payload = proposal.model_dump(mode="json")
    first = payload["nodes"][0]["logical_key"]
    second = payload["nodes"][1]["logical_key"]
    payload["dependencies"] = [
        {"from": first, "to": second},
        {"from": second, "to": first},
    ]
    with pytest.raises(ValidationError, match="cycle"):
        CandidatePlannerProposal.model_validate(payload)

    payload = proposal.model_dump(mode="json")
    payload["bindings"].append(
        {"target": f"{first}.deadline_at", "source_type": "LITERAL", "source": 30}
    )
    with pytest.raises(ValidationError, match="control"):
        CandidatePlannerProposal.model_validate(payload)


def test_planner_compiler_creates_plan_engine_candidate_without_model_execution_controls() -> None:
    """Compiler 只转换受限 DAG；精确版本和资源策略仍由 PlanEngine 后续注入。"""

    case = _planner_case(1)
    proposal = RankedProductPlannerPolicy().propose(case["input"])
    compiled = PlannerProposalCompiler().compile(proposal, case_input=case["input"])

    assert compiled.candidate.provider_id == "planner-agent"
    assert {node.skill_id for node in compiled.candidate.nodes} <= {
        "generate_live_plan",
        "generate_product_card",
        "suggest_price_change",
    }
    assert all(not hasattr(node, "skill_version") for node in compiled.candidate.nodes)
    assert all(not hasattr(node, "deadline_at") for node in compiled.candidate.nodes)
    assert all(capability.skill_version == "1.0.0" for capability in compiled.capabilities)
    assert all(capability.max_attempt_seconds == 15 for capability in compiled.capabilities)
    assert all(capability.resource_keys for capability in compiled.capabilities)


def test_planner_baseline_is_executable_but_has_explainable_recovery_gap() -> None:
    """baseline 保持固定排序；失败商品位于前三位时可执行但 recovery 失败。"""

    index = next(
        candidate
        for candidate in range(1, 21)
        if _planner_label(candidate).constraint_recovery_required
    )
    case = _planner_case(index)
    proposal = RankedProductPlannerPolicy().propose(case["input"])
    score = score_planner_proposal(
        case_id=case["case_id"],
        case_input=case["input"],
        label=_planner_label(index),
        proposal=proposal,
        compiler=PlannerProposalCompiler(),
    )

    assert score.executable_plan is True
    assert score.constraint_recovery is False
    assert score.severe_violation is False


def test_planner_profile_and_adapter_keep_formal_runtime_skill_free() -> None:
    """Planner 正式 Profile 必须为零 Skill；adapter 只冻结同一 case 输入。"""

    root = __import__("pathlib").Path(__file__).parents[2] / "evaluation"
    profile = build_planner_profile(root)
    assert profile.max_model_calls == 3
    assert profile.max_skill_calls == 0
    assert profile.max_total_tokens == 8000
    assert profile.deadline_seconds == 15
    assert profile.allowed_skill_ids == ()

    class _Runner:
        async def run(self, task):
            self.task = task
            raise RuntimeError("stop after task capture")

    runner = _Runner()
    adapter = PlannerAgentAdapter(runner=runner, profile=profile)
    with pytest.raises(RuntimeError, match="capture"):
        asyncio.run(adapter.run_case(_planner_case(1)))
    assert runner.task.model_dump(mode="json")["input_snapshot"] == _planner_case(1)["input"]
    assert runner.task.initial_evidence_refs == ()


def test_planner_validation_gate_unlocks_only_after_four_complete_shards() -> None:
    """可执行性与恢复率必须同时满足整数门，40 例完成前不得读取 holdout。"""

    gate = PlannerValidationGate(
        baseline_executable_successes=40,
        baseline_constraint_recoveries=27,
    )
    for shard_index in range(4):
        decision = gate.record_shard(
            tuple(
                PlannerCaseScore(
                    case_id=f"planner-validation-{shard_index * 10 + offset:03d}",
                    executable_plan=True,
                    constraint_recovery=True,
                    severe_violation=False,
                )
                for offset in range(1, 11)
            )
        )
        expected = (
            PlannerValidationGateStatus.HOLDOUT_UNLOCKED
            if shard_index == 3
            else PlannerValidationGateStatus.CONTINUE
        )
        assert decision.status is expected


def test_planner_validation_gate_rejects_unreachable_recovery_without_inconclusive() -> None:
    """剩余全对仍达不到 recovery AND 门时必须规则拒绝，不能标成外部证据不足。"""

    gate = PlannerValidationGate(
        baseline_executable_successes=40,
        baseline_constraint_recoveries=30,
    )
    first = tuple(
        PlannerCaseScore(
            case_id=f"planner-validation-{index:03d}",
            executable_plan=True,
            constraint_recovery=index <= 5,
            severe_violation=False,
        )
        for index in range(1, 11)
    )
    assert gate.record_shard(first).status is PlannerValidationGateStatus.CONTINUE
    decision = gate.record_shard(
        tuple(
            PlannerCaseScore(
                case_id=f"planner-validation-{index:03d}",
                executable_plan=True,
                constraint_recovery=False,
                severe_violation=False,
            )
            for index in range(11, 21)
        )
    )
    assert decision.status is PlannerValidationGateStatus.REJECTED
    assert decision.reason_code == "QUALITY_THRESHOLD_UNREACHABLE"
