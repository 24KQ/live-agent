"""由 Skill Catalog 生成的启动冻结治理策略视图。

该模块不维护独立元数据。它只把 ``SkillManifest`` 中执行治理需要的字段投影为
不可变快照，供 Hook、Policy、Planner、Flow 和 Executor 在后续迁移任务中统一查询。
ToolRegistry Facade 在 Phase 14 删除前仍可兼容旧调用，但新代码不得依赖它。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from src.core.security_hooks import GateDecision
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.models import AuthorizationRequirement, SkillManifest
from src.state.models import LifecycleStage, RiskLevel


class SkillPolicyNotFoundError(KeyError):
    """请求了 Catalog 快照中不存在的 Skill，调用方必须 fail-closed。"""


@dataclass(frozen=True, slots=True)
class SkillPolicy:
    """单个 Skill 的只读治理字段投影。

    ``parameter_schema`` 直接引用 Manifest 已经深度冻结的 ``FrozenDict``，因此既不
    复制出第二份可写契约，也不会把内部可变引用泄漏给调用方。
    """

    skill_id: str
    version: str
    lifecycle: frozenset[LifecycleStage]
    risk_level: RiskLevel
    parameter_schema: dict[str, Any]
    gate_decision: GateDecision
    requires_idempotency_key: bool
    authorization_requirement: AuthorizationRequirement


@dataclass(frozen=True, slots=True, init=False)
class SkillPolicyView:
    """从一组 Manifest 一次性构建、启动后不再变化的策略查询视图。"""

    _policies: Mapping[str, SkillPolicy]
    _skill_ids: tuple[str, ...]

    def __init__(self, manifests: Sequence[SkillManifest]) -> None:
        policies: dict[str, SkillPolicy] = {}
        for manifest in manifests:
            if manifest.skill_id in policies:
                raise ValueError(f"SkillPolicyView 存在重复 skill_id: {manifest.skill_id}")
            policies[manifest.skill_id] = SkillPolicy(
                skill_id=manifest.skill_id,
                version=manifest.version,
                lifecycle=manifest.lifecycle,
                risk_level=manifest.risk_level,
                parameter_schema=manifest.parameter_schema,
                gate_decision=manifest.gate_decision,
                requires_idempotency_key=manifest.requires_idempotency_key,
                authorization_requirement=manifest.authorization_requirement,
            )

        # MappingProxyType 封闭内部字典，避免调试代码或未来辅助方法意外写入。
        object.__setattr__(self, "_policies", MappingProxyType(policies))
        object.__setattr__(self, "_skill_ids", tuple(sorted(policies)))

    def get(self, skill_id: str) -> SkillPolicy:
        """按 ID 返回冻结策略；未知能力不提供默认放行策略。"""
        try:
            return self._policies[skill_id]
        except KeyError as exc:
            raise SkillPolicyNotFoundError(skill_id) from exc

    def skill_ids(self) -> tuple[str, ...]:
        """返回稳定排序的只读 ID 元组，便于启动审计和测试复核。"""
        return self._skill_ids

    def is_available(self, skill_id: str, lifecycle: LifecycleStage) -> bool:
        """按 Manifest 生命周期判断可用性；未知 Skill 继续 fail-closed。"""
        return lifecycle in self.get(skill_id).lifecycle


def get_default_skill_policy_view() -> SkillPolicyView:
    """从默认 Catalog 创建一个新的启动冻结策略视图。"""
    return SkillPolicyView(get_default_skill_catalog())


def assert_policy_view_matches_catalog(
    manifests: Sequence[SkillManifest],
    policy_view: SkillPolicyView,
) -> None:
    """验证策略视图与 Catalog 的全部执行治理字段完全一致。

    Catalog 决定业务契约，PolicyView 决定调用前治理。如果两者仅 ID/版本相同但
    生命周期、Schema 或门禁不同，调用就可能使用错误安全边界；因此在启动装配时
    比较完整投影，并在任何 Handler 或 Attempt 发生前拒绝漂移。
    """

    catalog_governance = {
        manifest.skill_id: (
            manifest.version,
            manifest.lifecycle,
            manifest.risk_level,
            manifest.parameter_schema,
            manifest.gate_decision,
            manifest.requires_idempotency_key,
            manifest.authorization_requirement,
        )
        for manifest in manifests
    }
    policy_governance = {
        skill_id: (
            policy.version,
            policy.lifecycle,
            policy.risk_level,
            policy.parameter_schema,
            policy.gate_decision,
            policy.requires_idempotency_key,
            policy.authorization_requirement,
        )
        for skill_id in policy_view.skill_ids()
        for policy in (policy_view.get(skill_id),)
    }
    if policy_governance != catalog_governance:
        raise ValueError("Skill Catalog and SkillPolicyView governance do not match")
