"""Phase 13 Task 9 播后 Skill 与受控记忆晋升的首批契约测试。"""

from __future__ import annotations

from decimal import Decimal
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from jsonschema import Draft202012Validator, ValidationError

from src.memory.candidate_store import (
    InMemoryMemoryCandidateStore,
    MemoryCandidate,
    MemoryCandidateStatus,
    MemoryPromotionCommand,
)
from src.memory.promotion_policy import PromotionPolicy
from src.memory.models import AnchorMemoryEntry
from src.skill_runtime.catalog import get_default_skill_catalog
from src.state.models import LifecycleStage, RiskLevel
from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers
from src.skill_runtime.models import SkillExecutionContext, SkillExecutionRoute


def _candidate() -> MemoryCandidate:
    """构造只含白名单结构字段的候选，不允许把 Agent 自由正文带入存储。"""

    return MemoryCandidate(
        candidate_id="candidate-001",
        idempotency_key="stage-001",
        anchor_id="anchor-001",
        room_id="room-001",
        evidence_ids=("trace-a", "trace-b"),
        preferred_category="kitchen",
        preferred_tags=("profit",),
        preferred_product_ids=("p001",),
        confidence=Decimal("0.90"),
    )


def test_candidate_store_stages_idempotently_and_rejects_free_text() -> None:
    """staging 只能保存结构化候选；同幂等键重放必须返回首次事实。"""

    store = InMemoryMemoryCandidateStore()
    first = store.stage(_candidate())
    replay = store.stage(_candidate())

    assert first == replay
    assert first.status is MemoryCandidateStatus.STAGED
    with pytest.raises(ValueError, match="free_text"):
        store.stage(_candidate().model_copy(update={"free_text": "model private text"}))


def test_promotion_requires_two_matching_traces_and_expected_version() -> None:
    """单证据或过期版本都不得自动晋升；命令重放返回第一次结果。"""

    store = InMemoryMemoryCandidateStore()
    staged = store.stage(_candidate())
    policy = PromotionPolicy(store=store, active_memory_port=None)
    command = MemoryPromotionCommand(
        command_id="promote-001",
        candidate_id=staged.candidate_id,
        expected_version=staged.version,
        expected_status=MemoryCandidateStatus.STAGED,
    )

    result = policy.promote(
        command,
        decision_traces=(
            {"decision_trace_id": "trace-a", "anchor_id": "anchor-001", "room_id": "room-001"},
        ),
        product_whitelist={"p001"},
    )

    assert result.status is MemoryCandidateStatus.STAGED
    assert result.reason_code == "INSUFFICIENT_INDEPENDENT_EVIDENCE"
    with pytest.raises(ValueError, match="expected_version"):
        policy.promote(
            command.model_copy(update={"command_id": "promote-stale", "expected_version": 2}),
            decision_traces=(),
            product_whitelist={"p001"},
        )


def test_post_live_skill_manifests_are_explicit_and_do_not_accept_hidden_store_inputs() -> None:
    """三个新增 Skill 均为 POST_LIVE；归因只能消费显式快照，暂存不能夹带自由文本。"""

    manifests = {item.skill_id: item for item in get_default_skill_catalog()}
    assert len(manifests) == 17
    evidence = manifests["collect_post_live_evidence"]
    attribution = manifests["calculate_post_live_attribution"]
    staging = manifests["stage_memory_candidates"]
    assert evidence.lifecycle == {LifecycleStage.POST_LIVE}
    assert attribution.risk_level is RiskLevel.LOW
    assert staging.requires_idempotency_key is True
    Draft202012Validator(evidence.parameter_schema).validate(
        {"anchor_id": "anchor-001", "room_id": "room-001", "trace_id": "trace-001"}
    )
    Draft202012Validator(attribution.parameter_schema).validate(
        {"evidence_snapshot": {"decision_traces": []}}
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(staging.parameter_schema).validate(
            {
                "candidate_id": "candidate-001",
                "evidence_ids": ["trace-a", "trace-b"],
                "free_text": "must never be persisted",
            }
        )


class _ActiveMemoryPort:
    """记录 Policy 写入，证明模板而不是模型自由文本成为 active memory。"""

    def __init__(self) -> None:
        self.entries: list[AnchorMemoryEntry] = []

    def write_memory(self, entry: AnchorMemoryEntry) -> str:
        self.entries.append(entry)
        return "memory-001"


def test_promotion_applies_two_scoped_traces_as_deterministic_template_memory() -> None:
    """双独立 trace 且货盘命中时才可写 active memory；命令重放不得二次写入。"""

    store = InMemoryMemoryCandidateStore()
    candidate = store.stage(_candidate())
    memory = _ActiveMemoryPort()
    policy = PromotionPolicy(store=store, active_memory_port=memory)
    command = MemoryPromotionCommand(command_id="promote-ok", candidate_id=candidate.candidate_id, expected_version=1, expected_status=MemoryCandidateStatus.STAGED)
    traces = (
        {"decision_trace_id": "trace-a", "anchor_id": "anchor-001", "room_id": "room-001"},
        {"decision_trace_id": "trace-b", "anchor_id": "anchor-001", "room_id": "room-001"},
    )

    result = policy.promote(command, decision_traces=traces, product_whitelist={"p001"})

    assert result.status is MemoryCandidateStatus.APPLIED
    assert len(memory.entries) == 1
    assert "model" not in memory.entries[0].content.lower()
    assert policy.promote(command, decision_traces=traces, product_whitelist={"p001"}) == result
    assert len(memory.entries) == 1


@pytest.mark.parametrize(
    ("traces", "whitelist", "reason"),
    [
        (({"decision_trace_id": "trace-a", "anchor_id": "anchor-x", "room_id": "room-001"}, {"decision_trace_id": "trace-b", "anchor_id": "anchor-x", "room_id": "room-001"}), {"p001"}, "INSUFFICIENT_INDEPENDENT_EVIDENCE"),
        (({"decision_trace_id": "trace-a", "anchor_id": "anchor-001", "room_id": "room-001"}, {"decision_trace_id": "trace-b", "anchor_id": "anchor-001", "room_id": "room-001"}), {"p999"}, "PRODUCT_WHITELIST_MISMATCH"),
    ],
)
def test_promotion_keeps_candidate_staged_when_scope_or_whitelist_fails(traces, whitelist, reason) -> None:
    """跨作用域或货盘不匹配都必须停留 STAGED，且不能写入 active memory。"""

    store = InMemoryMemoryCandidateStore()
    candidate = store.stage(_candidate())
    memory = _ActiveMemoryPort()
    result = PromotionPolicy(store=store, active_memory_port=memory).promote(
        MemoryPromotionCommand(command_id=f"command-{reason}", candidate_id=candidate.candidate_id, expected_version=1, expected_status=MemoryCandidateStatus.STAGED),
        decision_traces=traces,
        product_whitelist=whitelist,
    )
    assert result.status is MemoryCandidateStatus.STAGED
    assert result.reason_code == reason
    assert memory.entries == []


class _EvidencePort:
    """只返回调用方明确绑定的脱敏快照，测试 Handler 不会隐式查询其他事实源。"""

    def collect(self, *, anchor_id: str, room_id: str, trace_id: str) -> dict:
        return {"decision_traces": [{"trace_id": trace_id, "anchor_id": anchor_id, "room_id": room_id, "anchor_action": "accepted", "business_result": "good"}]}


def test_post_live_handlers_only_use_injected_evidence_and_candidate_store() -> None:
    """证据收集、纯归因和 staging 三段必须可经同一 Runtime Handler 显式组合。"""

    store = InMemoryMemoryCandidateStore()
    handlers = build_skill_handlers(SkillRuntimeDependencies(platform=object(), post_live_evidence_port=_EvidencePort(), memory_candidate_store=store))
    context = SkillExecutionContext(room_id="room-001", trace_id="trace-001", lifecycle=LifecycleStage.POST_LIVE, execution_route=SkillExecutionRoute.SKILL_RUNTIME, idempotency_key="stage-handler", deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15))
    evidence = asyncio.run(handlers["collect_post_live_evidence"].execute("collect_post_live_evidence", {"anchor_id": "anchor-001", "room_id": "room-001", "trace_id": "trace-001"}, context))
    attribution = asyncio.run(handlers["calculate_post_live_attribution"].execute("calculate_post_live_attribution", evidence, context))
    staged = asyncio.run(handlers["stage_memory_candidates"].execute("stage_memory_candidates", {"candidate_id": "handler-candidate", "anchor_id": "anchor-001", "room_id": "room-001", "evidence_ids": ["trace-a", "trace-b"], "preferred_category": "kitchen", "preferred_product_ids": ["p001"], "confidence": "0.90"}, context))
    assert attribution["attribution"]["total_decisions"] == 1
    assert staged == {"candidate_id": "handler-candidate", "status": "STAGED", "version": 1}
