"""Phase 11A 不可变路由策略。

RoutePolicy 在装配时创建，调用开始后不读取可变全局状态。
两个批次（generation 与 setup）可以独立切换。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.skill_runtime.models import SkillExecutionRoute


RouteConfig = SkillExecutionRoute
"""路由配置枚举，与 SkillExecutionRoute 同义，LEGACY 或 SKILL_RUNTIME。"""


class RoutePolicy(BaseModel):
    """进程装配期创建的不可变批次路由策略。"""

    model_config = ConfigDict(frozen=True)

    generation: RouteConfig = RouteConfig.LEGACY
    setup: RouteConfig = RouteConfig.LEGACY

    @classmethod
    def default(cls) -> "RoutePolicy":
        """返回全部 LEGACY 的默认策略。"""
        return cls()
