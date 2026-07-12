"""工具调用审计写入。

Phase 1 只把工具调用和状态变更写入 PostgreSQL 审计表，不持久化商品状态。
审计写入是高风险动作闭环的一部分：如果审计失败，调用方不能宣称业务动作
已经安全完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from numbers import Number
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.config.settings import Settings
from src.core.security_hooks import GateDecision
from src.state.models import ActionType, RiskLevel


class AuditWriteError(RuntimeError):
    """审计写入失败。"""


class IdempotencyConflictError(AuditWriteError):
    """同一工具幂等键被用于语义不同的审计事件。

    该异常只表达“调用方复用了不兼容的幂等键”，消息中不得包含幂等键、请求载荷
    或结果载荷，避免上层日志在报告冲突时意外回显敏感业务数据。
    """


@dataclass(frozen=True)
class AuditEvent:
    """待写入数据库的审计事件。"""

    trace_id: str
    room_id: str
    tool_name: str
    action_type: ActionType
    risk_level: RiskLevel
    gate_decision: GateDecision
    operator_decision: str | None
    idempotency_key: str | None = None
    request_payload: dict[str, Any] = field(default_factory=dict)
    result_payload: dict[str, Any] = field(default_factory=dict)


class ToolCallAuditStore:
    """PostgreSQL 工具调用审计 Store。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def record_event(self, event: AuditEvent) -> str:
        """写入一条审计事件并返回 audit_id。

        没有幂等键的普通事件始终插入新行。带幂等键的事件先执行原子 INSERT；如果
        唯一索引判定已有胜者，则在同一 READ COMMITTED 连接内用下一条 SELECT 读取
        完整审计事实。只有全部业务字段一致才返回旧 ID，否则以受控冲突失败关闭。

        全部值都通过 psycopg 参数绑定写入 JSONB，避免手工拼接 SQL 的转义与注入
        风险。数据库异常统一转换为不携带 SQL 参数的领域错误，防止敏感载荷进入日志。
        """

        insert_sql = """
            INSERT INTO tool_call_audit (
                trace_id,
                room_id,
                tool_name,
                action_type,
                risk_level,
                gate_decision,
                operator_decision,
                idempotency_key,
                request_payload,
                result_payload
            )
            VALUES (
                %(trace_id)s,
                %(room_id)s,
                %(tool_name)s,
                %(action_type)s,
                %(risk_level)s,
                %(gate_decision)s,
                %(operator_decision)s,
                %(idempotency_key)s,
                %(request_payload)s,
                %(result_payload)s
            )
            RETURNING audit_id::text AS audit_id;
        """
        idempotent_insert_sql = """
            INSERT INTO tool_call_audit (
                trace_id,
                room_id,
                tool_name,
                action_type,
                risk_level,
                gate_decision,
                operator_decision,
                idempotency_key,
                request_payload,
                result_payload
            )
            VALUES (
                %(trace_id)s,
                %(room_id)s,
                %(tool_name)s,
                %(action_type)s,
                %(risk_level)s,
                %(gate_decision)s,
                %(operator_decision)s,
                %(idempotency_key)s,
                %(request_payload)s,
                %(result_payload)s
            )
            ON CONFLICT (tool_name, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            DO NOTHING
            RETURNING audit_id::text AS audit_id;
        """
        select_existing_sql = """
            SELECT
                audit_id::text AS audit_id,
                trace_id,
                room_id,
                tool_name,
                action_type,
                risk_level,
                gate_decision,
                operator_decision,
                idempotency_key,
                request_payload,
                result_payload
            FROM tool_call_audit
            WHERE tool_name = %(tool_name)s
              AND idempotency_key = %(idempotency_key)s;
        """
        # 兼容早期调用方把键放在 request_payload 的写法；新代码应始终使用
        # AuditEvent.idempotency_key，避免载荷字段与数据库唯一索引语义脱节。
        effective_idempotency_key = event.idempotency_key or event.request_payload.get("idempotency_key")
        parameters = {
            "trace_id": event.trace_id,
            "room_id": event.room_id,
            "tool_name": event.tool_name,
            "action_type": event.action_type.value,
            "risk_level": event.risk_level.value,
            "gate_decision": event.gate_decision.value,
            "operator_decision": event.operator_decision,
            "idempotency_key": effective_idempotency_key,
            "request_payload": psycopg.types.json.Jsonb(event.request_payload),
            "result_payload": psycopg.types.json.Jsonb(event.result_payload),
        }
        try:
            with psycopg.connect(
                **self._settings.postgres_connection_kwargs,
                row_factory=dict_row,
            ) as connection:
                with connection.cursor() as cursor:
                    if effective_idempotency_key is None:
                        cursor.execute(insert_sql, parameters)
                        inserted = cursor.fetchone()
                    else:
                        cursor.execute(idempotent_insert_sql, parameters)
                        inserted = cursor.fetchone()
                        if inserted is None:
                            # PostgreSQL 的 READ COMMITTED 为每条语句创建新快照。冲突
                            # INSERT 等待并发胜者提交后，紧随其后的 SELECT 能读取胜者事实。
                            cursor.execute(select_existing_sql, parameters)
                            existing = cursor.fetchone()
                            if existing is None:
                                raise AuditWriteError("failed to resolve audit idempotency replay")
                            if not _event_matches_stored_fact(
                                event,
                                existing,
                                effective_idempotency_key=effective_idempotency_key,
                            ):
                                raise IdempotencyConflictError(
                                    "conflicting audit idempotency replay"
                                )
                            audit_id = str(existing["audit_id"])
                        else:
                            audit_id = str(inserted["audit_id"])

                    if effective_idempotency_key is None and inserted is None:
                        raise AuditWriteError("failed to return inserted audit event")
                    if effective_idempotency_key is None:
                        audit_id = str(inserted["audit_id"])
                connection.commit()
            return audit_id
        except IdempotencyConflictError:
            # 冲突是调用方可识别、可处置的领域错误，必须保留具体类型。
            raise
        except AuditWriteError:
            raise
        except Exception as exc:  # noqa: BLE001 - 审计失败必须被统一转换为业务可读错误。
            raise AuditWriteError("failed to write tool call audit") from exc

    def get_event_by_trace_id(self, trace_id: str) -> dict[str, Any] | None:
        """按 trace_id 读取最近一条审计事件，供测试和排障使用。"""

        sql = """
            SELECT
                audit_id::text,
                trace_id,
                room_id,
                tool_name,
                action_type,
                risk_level,
                gate_decision,
                operator_decision,
                idempotency_key,
                request_payload,
                result_payload,
                created_at
            FROM tool_call_audit
            WHERE trace_id = %(trace_id)s
            ORDER BY created_at DESC
            LIMIT 1;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, {"trace_id": trace_id})
                row = cursor.fetchone()
        return dict(row) if row is not None else None

    def list_events_by_trace_id(self, trace_id: str) -> list[dict[str, Any]]:
        """按 trace_id 读取完整审计链路。

        Phase 2A 一个播前准备流程会产生查询货盘、生成排品、生成手卡、模拟建播等
        多条审计记录。列表接口按创建时间返回，便于测试和后续 CLI 回放整条链路。
        """

        sql = """
            SELECT
                audit_id::text,
                trace_id,
                room_id,
                tool_name,
                action_type,
                risk_level,
                gate_decision,
                operator_decision,
                idempotency_key,
                request_payload,
                result_payload,
                created_at
            FROM tool_call_audit
            WHERE trace_id = %(trace_id)s
            ORDER BY created_at ASC, audit_id ASC;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, {"trace_id": trace_id})
                rows = cursor.fetchall()
        return [dict(row) for row in rows]


def _event_matches_stored_fact(
    event: AuditEvent,
    stored: dict[str, Any],
    *,
    effective_idempotency_key: str,
) -> bool:
    """比较重放事件与数据库原始事实的完整业务语义。

    工具名和幂等键虽然已用于 SELECT 定位，仍纳入比较以让本函数独立表达完整约束。
    JSONB 由 psycopg 解码为 Python 容器后按结构比较，因此对象键顺序不会制造伪冲突，
    但数组顺序、字段值或字段增删都会被识别为不同调用。
    """

    expected_scalars = {
        "trace_id": event.trace_id,
        "room_id": event.room_id,
        "tool_name": event.tool_name,
        "action_type": event.action_type.value,
        "risk_level": event.risk_level.value,
        "gate_decision": event.gate_decision.value,
        "operator_decision": event.operator_decision,
        "idempotency_key": effective_idempotency_key,
    }
    if not all(stored.get(field) == value for field, value in expected_scalars.items()):
        return False
    return _json_values_semantically_equal(
        stored.get("request_payload"),
        event.request_payload,
    ) and _json_values_semantically_equal(
        stored.get("result_payload"),
        event.result_payload,
    )


def _json_values_semantically_equal(left: Any, right: Any) -> bool:
    """按 JSON 类型系统递归比较两个载荷值。

    Python 中 ``bool`` 是 ``int`` 的子类，直接使用 ``==`` 会把 JSON 的 ``true`` 与
    数字 ``1`` 错判为相同。这里先单独处理布尔值，再把整数、浮点数等都按 JSON 的
    number 类型比较；对象忽略键顺序，数组保留顺序，其余标量要求类型和值都一致。
    """

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, Number) or isinstance(right, Number):
        return isinstance(left, Number) and isinstance(right, Number) and left == right
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict) or left.keys() != right.keys():
            return False
        return all(_json_values_semantically_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
            return False
        return all(
            _json_values_semantically_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def initialize_tool_call_audit_schema(settings: Settings) -> None:
    """初始化工具调用审计表。

    CLI 演示和集成测试都应显式调用该函数，避免依赖开发者本机数据库里已经
    存在审计表。SQL 文件本身使用事务级 advisory lock，重复执行是安全的。
    """

    project_root = Path(__file__).resolve().parents[2]
    sql = (project_root / "docker" / "init_phase1_audit.sql").read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
        connection.commit()
