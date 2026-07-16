"""Phase 13 无付费 Demo 的默认关闭多 Profile 路由投影。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DemoSpecialistRoute:
    """展示未来受控多 Agent 扩展接口，不执行 Agent 或修改生产配置。"""

    profile_identity: str
    default_mode: str
    specialist_mode: str
    agent_to_agent_allowed: bool

    def resolve(self, mode: str) -> str:
        """只解析确定性或显式 Specialist 模式，所有自由 handoff 一律拒绝。"""

        if mode == "AGENT_TO_AGENT":
            raise ValueError("agent-to-agent handoff is forbidden")
        if mode not in {self.default_mode, self.specialist_mode}:
            raise ValueError("unknown specialist route mode")
        return self.profile_identity if mode == self.specialist_mode else "DETERMINISTIC_BASELINE"


def build_demo_routes(*, retained_count: int) -> tuple[DemoSpecialistRoute, ...]:
    """按固定候选顺序投影 0-3 个已保留 Profile，默认仍始终走确定性路径。"""

    identities = (
        "live-ops-agent@1.0.0",
        "planner-agent@1.0.0",
        "review-memory-agent@1.0.0",
    )
    if retained_count not in range(0, len(identities) + 1):
        raise ValueError("retained_count must be between 0 and 3")
    return tuple(
        DemoSpecialistRoute(
            profile_identity=identity,
            default_mode="DETERMINISTIC",
            specialist_mode="SPECIALIST_AGENT",
            agent_to_agent_allowed=False,
        )
        for identity in identities[:retained_count]
    )
