"""Phase 16 Task 8 浏览器安全订阅票据的 RED/GREEN 契约。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.gateway.decision_support_subscription import (
    DecisionSupportSubscriptionTicketError,
    DecisionSupportSubscriptionTickets,
)


def test_subscription_ticket_is_single_use_and_bound_to_workspace_and_operator() -> None:
    """订阅票据只能由签发操作员在目标会话消费一次，不能变成通用 WebSocket 密钥。"""

    instant = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    tickets = DecisionSupportSubscriptionTickets(
        clock=lambda: instant,
        token_factory=lambda: "opaque-ticket-1",
    )
    ticket = tickets.issue(
        live_session_id="live-session-phase16-ticket",
        operator_id="operator-phase16-ticket",
        browser_binding="browser-binding-1",
    )

    with pytest.raises(DecisionSupportSubscriptionTicketError, match="workspace"):
        tickets.consume(
            ticket=ticket,
            live_session_id="another-session",
            browser_binding="browser-binding-1",
        )
    assert tickets.consume(
        ticket=ticket,
        live_session_id="live-session-phase16-ticket",
        browser_binding="browser-binding-1",
    ) == "operator-phase16-ticket"
    with pytest.raises(DecisionSupportSubscriptionTicketError, match="unknown or consumed"):
        tickets.consume(
            ticket=ticket,
            live_session_id="live-session-phase16-ticket",
            browser_binding="browser-binding-1",
        )


def test_subscription_ticket_expires_before_websocket_acceptance() -> None:
    """过期票据必须在 accept 前拒绝，不能因浏览器重试延长订阅能力。"""

    instant = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    clock = {"value": instant}
    tickets = DecisionSupportSubscriptionTickets(
        clock=lambda: clock["value"],
        token_factory=lambda: "opaque-ticket-expired",
    )
    ticket = tickets.issue(
        live_session_id="live-session-phase16-ticket",
        operator_id="operator-phase16-ticket",
        browser_binding="browser-binding-expired",
    )
    clock["value"] = instant + timedelta(seconds=61)

    with pytest.raises(DecisionSupportSubscriptionTicketError, match="expired"):
        tickets.consume(
            ticket=ticket,
            live_session_id="live-session-phase16-ticket",
            browser_binding="browser-binding-expired",
        )


def test_subscription_ticket_rejects_another_browser_binding_before_consumption() -> None:
    """泄露的短票据没有同源 HttpOnly binding cookie 也不能建立订阅。"""

    instant = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    tickets = DecisionSupportSubscriptionTickets(
        clock=lambda: instant,
        token_factory=lambda: "opaque-ticket-browser-bound",
    )
    ticket = tickets.issue(
        live_session_id="live-session-phase16-ticket",
        operator_id="operator-phase16-ticket",
        browser_binding="browser-binding-owner",
    )

    with pytest.raises(DecisionSupportSubscriptionTicketError, match="browser binding"):
        tickets.consume(
            ticket=ticket,
            live_session_id="live-session-phase16-ticket",
            browser_binding="browser-binding-other",
        )
    assert tickets.consume(
        ticket=ticket,
        live_session_id="live-session-phase16-ticket",
        browser_binding="browser-binding-owner",
    ) == "operator-phase16-ticket"


def test_new_browser_binding_revokes_unconsumed_tickets_for_previous_operator_session() -> None:
    """同源浏览器重新认证时，旧操作员尚未消费的票据必须立即失效。"""

    instant = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    tokens = iter(("opaque-ticket-old", "opaque-ticket-new"))
    tickets = DecisionSupportSubscriptionTickets(
        clock=lambda: instant,
        token_factory=lambda: next(tokens),
    )
    old_ticket = tickets.issue(
        live_session_id="live-session-phase16-ticket",
        operator_id="operator-a",
        browser_binding="browser-binding-a",
    )
    tickets.revoke_browser_binding("browser-binding-a")
    tickets.issue(
        live_session_id="live-session-phase16-ticket",
        operator_id="operator-b",
        browser_binding="browser-binding-b",
    )

    with pytest.raises(DecisionSupportSubscriptionTicketError, match="unknown or consumed"):
        tickets.consume(
            ticket=old_ticket,
            live_session_id="live-session-phase16-ticket",
            browser_binding="browser-binding-a",
        )
