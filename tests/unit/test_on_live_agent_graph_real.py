"""Phase 5E Agent 接通本地真实服务测试。

测试 _LocalServiceExecutor:
- mock OnLiveFlowService 处理售罄事件
- mock DanmakuFlowService 处理弹幕
- 推荐备用商品返回真实 Product
- 生成主播提示返回真实 OnLivePrompt
- graph 接真服务后能正常 END
- 向后兼容: 无 service 时退回 _DefaultExecutor
"""

from __future__ import annotations

from inspect import signature
import pytest
from unittest.mock import MagicMock, patch

from src.core.on_live_agent_graph import (
    OnLiveAgentGraphState,
    build_on_live_agent_graph,
    create_initial_on_live_state,
)
from src.core.on_live_agent_graph import _LocalServiceExecutor, _DefaultExecutor


def _make_state(
    room_id: str = "room-5e-test",
    trace_id: str = "trace-5e-test",
    trust_score: float = 0.7,
    danmaku_summary=None,
    inventory_alerts=None,
) -> OnLiveAgentGraphState:
    return create_initial_on_live_state(
        room_id=room_id,
        trace_id=trace_id,
        trust_score=trust_score,
        danmaku_summary=danmaku_summary or [],
        inventory_alerts=inventory_alerts or [],
    )


class TestLocalServiceExecutor:

    def test_executor_exists(self):
        """_LocalServiceExecutor 类存在。"""
        executor = _LocalServiceExecutor()
        assert executor is not None

    def test_executor_does_not_require_llm(self):
        """_LocalServiceExecutor 不依赖 LLM，本地服务即可。"""
        executor = _LocalServiceExecutor()
        assert not hasattr(executor, "_llm") or executor._llm is None

    def test_handle_sold_out_event_returns_observation(self):
        """调用本地 OnLiveFlowService 处理售罄事件。"""
        mock_service = MagicMock()
        mock_service.handle_sold_out_event.return_value = MagicMock(
            updated_state=MagicMock(),
            backup_product=MagicMock(product_id="backup-001"),
            prompt=MagicMock(message="商品已售罄", severity="warning"),
            audit_ids=["audit-001"],
            trace_id="trace-5e-test",
        )
        executor = _LocalServiceExecutor(on_live_service=mock_service)

        from src.state.models import LiveRoomState, Product
        from decimal import Decimal
        state = LiveRoomState(
            room_id="room-5e-test",
            lifecycle="ON_LIVE",
            products=[Product(product_id="prod-001", name="测试商品", price=Decimal("10"), inventory=0)],
            current_product_id="prod-001",
        )
        result = executor.execute(
            tool_name="handle_sold_out_event",
            arguments={"product_id": "prod-001"},
            room_id="room-5e-test",
            trace_id="trace-5e-test",
            state=state,
        )

        assert result["status"] == "success"
        assert "audit-001" in str(result.get("audit_ids", []))

    def test_recommend_backup_returns_product(self):
        """调用本地 recommend_backup_product 返回真实 Product。"""
        executor = _LocalServiceExecutor()
        from src.state.models import LiveRoomState, Product
        from decimal import Decimal
        state = LiveRoomState(
            room_id="room-5e-test",
            lifecycle="ON_LIVE",
            products=[
                Product(product_id="prod-001", name="已售罄", price=Decimal("10"), inventory=0),
                Product(product_id="prod-002", name="备用商品", price=Decimal("20"), inventory=100),
            ],
            current_product_id="prod-001",
        )
        result = executor.execute(
            tool_name="recommend_backup_product",
            arguments={"sold_out_product_id": "prod-001"},
            room_id="room-5e-test",
            trace_id="trace-5e-test",
            state=state,
        )
        assert result["status"] == "success"
        assert result.get("backup_product_id") == "prod-002"

    def test_generate_on_live_prompt_returns_prompt(self):
        """调用本地 generate_sold_out_prompt 返回真实 OnLivePrompt。"""
        executor = _LocalServiceExecutor()
        from src.state.models import Product
        from decimal import Decimal
        sold_out = Product(product_id="prod-001", name="已售罄商品", price=Decimal("10"), inventory=0)
        result = executor.execute(
            tool_name="generate_on_live_prompt",
            arguments={"sold_out_product_id": "prod-001"},
            room_id="room-5e-test",
            trace_id="trace-5e-test",
            sold_out_product=sold_out,
        )
        assert result["status"] == "success"
        assert "message" in result

    def test_aggregate_danmaku_returns_groups(self):
        """调用本地 DanmakuFlowService 处理弹幕。"""
        mock_service = MagicMock()
        executor = _LocalServiceExecutor(danmaku_service=mock_service)

        from src.state.models import LiveRoomState, Product
        from decimal import Decimal
        state = LiveRoomState(
            room_id="room-5e-test",
            lifecycle="ON_LIVE",
            products=[Product(product_id="prod-001", name="测试商品", price=Decimal("10"), inventory=100)],
            current_product_id="prod-001",
        )
        result = executor.execute(
            tool_name="aggregate_danmaku_questions",
            arguments={
                "events": [
                    {"room_id": "room-5e-test", "viewer_id": "v1", "content": "多少钱", "trace_id": "trace-5e-test"}
                ]
            },
            room_id="room-5e-test",
            trace_id="trace-5e-test",
            state=state,
        )

        assert result["status"] == "success"


class TestGraphWithRealServices:

    def test_graph_uses_local_executor_when_service_provided(self):
        """传入 service 时使用 _LocalServiceExecutor。"""
        mock_on_live = MagicMock()
        executor = _LocalServiceExecutor(on_live_service=mock_on_live)
        graph = build_on_live_agent_graph(executor=executor)

        state = _make_state()
        result = graph.invoke(state)
        assert result is not None
        assert result.get("error") is None

    def test_graph_backward_compatible_without_service(self):
        """不传 service 时退回 _DefaultExecutor。"""
        graph = build_on_live_agent_graph()
        state = _make_state()
        result = graph.invoke(state)
        assert result is not None
        assert result.get("error") is None

    def test_graph_real_service_inventory_alert(self):
        """库存告警 + 真 service 能跑通。"""
        mock_on_live = MagicMock()
        executor = _LocalServiceExecutor(on_live_service=mock_on_live)

        state = _make_state(
            inventory_alerts=[
                {"product_id": "prod-001", "product_name": "测试商品", "severity": "warning"},
            ],
        )
        graph = build_on_live_agent_graph(executor=executor)
        result = graph.invoke(state)
        assert result is not None
        assert len(result.get("executed_tools", [])) > 0


class TestRuntimeOnLiveExecutor:
    """Phase 11B RuntimeOnLiveExecutor 播中兼容入口测试。"""

    def test_runtime_executor_exposes_state_parameter_in_compatibility_signature(self):
        """兼容入口应显式暴露 state，供 Graph Protocol 和静态检查识别。"""
        from src.core.on_live_agent_graph import RuntimeOnLiveExecutor

        parameter = signature(RuntimeOnLiveExecutor.execute).parameters.get("state")

        assert parameter is not None
        assert parameter.default is None

    def test_runtime_executor_keeps_sold_out_pending_without_trusted_event(self):
        """播中 Harness 不能把普通 Graph state 伪造成售罄写的可信事件授权。

        Task 6 的售罄 Skill 已收敛为 2.0.0 CAS 写。Harness 仍保留旧 dict 外观，
        但没有来自 Event Inbox 的 ``EventAuthorizationContext`` 时必须返回 pending，
        绝不能调用 Fake 平台或回退旧本地服务。
        """
        from decimal import Decimal

        from src.core.on_live_agent_graph import RuntimeOnLiveExecutor
        from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
        from src.skill_runtime.fake_platform import (
            FakeLiveCommercePlatform,
            FakePlatformFixture,
            FakePlatformProduct,
        )
        from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers

        platform = FakeLiveCommercePlatform.from_fixture(
            FakePlatformFixture(
                room_id="room-runtime-on-live",
                products=(
                    FakePlatformProduct(
                        product_id="p001",
                        name="售罄商品",
                        price=Decimal("39.90"),
                        inventory=3,
                        version=1,
                    ),
                    FakePlatformProduct(
                        product_id="p002",
                        name="备选商品",
                        price=Decimal("59.90"),
                        inventory=8,
                        version=1,
                    ),
                ),
            )
        )
        executor = RuntimeOnLiveExecutor(
            SyncSkillExecutorAdapter(
                SkillExecutor(
                    handlers=build_skill_handlers(SkillRuntimeDependencies(platform=platform))
                )
            )
        )

        result = executor.execute(
            tool_name="handle_sold_out_event",
            arguments={
                "product_id": "p001",
                "expected_version": 1,
                "idempotency_key": "idem-runtime-sold-out",
            },
            room_id="room-runtime-on-live",
            trace_id="trace-runtime-on-live",
            state={"ignored": True},
        )

        assert result["tool_name"] == "handle_sold_out_event"
        assert result["status"] == "pending"
        assert result["attempt_id"] is None
        assert result["summary"].startswith("APPROVAL_REQUIRED:")
        assert platform.product("p001").inventory == 3

    def test_runtime_executor_preserves_sanitized_error_dict_without_fallback(self):
        """Runtime 失败仍返回旧 dict 形状，且摘要不泄露原始商品参数。"""
        from decimal import Decimal

        from src.core.on_live_agent_graph import RuntimeOnLiveExecutor
        from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
        from src.skill_runtime.fake_platform import (
            FakeLiveCommercePlatform,
            FakePlatformFixture,
            FakePlatformProduct,
        )
        from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers

        platform = FakeLiveCommercePlatform.from_fixture(
            FakePlatformFixture(
                room_id="room-runtime-on-live-error",
                products=(
                    FakePlatformProduct(
                        product_id="p001",
                        name="正常商品",
                        price=Decimal("39.90"),
                        inventory=3,
                        version=1,
                    ),
                ),
            )
        )
        executor = RuntimeOnLiveExecutor(
            SyncSkillExecutorAdapter(
                SkillExecutor(
                    handlers=build_skill_handlers(SkillRuntimeDependencies(platform=platform))
                )
            )
        )
        untrusted_product_id = "missing-sensitive-product"

        result = executor.execute(
            tool_name="handle_sold_out_event",
            arguments={
                "product_id": untrusted_product_id,
                "idempotency_key": "idem-runtime-sold-out-error",
            },
            room_id="room-runtime-on-live-error",
            trace_id="trace-runtime-on-live-error",
            state={"ignored": True},
        )

        assert result["tool_name"] == "handle_sold_out_event"
        assert result["status"] == "error"
        assert "failure_category" not in result
        assert result["attempt_id"] is None
        assert result["summary"].startswith("INVALID_ARGUMENTS:")
        assert untrusted_product_id not in str(result)
        # 不存在 Runtime -> Legacy fallback；失败调用不能凭空改动 Fake 平台状态。
        assert platform.product("p001").inventory == 3
