"""Phase 15 五类受限 Subject Runner。

Runner 只接受启动时冻结的 SubjectManifest 和确定性观察适配器。它不拥有模型
选择、Skill 发现、自动回退或写入外部事实的能力；每次执行先检查域和规则，再
生成不可变 ``EvaluationCaseResult``。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from src.release_gates.dataset import GoldenCase
from src.release_gates.models import (
    EvaluationCaseResult,
    EvaluationCaseStatus,
    SubjectKind,
    SubjectManifest,
    SubjectObservation,
)
from src.release_gates.rules import RuleCode, evaluate_rules


class SubjectAdapter(Protocol):
    """确定性适配器的最小调用面；生产写入不在此接口内。"""

    def __call__(self, case: GoldenCase) -> SubjectObservation:
        """根据冻结 case 返回观察快照。"""


_EXPECTED_DOMAINS: dict[SubjectKind, frozenset[str]] = {
    SubjectKind.SKILL_RUNTIME: frozenset({"RUNTIME_SKILL"}),
    SubjectKind.PLAN_ENGINE: frozenset({"RUNTIME_PLAN"}),
    SubjectKind.EVENT_RUNTIME: frozenset({"RUNTIME_EVENT"}),
    SubjectKind.DECISION_SUPPORT: frozenset({"LIVE"}),
    SubjectKind.LIFECYCLE: frozenset({"PREPARE", "REVIEW"}),
}


class BoundedSubjectRunner:
    """按 SubjectKind 固定输入域并执行规则优先判定的共享内核。"""

    expected_kind: SubjectKind

    def __init__(self, manifest: SubjectManifest) -> None:
        if manifest.subject_kind is not self.expected_kind:
            raise ValueError("SubjectManifest kind does not match this Runner")
        self._manifest = manifest

    @property
    def manifest(self) -> SubjectManifest:
        """只读返回启动冻结的 Subject 身份。"""

        return self._manifest

    def run_case(
        self,
        case: GoldenCase,
        subject: SubjectObservation | SubjectAdapter | BaseException,
    ) -> EvaluationCaseResult:
        """执行一个 case；异常和契约错误都归一化为 BLOCKED。"""

        if case.domain not in _EXPECTED_DOMAINS[self.expected_kind]:
            return self._blocked(case, RuleCode.DOMAIN)
        try:
            if isinstance(subject, BaseException):
                raise subject
            observation = subject(case) if callable(subject) else subject
            if not isinstance(observation, SubjectObservation):
                observation = SubjectObservation.model_validate(observation)
            evaluation = evaluate_rules(case, self._manifest, observation)
            status = EvaluationCaseStatus.PASS if evaluation.passed else EvaluationCaseStatus.FAIL
            # 敏感输出不能被结果 artifact 再次持久化；其它结构化失败事实可以保留。
            safe_output = None if RuleCode.SENSITIVE_OUTPUT in evaluation.codes else observation.output
            return EvaluationCaseResult(
                case_id=case.case_id,
                subject_id=self._manifest.subject_id,
                subject_version=self._manifest.subject_version,
                status=status,
                severe_violation=evaluation.severe_violation,
                rule_codes=tuple(code.value for code in evaluation.codes),
                output=safe_output,
                evidence_refs=observation.evidence_refs,
                summary=("subject passed deterministic release rules" if evaluation.passed else "subject blocked by deterministic release rules"),
            )
        except Exception:
            # 不回显异常文本，避免将网络响应、密钥或调用栈写入评估资产。
            return self._blocked(case, RuleCode.SUBJECT_ERROR)

    def run(self, case: GoldenCase, subject: SubjectObservation | SubjectAdapter | BaseException) -> EvaluationCaseResult:
        """提供简短别名，保持本地 CLI/测试调用面的可读性。"""

        return self.run_case(case, subject)

    def _blocked(self, case: GoldenCase, code: RuleCode) -> EvaluationCaseResult:
        """生成不含未验证输出的 BLOCKED 结果。"""

        return EvaluationCaseResult(
            case_id=case.case_id,
            subject_id=self._manifest.subject_id,
            subject_version=self._manifest.subject_version,
            status=EvaluationCaseStatus.BLOCKED,
            severe_violation=True,
            rule_codes=(code.value,),
            summary="subject execution was blocked before release scoring",
        )


class SkillRuntimeRunner(BoundedSubjectRunner):
    """Skill Runtime 只消费 RUNTIME_SKILL cases。"""

    expected_kind = SubjectKind.SKILL_RUNTIME


class PlanEngineRunner(BoundedSubjectRunner):
    """PlanEngine 只消费 RUNTIME_PLAN cases。"""

    expected_kind = SubjectKind.PLAN_ENGINE


class EventRuntimeRunner(BoundedSubjectRunner):
    """事件/Replan Runtime 只消费 RUNTIME_EVENT cases。"""

    expected_kind = SubjectKind.EVENT_RUNTIME


class DecisionSupportRunner(BoundedSubjectRunner):
    """三场景人机协同支持只消费 LIVE cases，不能写入经营事实。"""

    expected_kind = SubjectKind.DECISION_SUPPORT


class LifecycleRunner(BoundedSubjectRunner):
    """播前和播后生命周期支持只消费 PREPARE/REVIEW cases。"""

    expected_kind = SubjectKind.LIFECYCLE
