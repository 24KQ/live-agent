"""无外部依赖的确定性 AgentModelPort。"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence

from src.specialist_runtime.model_port import ModelOutcome, ModelRequest


class ScriptedAgentModel:
    """按 request_id 消费固定结果，用于开发集和失败路径测试。"""

    def __init__(self, *, outcomes: Mapping[str, Sequence[ModelOutcome]]) -> None:
        # 复制外部序列，避免测试或 Fixture 在运行中改写尚未消费的结果。
        self._outcomes = {
            request_id: deque(sequence)
            for request_id, sequence in outcomes.items()
        }
        self._call_count = 0

    @property
    def call_count(self) -> int:
        """返回已经消费的单次尝试数量。"""

        return self._call_count

    async def complete(self, request: ModelRequest) -> ModelOutcome:
        """返回下一个固定结果；序列耗尽时显式失败而不是重复末项。"""

        queue = self._outcomes.get(request.request_id)
        if not queue:
            raise RuntimeError(f"script exhausted for request: {request.request_id}")
        outcome = queue[0]
        if outcome.request_id != request.request_id:
            raise RuntimeError("scripted outcome request_id does not match request")
        queue.popleft()
        self._call_count += 1
        return outcome
