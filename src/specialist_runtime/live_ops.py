"""Phase 13 LiveOpsAgent 的受限输出、确定性 baseline 与 Profile 装配。"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import ConfigDict, Field

from src.specialist_runtime.models import (
    AgentResult,
    AgentResultStatus,
    AgentTask,
    EvidenceRef,
    StrictFrozenModel,
    SpecialistTaskKind,
)
from src.specialist_runtime.profiles import SpecialistProfile


class LiveOpsAction(StrEnum):
    """LiveOpsAgent 唯一允许返回的四类建议动作。"""

    NO_ACTION = "NO_ACTION"
    HUMAN_ATTENTION = "HUMAN_ATTENTION"
    SWITCH_PRODUCT_SUGGESTION = "SWITCH_PRODUCT_SUGGESTION"
    DANMAKU_REPLY_SUGGESTION = "DANMAKU_REPLY_SUGGESTION"


class LiveOpsSuggestion(StrictFrozenModel):
    """不含执行授权和高风险写意图的结构化播中建议。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: LiveOpsAction
    reason_code: str = Field(..., min_length=1)
    suggestion: str = Field(..., min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(..., min_length=1)


class PriorityLiveOpsPolicy:
    """以风险优先、备品次之、弹幕再次之的固定规则生成 baseline。"""

    def decide(self, case_input: dict) -> LiveOpsSuggestion:
        """只消费冻结 case 快照，不查询 Store 或执行任何 Skill。"""

        alert = case_input["inventory_alert"]
        danmaku = case_input["danmaku"]
        evidence_refs = tuple(
            EvidenceRef.model_validate(item) for item in case_input["evidence_refs"]
        )
        if alert["risk_open"] and alert["backup_available"]:
            action = LiveOpsAction.SWITCH_PRODUCT_SUGGESTION
            reason_code = "OPEN_RISK_WITH_BACKUP"
            suggestion = "SUGGEST_SWITCH_TO_VERIFIED_BACKUP"
        elif alert["risk_open"]:
            action = LiveOpsAction.HUMAN_ATTENTION
            reason_code = "OPEN_RISK_REQUIRES_HUMAN"
            suggestion = "REQUEST_HUMAN_ATTENTION"
        elif danmaku["question_count"] >= 10:
            action = LiveOpsAction.DANMAKU_REPLY_SUGGESTION
            reason_code = "HIGH_QUESTION_VOLUME"
            suggestion = "SUGGEST_GOVERNED_DANMAKU_REPLY"
        else:
            action = LiveOpsAction.NO_ACTION
            reason_code = "NO_OPEN_INCIDENT"
            suggestion = "NO_ACTION_REQUIRED"
        return LiveOpsSuggestion(
            action=action,
            reason_code=reason_code,
            suggestion=suggestion,
            evidence_refs=evidence_refs,
        )


class _SpecialistRunner(Protocol):
    """LiveOps adapter 依赖的最小 Runner 协议，避免引入第二套执行循环。"""

    async def run(self, task: AgentTask) -> AgentResult:
        """执行一个已经冻结的 Specialist 任务。"""


class LiveOpsAgentAdapter:
    """把冻结 LiveOps case 映射为唯一受限 Specialist Runner 调用。"""

    def __init__(self, *, runner: _SpecialistRunner, profile: SpecialistProfile) -> None:
        if profile.task_kind is not SpecialistTaskKind.LIVE_OPS_ADVICE:
            raise ValueError("LiveOps adapter requires LIVE_OPS_ADVICE profile")
        self._runner = runner
        self._profile = profile

    def build_task(self, case: dict[str, Any]) -> AgentTask:
        """仅从冻结 case 复制业务快照和 EvidenceRef，执行控制仍由 Runner 注入。"""

        if case.get("candidate") != "live_ops":
            # 数据集候选身份是 Profile/预算/去留事实的一部分；不能靠下游 Store 再纠正错投任务。
            raise ValueError("LiveOps adapter requires live_ops candidate case")
        case_id = case.get("case_id")
        case_input = case.get("input")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("LiveOps case requires case_id")
        if not isinstance(case_input, dict):
            raise ValueError("LiveOps case requires object input")
        room_id = case_input.get("room_id")
        trace_id = case_input.get("trace_id")
        raw_evidence = case_input.get("evidence_refs")
        if (
            not isinstance(room_id, str)
            or not room_id
            or not isinstance(trace_id, str)
            or not trace_id
            or not isinstance(raw_evidence, list)
            or not raw_evidence
        ):
            raise ValueError("LiveOps case lacks trusted room, trace or evidence")
        # case_id 是评估重跑的稳定身份，因此 task_id 不能掺入时间戳、随机数或模型请求号。
        return AgentTask(
            task_id=f"live-ops:{case_id}",
            task_kind=SpecialistTaskKind.LIVE_OPS_ADVICE,
            profile_id=self._profile.profile_id,
            profile_version=self._profile.profile_version,
            room_id=room_id,
            trace_id=trace_id,
            objective="Generate one governed live operations suggestion from supplied evidence.",
            input_snapshot=case_input,
            initial_evidence_refs=tuple(
                EvidenceRef.model_validate(item) for item in raw_evidence
            ),
            evaluation_case_id=case_id,
        )

    async def run_case(self, case: dict[str, Any]) -> AgentResult:
        """运行一次受限 Agent；评估路径不提供 fallback 或重试。"""

        task = self.build_task(case)
        result = await self._runner.run(task)
        # adapter 是 case 与 Runner 之间唯一边界，必须拒绝把别的任务或 Profile 的结果写入本 case。
        if (
            result.task_id != task.task_id
            or result.profile_id != task.profile_id
            or result.profile_version != task.profile_version
        ):
            raise ValueError("LiveOps Runner returned mismatched task identity")
        return result

    async def suggestion_for_case(self, case: dict[str, Any]) -> LiveOpsSuggestion:
        """只把成功结果解释为领域建议，失败保持失败而不伪装成 baseline 成功。"""

        result = await self.run_case(case)
        if result.status is not AgentResultStatus.SUCCEEDED:
            raise ValueError("LiveOps Agent did not produce a successful suggestion")
        try:
            return LiveOpsSuggestion.model_validate(result.output)
        except Exception as error:
            raise ValueError("LiveOps Runner output is not a strict suggestion") from error


def build_live_ops_profile(evaluation_root: Path) -> SpecialistProfile:
    """从 Task 6 数据集基线 Manifest 构造精确 LiveOps Profile。"""

    root = Path(evaluation_root)
    manifest = json.loads(
        (root / "manifests" / "phase13-v2.json").read_text(encoding="utf-8")
    )
    facts = manifest["profiles"]["live_ops"]
    result_schema = json.loads(
        (root / facts["result_schema_path"]).read_text(encoding="utf-8")
    )
    return SpecialistProfile(
        profile_id=facts["profile_id"],
        profile_version=facts["profile_version"],
        task_kind=SpecialistTaskKind(facts["task_kind"]),
        model_id=facts["model_id"],
        endpoint_host=facts["endpoint_host"],
        temperature=Decimal(facts["temperature"]),
        prompt_text=facts["prompt_text"],
        prompt_hash=facts["prompt_digest"],
        result_schema_hash=facts["result_schema_digest"],
        result_schema=result_schema,
        allowed_skill_ids=tuple(facts["allowed_skill_ids"]),
        skill_versions=facts["skill_versions"],
        max_model_calls=facts["max_model_calls"],
        max_skill_calls=facts["max_skill_calls"],
        max_total_tokens=facts["max_total_tokens"],
        deadline_seconds=facts["deadline_seconds"],
        max_case_cost_cny=Decimal(facts["max_case_cost_cny"]),
    )
