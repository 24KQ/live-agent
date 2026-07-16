"""Phase 13 ReviewMemoryAgent 的受限结构化输出与冻结 Profile 装配。"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

from pydantic import ConfigDict, Field

from src.specialist_runtime.models import StrictFrozenModel, SpecialistTaskKind
from src.specialist_runtime.models import AgentResult, AgentResultStatus, AgentTask, EvidenceRef
from src.specialist_runtime.profiles import SpecialistProfile


class ReviewAttribution(StrictFrozenModel):
    """复盘归因只保留类别、稳定原因码和至少两条证据身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    category: str = Field(..., min_length=1)
    reason_code: str = Field(..., min_length=1)
    evidence_ids: tuple[str, ...] = Field(..., min_length=2)


class ReviewMemoryCandidate(StrictFrozenModel):
    """模型候选不包含正文、置信度覆盖或 active-memory 写入控制字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    candidate_class: str = Field(..., alias="class", pattern="^(APPLY|REJECT|REVIEW)$")
    product_id: str = Field(..., pattern=r"^sim-product-")
    category: str = Field(..., min_length=1)
    tag: str = Field(..., min_length=1)
    evidence_ids: tuple[str, ...] = Field(..., min_length=2)


class ReviewMemoryRecommendation(StrictFrozenModel):
    """共享 Runner 的 FINAL 输出，只能建议候选，不能直接写长期记忆。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    attribution: ReviewAttribution
    # 当前每个冻结评估 case 只有一个 gold candidate class；限制为单个候选，避免模型
    # 并列输出 APPLY/REJECT/REVIEW 来规避三分类 macro-F1 的确定性评分。
    memory_candidates: tuple[ReviewMemoryCandidate, ...] = Field(..., min_length=1, max_length=1)
    evidence_ids: tuple[str, ...] = Field(..., min_length=2)


def build_review_memory_profile(evaluation_root: Path) -> SpecialistProfile:
    """从冻结 Dataset Manifest 构造三播后 Skill、3 模型调用的 Review Profile。"""

    root = Path(evaluation_root)
    manifest = json.loads((root / "manifests" / "phase13-v2.json").read_text(encoding="utf-8"))
    facts = manifest["profiles"]["review_memory"]
    result_schema = json.loads((root / facts["result_schema_path"]).read_text(encoding="utf-8"))
    return SpecialistProfile(
        profile_id=facts["profile_id"], profile_version=facts["profile_version"], task_kind=SpecialistTaskKind(facts["task_kind"]), model_id=facts["model_id"], endpoint_host=facts["endpoint_host"], temperature=Decimal(facts["temperature"]), prompt_text=facts["prompt_text"], prompt_hash=facts["prompt_digest"], result_schema_hash=facts["result_schema_digest"], result_schema=result_schema, allowed_skill_ids=tuple(facts["allowed_skill_ids"]), skill_versions=facts["skill_versions"], max_model_calls=facts["max_model_calls"], max_skill_calls=facts["max_skill_calls"], max_total_tokens=facts["max_total_tokens"], deadline_seconds=facts["deadline_seconds"], max_case_cost_cny=Decimal(facts["max_case_cost_cny"]),
    )


class ReviewMemoryBaseline:
    """确定性基线只根据冻结 replay/trace/白名单生成同一受限输出结构。"""

    def decide(self, case_input: dict) -> ReviewMemoryRecommendation:
        traces = case_input["decision_traces"]
        evidence_ids = tuple(item["evidence_id"] for item in traces[:2])
        # Replay 的 dominant_signal 与本任务 gold 归因类别同源；若基线直接复制它，
        # 便会泄漏评估答案并把相对提升门变成数学上不可能满足的 100% 基线。这里固定
        # 使用可审计的库存优先规则，保留一个真实、可重复且不读取标签的确定性对照。
        category = "inventory"
        product_id = case_input["catalog_whitelist"]["product_ids"][0]
        candidate_category = case_input["catalog_whitelist"]["categories"][0]
        tag = case_input["catalog_whitelist"]["tags"][0]
        candidate_class = "APPLY" if case_input["candidate_context"]["whitelist_match"] else "REVIEW"
        return ReviewMemoryRecommendation(
            attribution=ReviewAttribution(category=category, reason_code="DETERMINISTIC_INVENTORY_PRIOR", evidence_ids=evidence_ids),
            memory_candidates=(ReviewMemoryCandidate(**{"class": candidate_class, "product_id": product_id, "category": candidate_category, "tag": tag, "evidence_ids": evidence_ids}),),
            evidence_ids=evidence_ids,
        )


class ReviewMemoryAgentAdapter:
    """将冻结 review case 映射为一次共享 Runner 调用，不提供 Agent 间调用或 fallback。"""

    def __init__(self, *, runner, profile: SpecialistProfile) -> None:
        if profile.task_kind is not SpecialistTaskKind.POST_LIVE_REVIEW:
            raise ValueError("ReviewMemory adapter requires POST_LIVE_REVIEW profile")
        self._runner, self._profile = runner, profile

    def build_task(self, case: dict) -> AgentTask:
        if case.get("candidate") != "review_memory":
            raise ValueError("ReviewMemory adapter requires review_memory case")
        data = case["input"]
        refs = tuple(EvidenceRef.model_validate(item) for item in data["decision_traces"])
        return AgentTask(task_id=f"review-memory:{case['case_id']}", task_kind=SpecialistTaskKind.POST_LIVE_REVIEW, profile_id=self._profile.profile_id, profile_version=self._profile.profile_version, room_id=data["room_id"], trace_id=data["replay"]["trace_id"], objective="Return grounded structured review and staged candidates.", input_snapshot=data, initial_evidence_refs=refs, evaluation_case_id=case["case_id"])

    async def run_case(self, case: dict) -> AgentResult:
        """执行一次受限 ReviewMemory 任务，并拒绝错绑 Task/Profile 的 Runner 结果。"""

        task = self.build_task(case)
        result: AgentResult = await self._runner.run(task)
        # adapter 是 case 与共享 Runner 的唯一接缝；结果身份若错绑，不能进入配对评估 Store。
        if (
            result.task_id != task.task_id
            or result.profile_id != task.profile_id
            or result.profile_version != task.profile_version
        ):
            raise ValueError("ReviewMemory Runner returned mismatched task identity")
        return result

    async def recommendation_for_case(self, case: dict) -> ReviewMemoryRecommendation:
        result = await self.run_case(case)
        if result.status is not AgentResultStatus.SUCCEEDED:
            raise ValueError("ReviewMemory Agent did not produce a successful result")
        return ReviewMemoryRecommendation.model_validate(result.output)
