"""Phase 14 Task 8 三视图运营工作台的无浏览器契约测试。"""

from __future__ import annotations

from pathlib import Path


INDEX = Path(__file__).parents[2] / "front" / "index.html"


def _html() -> str:
    """以 UTF-8 读取实际入口，避免测试替换或复制前端资产。"""

    return INDEX.read_text(encoding="utf-8")


def test_dashboard_has_one_session_and_three_fixed_views() -> None:
    """同一 live_session_id 必须贯穿 PREPARE/LIVE/REVIEW 三个视图。"""

    html = _html()
    for view in ("PREPARE", "LIVE", "REVIEW"):
        assert f'data-view="{view}"' in html
    assert "live-session-id" in html
    assert "workspace-panel" in html
    assert "switchView" in html


def test_live_view_contains_structured_proposal_and_operator_controls() -> None:
    """播中必须展示事实、风险、1-3 个方案和结构化运营决定入口。"""

    html = _html()
    required_tokens = (
        "evidence-list",
        "risk-list",
        "decision-options",
        "operator-controls",
        "backup_product_id",
        "host_prompt",
        "priority",
        "timing",
        "submitDecision",
    )
    for token in required_tokens:
        assert token in html


def test_presenter_surface_is_read_only_and_separate_from_operator_controls() -> None:
    """主播提示区不能包含批准/拒绝/修改按钮，运营区才拥有决定控件。"""

    html = _html()
    presenter_start = html.index('id="presenter-readonly"')
    presenter_end = html.index("</section>", presenter_start)
    presenter = html[presenter_start:presenter_end]
    assert "presenter" in presenter.lower()
    assert "submitDecision" not in presenter
    assert "decision-action" not in presenter
    assert 'id="operator-controls"' in html


def test_dashboard_handles_degraded_reconciliation_and_reconnect_states() -> None:
    """模型失败、未知副作用和 WebSocket 重连必须是显式 UI 状态。"""

    html = _html()
    for token in (
        "DEGRADED",
        "WAITING_RECONCILIATION",
        "RECONNECTING",
        "decision_support_workspace_update",
        "/api/decision-support/workspaces/",
        "/proposals",
        "/decisions",
        "decisionBlockReason",
        "setDecisionControls",
        "X-Operator-Token",
        "requestSequence",
        "state.dataState = \"READY\"",
        "connectionState !== \"CONNECTED\"",
        "decisionKind !== \"REJECT\"",
    ):
        assert token in html


def test_dashboard_uses_governed_proposal_sync_and_replay_surfaces() -> None:
    """方案同步、方案选择和播后候选必须使用受治理的持久化事实。"""

    html = _html()
    for token in (
        "appendCurrentProposal",
        "PROPOSAL_SUFFIX",
        "selectedOptionId",
        "closeDecisionSupportSocket",
        "memory_candidates",
        "未生成执行命令",
    ):
        assert token in html


def test_dashboard_uses_safe_text_rendering_and_mobile_layout_hooks() -> None:
    """动态证据和提示必须经过转义，视图在窄屏时仍能收缩而不横向溢出。"""

    html = _html()
    assert "escapeHtml" in html
    assert "@media" in html
    assert "min-width: 0" in html
    assert "overflow-wrap" in html or "word-break" in html
