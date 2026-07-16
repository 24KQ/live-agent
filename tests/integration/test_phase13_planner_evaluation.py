"""Phase 13 Task 8 PlannerAgent 的 80 例无网络纵向集成。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

from src.specialist_evaluation.models import (
    EvaluationCandidate,
    EvaluationManifest,
    EvaluationManifestKind,
    EvaluationRun,
    _build_formal_manifest_authorization,
)
from src.specialist_evaluation.planner import (
    PlannerCaseLabel,
    PlannerPairedEvaluationRecorder,
    PlannerValidationGateStatus,
)
from src.specialist_evaluation.store import InMemorySpecialistEvaluationStore
from src.specialist_runtime.budget import InMemoryModelBudgetStore
from src.specialist_runtime.evidence import EvidenceResolverRegistry
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import EvidenceKind, canonical_json_sha256
from src.specialist_runtime.planner import (
    CandidatePlannerProposal,
    PlannerAgentAdapter,
    RankedProductPlannerPolicy,
    build_planner_profile,
)
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
from src.specialist_runtime.runner import BoundedSpecialistRunner
from src.specialist_runtime.scripted_model import ScriptedAgentModel
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers
from src.skill_runtime.models import SkillExecutionContext, SkillExecutionRoute
from src.state.models import LifecycleStage
from src.config.settings import get_settings
from src.memory.demo_memory_seed import initialize_phase3_schema
from src.memory.memory_store import MemoryStore
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource, MemoryStatus
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data
from src.skills.embedding_service import EmbeddingService
from src.skill_runtime.fake_platform import FakeLiveCommercePlatform, FakePlatformFixture


ROOT = Path(__file__).parents[2]
EVALUATION_ROOT = ROOT / "evaluation"
HASH_A = "a" * 64


def test_retrieve_anchor_memory_reads_scoped_active_rows_from_postgres(monkeypatch) -> None:
    """真实 PostgreSQL 读取仍须经过 Handler 二次过滤，且不得泄露正文和 embedding。"""

    # MemoryStore 的历史写路径会尝试补 embedding；本任务只验证关系数据读取，显式替换为
    # 本地空向量，避免测试环境访问任何模型端点。
    monkeypatch.setattr(EmbeddingService, "embed", lambda _self, _content: [])
    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    store = MemoryStore(settings)
    anchor_id = "anchor-demo-001"
    room_id = "room-demo-001"
    prefix = "phase13-task8-postgres"
    active_id = store.write_memory(
        AnchorMemoryEntry(
            memory_key=f"{prefix}-active",
            anchor_id=anchor_id,
            room_id=room_id,
            layer=MemoryLayer.L2,
            content="不得由 Skill 返回的记忆正文",
            metadata={"preferred_category": "厨房", "private_note": "不得返回"},
            confidence=Decimal("0.90"),
            evidence_weight=Decimal("0.80"),
            source=MemorySource.SYSTEM_OBSERVED,
            status=MemoryStatus.ACTIVE,
            embedding=[0.1, 0.2],
        )
    )
    store.write_memory(
        AnchorMemoryEntry(
            memory_key=f"{prefix}-suppressed",
            anchor_id=anchor_id,
            room_id=room_id,
            layer=MemoryLayer.L2,
            content="已抑制记忆",
            metadata={"preferred_category": "家居"},
            source=MemorySource.SYSTEM_OBSERVED,
            status=MemoryStatus.SUPPRESSED,
            suppressed_reason="证据冲突",
        )
    )
    handler = build_skill_handlers(
        SkillRuntimeDependencies(
            # 记忆读取不会访问平台；仍显式创建独立 Fixture，满足统一 Handler 装配契约。
            platform=FakeLiveCommercePlatform.from_fixture(FakePlatformFixture(room_id=room_id)),
            memory_port=store,
        )
    )["retrieve_anchor_memory"]

    result = asyncio.run(
        handler.execute(
            "retrieve_anchor_memory",
            {"anchor_id": anchor_id, "room_id": room_id, "limit": 20},
                SkillExecutionContext(
                    room_id=room_id,
                    trace_id=f"trace-{prefix}",
                    lifecycle=LifecycleStage.PRE_LIVE,
                    execution_route=SkillExecutionRoute.SKILL_RUNTIME,
                    deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15),
                ),
        )
    )

    selected = next(item for item in result["memory_refs"] if item["memory_id"] == active_id)
    assert selected["preferred_category"] == "厨房"
    assert "content" not in selected
    assert "embedding" not in selected
    assert "private_note" not in selected
    assert all(item["memory_key"] != f"{prefix}-suppressed" for item in result["memory_refs"])


class _EmptyLoader:
    """Planner case 当前不携带 EvidenceRef，但共享 Registry 仍需完整八类 loader。"""

    def load(self, _evidence_id: str):
        return None


class _NoSkillPort:
    """零 Skill Profile 的哨兵；任何调用都代表 Planner 权限回归。"""

    async def invoke(self, **_kwargs):
        raise AssertionError("Planner formal runtime must not call Skill")


class _PricingPolicy:
    """脚本模型使用固定计价路径验证预算，不产生真实费用。"""

    policy_digest = HASH_A

    def count_input_tokens(self, _request) -> int:
        return 10

    def worst_case_cost(self, _request, _profile) -> Decimal:
        return Decimal("0.001")

    def actual_cost(self, _usage, _profile) -> Decimal:
        return Decimal("0.001")


def _records(split: str) -> tuple[list[dict], dict[str, PlannerCaseLabel]]:
    cases = [
        json.loads(line)
        for line in (
            EVALUATION_ROOT / "cases" / "phase13" / f"planner-{split}.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    labels = {
        item["case_id"]: PlannerCaseLabel.model_validate(item["label"])
        for item in (
            json.loads(line)
            for line in (
                EVALUATION_ROOT / "labels" / "phase13" / f"planner-{split}.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        )
    }
    return cases, labels


def _formal_manifest() -> EvaluationManifest:
    """把 Task 6 的 240 case 基线投影为仅供本测试 Store 使用的 Formal 身份。"""

    source = json.loads(
        (EVALUATION_ROOT / "manifests" / "phase13-v2.json").read_text(encoding="utf-8")
    )["store_manifest"]
    payload = {
        **source,
        "manifest_id": "phase13-planner-scripted-test",
        "manifest_kind": EvaluationManifestKind.FORMAL_EVALUATION.value,
        "source_commit": "a" * 40,
    }
    payload.pop("manifest_digest", None)
    return EvaluationManifest.model_validate(payload)


def _recovery_aware(case: dict) -> CandidatePlannerProposal:
    """生成 ScriptedModel 的理想输出：只移除当前已失败商品，其他排序保持 baseline。"""

    baseline = RankedProductPlannerPolicy().propose(case["input"])
    failed = set(case["input"]["current_plan"]["failed_product_ids"])
    removed_keys = {
        node.logical_key
        for node in baseline.nodes
        if node.logical_key.startswith("card:")
        and node.logical_key.removeprefix("card:") in failed
    }
    return CandidatePlannerProposal(
        nodes=tuple(node for node in baseline.nodes if node.logical_key not in removed_keys),
        dependencies=tuple(
            edge
            for edge in baseline.dependencies
            if edge.from_key not in removed_keys and edge.to_key not in removed_keys
        ),
        bindings=tuple(
            binding
            for binding in baseline.bindings
            if binding.target.rsplit(".", 1)[0] not in removed_keys
        ),
    )


def test_scripted_planner_runs_80_cases_and_unlocks_holdout_after_validation() -> None:
    """同一冻结输入完成 baseline/Agent 配对，40 validation 通过后才运行 20 holdout。"""

    profile = build_planner_profile(EVALUATION_ROOT)
    all_cases: list[dict] = []
    labels: dict[str, PlannerCaseLabel] = {}
    for split in ("development", "validation", "holdout"):
        cases, split_labels = _records(split)
        all_cases.extend(cases)
        labels.update(split_labels)

    task_builder = PlannerAgentAdapter(runner=None, profile=profile)  # type: ignore[arg-type]
    outcomes = {}
    for case in all_cases:
        task = task_builder.build_task(case)
        proposal = _recovery_aware(case)
        final_output = proposal.model_dump(mode="json", by_alias=True)
        action = {"kind": "FINAL", "final_output": final_output, "evidence_refs": []}
        request_id = f"{task.task_id}:{task.task_digest}:model:1"
        outcomes[request_id] = (
            ModelSuccess(
                request_id=request_id,
                model_id=profile.model_id,
                output=action,
                usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                response_digest=canonical_json_sha256(action),
                latency_ms=Decimal("1"),
            ),
        )
    scripted_model = ScriptedAgentModel(outcomes=outcomes)
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=scripted_model,
        budget_store=InMemoryModelBudgetStore(),
        evidence_registry=EvidenceResolverRegistry({kind: _EmptyLoader() for kind in EvidenceKind}),
        skill_port=_NoSkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda task: task.input_snapshot["anchor_id"],
        pricing_policy=_PricingPolicy(),
    )
    adapter = PlannerAgentAdapter(runner=runner, profile=profile)
    store = InMemorySpecialistEvaluationStore()
    manifest = _formal_manifest()
    authorization = _build_formal_manifest_authorization(manifest)
    store.register_manifest(manifest, authorization=authorization)
    run = EvaluationRun(
        run_id="run-planner-scripted",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=EvaluationCandidate.PLANNER,
    )
    store.create_run(run, authorization=authorization)
    claim = store.claim_next_run("planner-scripted-worker", manifest_id=manifest.manifest_id)
    assert claim is not None
    recorder = PlannerPairedEvaluationRecorder(store=store)
    baseline = RankedProductPlannerPolicy()

    for case in all_cases[:60]:
        result = asyncio.run(adapter.run_case(case))
        recorder.record_pair(
            run=run,
            claim=claim,
            case=case,
            label=labels[case["case_id"]],
            baseline=baseline.propose(case["input"]),
            agent_result=result,
        )
    assert recorder.rebuild_validation_gate(run=run).status is PlannerValidationGateStatus.HOLDOUT_UNLOCKED
    for case in all_cases[60:]:
        result = asyncio.run(adapter.run_case(case))
        recorder.record_pair(
            run=run,
            claim=claim,
            case=case,
            label=labels[case["case_id"]],
            baseline=baseline.propose(case["input"]),
            agent_result=result,
        )

    executable, recovery = recorder.save_validation_metrics(run=run, claim=claim)
    assert executable.agent_success_count == 40
    assert recovery.agent_success_count == 40
    assert recovery.delta_percentage_points >= Decimal("10")
    assert scripted_model.call_count == 80
    assert len(store.list_attempts(run.run_id)) == 160
