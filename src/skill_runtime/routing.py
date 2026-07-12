"""Phase 11A 不可变路由策略。

RoutePolicy 在装配时创建，调用开始后不读取可变全局状态。
两个批次（generation 与 setup）可以独立切换。
"""

from __future__ import annotations

from src.skill_runtime.models import SkillExecutionRoute


RouteConfig = SkillExecutionRoute
"""路由配置枚举，与 SkillExecutionRoute 同义，LEGACY 或 SKILL_RUNTIME。"""


class RoutePolicy:
    """不可变路由策略。构造后不可修改。"""

    def __init__(
        self,
        generation: RouteConfig = RouteConfig.LEGACY,
        setup: RouteConfig = RouteConfig.LEGACY,
    ) -> None:
        self._generation = generation
        self._setup = setup

    @property
    def generation(self) -> RouteConfig:
        """generation 批次路由（query_products, generate_live_plan, generate_product_card）。"""
        return self._generation

    @property
    def setup(self) -> RouteConfig:
        """setup 批次路由（setup_live_session）。"""
        return self._setup

    @classmethod
    def default(cls) -> "RoutePolicy":
        """返回全部 LEGACY 的默认策略。"""
        return cls()

    def __repr__(self) -> str:
        return f"RoutePolicy(generation={self._generation}, setup={self._setup})"
