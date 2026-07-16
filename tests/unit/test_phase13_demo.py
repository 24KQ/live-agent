"""Phase 13 Task 12 无付费 Demo 与多 Agent 扩展接口门禁。"""

from __future__ import annotations

import pytest

from src.specialist_evaluation.demo import (
    DemoSpecialistRoute,
    build_demo_routes,
)


@pytest.mark.parametrize("retained_count", (0, 1, 2, 3))
def test_demo_routes_are_default_closed_and_only_expose_retained_profiles(retained_count: int) -> None:
    """多 Profile 预留只允许显式 SPECIALIST_AGENT 路由，默认始终是确定性路径。"""

    routes = build_demo_routes(retained_count=retained_count)

    assert len(routes) == retained_count
    assert all(route.default_mode == "DETERMINISTIC" for route in routes)
    assert all(route.specialist_mode == "SPECIALIST_AGENT" for route in routes)
    assert all(route.agent_to_agent_allowed is False for route in routes)


def test_demo_route_rejects_agent_to_agent_and_unknown_mode() -> None:
    """Phase 13 只能预留确定性编排接口，不能暗中加入自由 handoff。"""

    route = DemoSpecialistRoute(
        profile_identity="planner-agent@1.0.0",
        default_mode="DETERMINISTIC",
        specialist_mode="SPECIALIST_AGENT",
        agent_to_agent_allowed=False,
    )

    with pytest.raises(ValueError, match="agent-to-agent"):
        route.resolve("AGENT_TO_AGENT")
    with pytest.raises(ValueError, match="unknown"):
        route.resolve("RANDOM")
