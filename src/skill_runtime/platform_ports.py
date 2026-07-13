"""Phase 11B 业务域 Platform Port 定义。

Port 只描述 Handler 读取或修改直播业务状态的边界，纯确定性排品、手卡和文案
不应为了形式统一而伪造平台调用。每个方法都是 async 单次尝试，不拥有重试策略。
"""

from __future__ import annotations

from typing import Protocol, TypeAlias

from src.skill_runtime.models import AdapterRequest, AdapterSuccess, FailureFact


AdapterResult: TypeAlias = AdapterSuccess | FailureFact
"""业务域 Port 的唯一返回形态：确认成功事实或结构化失败事实。"""


class ProductPricingPort(Protocol):
    """商品快照读取与带版本价格写入边界。"""

    async def list_products(self, request: AdapterRequest) -> AdapterResult:
        """读取可信商品快照。"""

    async def set_price(self, request: AdapterRequest) -> AdapterResult:
        """按 expected_version 执行比较并交换式改价。"""


class LiveSessionPort(Protocol):
    """建播会话的幂等准备与查询边界。"""

    async def prepare_session(self, request: AdapterRequest) -> AdapterResult:
        """准备或重放一个直播会话。"""


class LiveOperationsPort(Protocol):
    """售罄、备选商品和播中上下文状态边界。"""

    async def mark_sold_out(self, request: AdapterRequest) -> AdapterResult:
        """把指定商品标记为售罄并返回更新后的业务事实。"""

    async def resolve_product_context(self, request: AdapterRequest) -> AdapterResult:
        """只读解析售罄商品与可选备选商品的可信快照。"""

    async def current_context(self, request: AdapterRequest) -> AdapterResult:
        """读取播中弹幕摘要和库存告警所需的可信状态。"""
