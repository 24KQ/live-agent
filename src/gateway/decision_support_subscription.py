"""Phase 16 浏览器到 Decision Support WebSocket 的短时订阅票据。

浏览器原生 WebSocket 无法携带现有的操作员认证头。此模块只在已经通过 REST
认证后签发一次性握手票据；它不存储 Workspace、证据、模型结果或经营命令，也不
替代任何 HTTP 写操作的 Token、lease、CAS、fencing 或 OperatorDecision 校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from threading import RLock
from typing import Callable


SUBSCRIPTION_TICKET_TTL_SECONDS = 60
"""票据仅用于一次 WebSocket 握手，过期窗口必须短于任何运营锁租约。"""

WEBSOCKET_TICKET_SUBPROTOCOL_PREFIX = "liveagent.ticket."
"""浏览器和服务端共同冻结的无长期凭据 subprotocol 前缀。"""

DECISION_SUPPORT_BROWSER_BINDING_COOKIE = "liveagent_decision_support_binding"
"""仅发送到决策 WebSocket 的 HttpOnly 同源票据绑定 cookie 名称。"""


class DecisionSupportSubscriptionTicketError(ValueError):
    """票据未知、过期、重复使用或与目标 Workspace 不匹配时的 fail-closed 错误。"""


@dataclass(frozen=True)
class _SubscriptionTicket:
    """只保存握手所需的最小绑定，避免把业务投影或认证 Token 留在内存注册表。"""

    live_session_id: str
    operator_id: str
    browser_binding: str
    expires_at: datetime


class DecisionSupportSubscriptionTickets:
    """以进程内互斥锁原子签发和消费短时订阅票据。

    Phase 16 只支持单进程本地演示。服务重启时该注册表自然清空，客户端必须重新以
    REST 操作员认证申请票据，因此重启不会恢复或扩大任何订阅权限。多节点部署必须
    在独立设计中替换为签名或共享、可撤销的票据存储。
    """

    __slots__ = ("_clock", "_lock", "_tickets", "_token_factory")

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._token_factory = token_factory or (lambda: token_urlsafe(32))
        self._lock = RLock()
        self._tickets: dict[str, _SubscriptionTicket] = {}

    def issue(
        self,
        *,
        live_session_id: str,
        operator_id: str,
        browser_binding: str,
    ) -> str:
        """为已认证操作员及其同源浏览器签发随机票据，不接受浏览器自选票据值。"""

        if not live_session_id or not operator_id or not browser_binding:
            raise ValueError("subscription ticket scope must not be empty")
        instant = self._utc_now()
        with self._lock:
            self._discard_expired(instant)
            ticket = self._token_factory()
            if not ticket or ticket in self._tickets:
                raise DecisionSupportSubscriptionTicketError(
                    "subscription ticket generation failed"
                )
            self._tickets[ticket] = _SubscriptionTicket(
                live_session_id=live_session_id,
                operator_id=operator_id,
                browser_binding=browser_binding,
                expires_at=instant + timedelta(seconds=SUBSCRIPTION_TICKET_TTL_SECONDS),
            )
            return ticket

    def consume(
        self,
        *,
        ticket: str,
        live_session_id: str,
        browser_binding: str,
    ) -> str:
        """在 WebSocket accept 前原子消费票据，拒绝跨 session、跨浏览器、过期和重放。"""

        if not ticket or not live_session_id or not browser_binding:
            raise DecisionSupportSubscriptionTicketError("subscription ticket is required")
        instant = self._utc_now()
        with self._lock:
            record = self._tickets.get(ticket)
            if record is None:
                raise DecisionSupportSubscriptionTicketError(
                    "subscription ticket is unknown or consumed"
                )
            if instant >= record.expires_at:
                del self._tickets[ticket]
                raise DecisionSupportSubscriptionTicketError("subscription ticket is expired")
            if record.live_session_id != live_session_id:
                raise DecisionSupportSubscriptionTicketError(
                    "subscription ticket workspace does not match"
                )
            if record.browser_binding != browser_binding:
                raise DecisionSupportSubscriptionTicketError(
                    "subscription ticket browser binding does not match"
                )
            # 删除在返回身份之前完成，确保两个并发握手中只有一个能订阅同一 Workspace。
            del self._tickets[ticket]
            return record.operator_id

    def revoke_browser_binding(self, browser_binding: str) -> None:
        """撤销同源浏览器上一身份尚未消费的票据，收紧操作员切换窗口。

        已经建立的 WebSocket 不会因本地单进程注册表删除而被主动断开，因为 Phase 16
        的订阅通道只读且尚未具备连接级撤销协议；本方法只确保旧身份手中的未消费
        ticket 无法在新的 REST 认证完成后继续完成握手。
        """

        if not browser_binding:
            return
        with self._lock:
            for ticket, record in tuple(self._tickets.items()):
                if record.browser_binding == browser_binding:
                    del self._tickets[ticket]

    def _utc_now(self) -> datetime:
        """统一验证可感知时区的时钟，避免测试或配置注入裸时间扩大票据寿命。"""

        instant = self._clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("subscription ticket clock must be timezone-aware")
        return instant.astimezone(timezone.utc)

    def _discard_expired(self, instant: datetime) -> None:
        """签发时惰性清理过期票据，限制本地演示进程的无界内存增长。"""

        for ticket, record in tuple(self._tickets.items()):
            if instant >= record.expires_at:
                del self._tickets[ticket]
