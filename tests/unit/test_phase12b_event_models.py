"""Phase 12B 售罄事件、可信来源和事件授权公共契约测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib
import json
from typing import Any

import pytest
from pydantic import ValidationError


def _events() -> Any:
    """延迟导入事件模块，使缺失实现形成可读 RED。"""
    try:
        return importlib.import_module("src.plan_engine.events")
    except ModuleNotFoundError:
        pytest.fail("尚未实现 Phase 12B 事件公共模型", pytrace=False)


def _event(**overrides: Any) -> Any:
    """构造一个固定售罄事实，测试可按字段覆盖异常边界。"""
    values = {
        "event_id": "event-001",
        "room_id": "room-001",
        "product_id": "product-001",
        "observed_version": 3,
        "occurred_at": datetime(
            2026,
            7,
            15,
            16,
            0,
            tzinfo=timezone(timedelta(hours=8)),
        ),
        "source": "inventory-service",
    }
    values.update(overrides)
    return _events().InventoryFactEvent.create_sold_out(**values)


def _provenance(event: Any, **overrides: Any) -> Any:
    """构造与事件摘要闭合的已验证入站来源。"""
    values = {
        "provenance_id": "provenance-001",
        "profile_id": "inventory-kafka-v1",
        "transport": "KAFKA",
        "topic": "live-inventory",
        "source": event.source,
        "received_at": datetime(2026, 7, 15, 8, 0, 1, tzinfo=timezone.utc),
        "payload_digest": event.payload_digest,
    }
    values.update(overrides)
    return _events().VerifiedIngressProvenance(**values)


def test_canonical_json_digest_is_stable_and_uses_compact_utf8() -> None:
    """key 顺序不影响摘要，中文按 UTF-8 原文编码且不插入多余空格。"""
    module = _events()
    left = {"b": 2, "a": ["中文", True, None, 1.5]}
    right = {"a": ["中文", True, None, 1.5], "b": 2}
    encoded = json.dumps(
        right,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    assert module.canonical_json_sha256(left) == sha256(encoded).hexdigest()
    assert module.canonical_json_sha256(left) == module.canonical_json_sha256(right)


@pytest.mark.parametrize(
    "value",
    [
        {1: "non-string-key"},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": ("tuple",)},
        {"value": {"set"}},
        {"value": b"bytes"},
    ],
)
def test_canonical_json_rejects_non_json_or_ambiguous_values(value: Any) -> None:
    """摘要边界必须拒绝 JSON 编码器可能隐式转换或非有限的值。"""
    with pytest.raises((TypeError, ValueError)):
        _events().canonical_json_sha256(value)


def test_inventory_event_computes_digest_and_normalizes_utc() -> None:
    """可信事实创建器必须计算摘要，并把 aware 时间统一为 UTC。"""
    event = _event()

    assert event.event_type.value == "SOLD_OUT"
    assert event.occurred_at == datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    assert len(event.payload_digest) == 64
    assert event.payload_digest == event.calculate_payload_digest()


def test_inventory_event_rejects_caller_supplied_digest_mismatch() -> None:
    """普通调用方不能用自报摘要覆盖规范事件事实。"""
    event = _event()
    payload = event.model_dump(mode="python")
    payload["payload_digest"] = "0" * 64

    with pytest.raises(ValidationError, match="摘要"):
        _events().InventoryFactEvent.model_validate(payload)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"event_id": ""}, "event_id"),
        ({"room_id": ""}, "room_id"),
        ({"product_id": ""}, "product_id"),
        ({"source": ""}, "source"),
        ({"observed_version": 0}, "observed_version"),
        ({"occurred_at": datetime(2026, 7, 15, 8, 0)}, "时区"),
    ],
)
def test_inventory_event_rejects_incomplete_or_ambiguous_fact(
    overrides: dict[str, Any],
    message: str,
) -> None:
    """事件身份、资源版本和时间语义不完整时必须在 Inbox 前拒绝。"""
    with pytest.raises((TypeError, ValueError, ValidationError), match=message):
        _event(**overrides)


def test_inventory_event_type_is_fixed_to_sold_out() -> None:
    """Phase 12B 首期不接受未设计的库存事件类型。"""
    event = _event()
    payload = event.model_dump(mode="python")
    payload["event_type"] = "RESTOCKED"

    with pytest.raises(ValidationError):
        _events().InventoryFactEvent.model_validate(payload)


def test_event_and_provenance_snapshots_are_immutable() -> None:
    """事件及来源进入持久化链后不能被原地改写。"""
    event = _event()
    provenance = _provenance(event)

    with pytest.raises(ValidationError):
        event.product_id = "changed"
    with pytest.raises(ValidationError):
        provenance.topic = "other-topic"


def test_provenance_requires_aware_time_and_valid_digest() -> None:
    """来源证据必须携带 UTC 可比时间和规范 SHA-256 摘要。"""
    event = _event()

    with pytest.raises(ValidationError):
        _provenance(event, received_at=datetime(2026, 7, 15, 8, 0))
    with pytest.raises(ValidationError):
        _provenance(event, payload_digest="bad-digest")


def test_direct_event_authorization_cannot_be_forged() -> None:
    """字段形状完整也不能绕过内部已验证来源工厂。"""
    from src.skill_runtime.models import EventAuthorizationContext

    event = _event()
    provenance = _provenance(event)
    with pytest.raises(ValidationError, match="内部事件授权工厂"):
        EventAuthorizationContext(
            event_id=event.event_id,
            provenance_id=provenance.provenance_id,
            payload_digest=event.payload_digest,
            observed_version=event.observed_version,
        )


def test_internal_authorization_factory_requires_matching_provenance_digest() -> None:
    """只有事件与持久化 provenance 摘要闭合时才能构造可信授权。"""
    module = _events()
    event = _event()
    provenance = _provenance(event)
    authorization = module._build_event_authorization_context(event, provenance)

    assert authorization.event_id == event.event_id
    assert authorization.provenance_id == provenance.provenance_id
    assert authorization.payload_digest == event.payload_digest
    assert authorization.observed_version == event.observed_version
    assert authorization.provenance_verified is True

    conflicting = provenance.model_copy(update={"payload_digest": "f" * 64})
    with pytest.raises(ValueError, match="摘要"):
        module._build_event_authorization_context(event, conflicting)


def test_verified_authorization_cannot_be_rebound_with_model_copy() -> None:
    """复制已验证对象并替换事件身份后，私有可信标记必须立即失效。"""
    from src.skill_runtime.models import SkillExecutionContext, SkillExecutionRoute

    event = _event()
    authorization = _events()._build_event_authorization_context(
        event,
        _provenance(event),
    )
    rebound = authorization.model_copy(update={"event_id": "event-forged"})

    assert rebound.provenance_verified is False
    with pytest.raises(ValidationError, match="来源"):
        SkillExecutionContext(
            room_id=event.room_id,
            trace_id="trace-001",
            lifecycle="ON_LIVE",
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            event_authorization=rebound,
        )


def test_execution_context_rejects_ambiguous_dual_authorization() -> None:
    """人审与可信事件不能同时出现，避免 Executor 猜测权限来源。"""
    from src.skill_runtime.models import (
        SkillExecutionContext,
        SkillExecutionRoute,
        _build_human_interrupt_approval,
    )

    event = _event()
    authorization = _events()._build_event_authorization_context(
        event,
        _provenance(event),
    )
    approval = _build_human_interrupt_approval(
        decision="APPROVED",
        operator_id="operator-001",
        approval_audit_id="approval-audit-001",
    )

    with pytest.raises(ValidationError, match="不能同时"):
        SkillExecutionContext(
            room_id=event.room_id,
            trace_id="trace-001",
            lifecycle="ON_LIVE",
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            approval=approval,
            event_authorization=authorization,
        )


def test_execution_context_accepts_one_verified_authorization_and_serializes_it() -> None:
    """单一可信事件授权应进入冻结执行上下文并留下可持久化证据。"""
    from src.skill_runtime.models import SkillExecutionContext, SkillExecutionRoute

    event = _event()
    authorization = _events()._build_event_authorization_context(
        event,
        _provenance(event),
    )
    context = SkillExecutionContext(
        room_id=event.room_id,
        trace_id="trace-001",
        lifecycle="ON_LIVE",
        execution_route=SkillExecutionRoute.SKILL_RUNTIME,
        event_authorization=authorization,
    )

    dumped = context.model_dump(mode="json")
    assert dumped["approval"] is None
    assert dumped["event_authorization"]["event_id"] == event.event_id
    with pytest.raises(ValidationError):
        context.event_authorization = None


def test_controlled_authorization_and_impact_enums_are_closed() -> None:
    """授权要求和影响范围必须是受控集合，不能接受任意文本。"""
    from src.skill_runtime.models import AuthorizationRequirement

    assert [item.value for item in AuthorizationRequirement] == [
        "NONE",
        "HUMAN_APPROVAL",
        "TRUSTED_EVENT_OR_HUMAN",
    ]
    assert [item.value for item in _events().ImpactScope] == [
        "PRODUCT",
        "ROOM",
        "PLATFORM",
    ]
    with pytest.raises(ValueError):
        AuthorizationRequirement("TRUST_PAYLOAD")
    with pytest.raises(ValueError):
        _events().ImpactScope("GLOBAL")
