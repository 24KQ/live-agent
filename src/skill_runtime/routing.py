"""Phase 11B 不可变批次路由策略。

RoutePolicy 在进程装配期从 Settings 解析一次，之后调用路径只读取冻结对象，
不再访问环境变量或可变全局配置。这样可以保证一次 Agent 工具调用不会在执行
过程中因为配置变更而从 Runtime 隐式切回 Legacy，或反向产生第二次副作用。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.skill_runtime.models import SkillExecutionRoute


RouteConfig = SkillExecutionRoute
"""路由配置枚举，与 SkillExecutionRoute 同义，LEGACY 或 SKILL_RUNTIME。"""

RouteBatch = Literal["batch1", "batch2", "batch3"]


BATCH_ONE_SKILL_IDS: frozenset[str] = frozenset(
    {
        "query_products",
        "generate_live_plan",
        "generate_product_card",
        "suggest_price_change",
        "create_live_plan_draft",
        "recommend_backup_product",
        "generate_on_live_prompt",
        "aggregate_danmaku_questions",
        "generate_danmaku_reply",
        "on_live_context_collect",
    }
)
"""批次一：只读、确定性生成和低风险播中辅助能力。"""

BATCH_TWO_SKILL_IDS: frozenset[str] = frozenset(
    {
        "setup_live_session",
        "handle_sold_out_event",
    }
)
"""批次二：建播与售罄处理，涉及审批、幂等或直播状态变化。"""

BATCH_THREE_SKILL_IDS: frozenset[str] = frozenset({"set_product_price"})
"""批次三：高风险真实改价能力，必须最后单独迁移。"""


def skill_batch_for(skill_id: str) -> RouteBatch:
    """返回 Skill 所属迁移批次，未知 Skill fail-closed。

    批次归属是阶段迁移契约的一部分，不能通过字符串前缀或生命周期推断。显式表能
    让新增 Skill 在测试中暴露遗漏，而不是默认落到某个可执行路径。
    """
    if skill_id in BATCH_ONE_SKILL_IDS:
        return "batch1"
    if skill_id in BATCH_TWO_SKILL_IDS:
        return "batch2"
    if skill_id in BATCH_THREE_SKILL_IDS:
        return "batch3"
    raise ValueError(f"未知 Skill 无法确定 Phase 11B 批次: {skill_id}")


class RoutePolicy(BaseModel):
    """进程装配期创建的不可变三批路由策略。"""

    model_config = ConfigDict(frozen=True)

    batch1: RouteConfig = Field(default=RouteConfig.LEGACY)
    batch2: RouteConfig = Field(default=RouteConfig.LEGACY)
    batch3: RouteConfig = Field(default=RouteConfig.LEGACY)

    @model_validator(mode="before")
    @classmethod
    def _accept_phase11a_aliases(cls, data: Any) -> Any:
        """兼容旧测试和旧装配代码传入 generation/setup 的构造方式。

        真实字段已经升级为 batch1/batch2/batch3；generation/setup 只读属性用于
        Phase 11A Facade 兼容。这里在模型入口做一次别名搬移，避免运行期维护两套
        状态并降低冻结策略的可信度。
        """
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        if "generation" in migrated and "batch1" not in migrated:
            migrated["batch1"] = migrated.pop("generation")
        else:
            migrated.pop("generation", None)
        if "setup" in migrated and "batch2" not in migrated:
            migrated["batch2"] = migrated.pop("setup")
        else:
            migrated.pop("setup", None)
        return migrated

    @property
    def generation(self) -> RouteConfig:
        """Phase 11A generation 只读别名，对应 Phase 11B batch1。"""
        return self.batch1

    @property
    def setup(self) -> RouteConfig:
        """Phase 11A setup 只读别名，对应 Phase 11B batch2。"""
        return self.batch2

    def route_for_skill(self, skill_id: str) -> RouteConfig:
        """按 Skill 批次读取冻结路由。"""
        batch = skill_batch_for(skill_id)
        return getattr(self, batch)

    @classmethod
    def from_settings(cls, settings: Any) -> "RoutePolicy":
        """从 Settings 构造冻结策略，并处理 Phase 11A 旧配置别名。

        新三批字段显式配置时优先使用新值；若 batch1/batch2 未显式配置，则分别
        读取旧 SKILL_ROUTE_PRELIVE_GENERATION/SETUP。batch3 没有旧别名，缺省保持
        LEGACY，防止高风险改价在迁移前被误打开。
        """
        provided = set(getattr(settings, "model_fields_set", set()))
        release_profile = getattr(settings, "phase15_route_profile", "LEGACY_DEFAULT")
        release_default = release_profile in {"EXPLICIT_RELEASE", "VERIFIED_DEFAULTS"}
        batch1 = (
            RouteConfig.SKILL_RUNTIME
            if release_default
            else settings.skill_route_phase11b_batch1
            if "skill_route_phase11b_batch1" in provided
            else settings.skill_route_prelive_generation
        )
        batch2 = (
            RouteConfig.SKILL_RUNTIME
            if release_default
            else settings.skill_route_phase11b_batch2
            if "skill_route_phase11b_batch2" in provided
            else settings.skill_route_prelive_setup
        )
        return cls(
            batch1=batch1,
            batch2=batch2,
            batch3=(RouteConfig.SKILL_RUNTIME if release_default else settings.skill_route_phase11b_batch3),
        )

    @classmethod
    def default(cls) -> "RoutePolicy":
        """返回三批全部 LEGACY 的默认策略。"""
        return cls()
