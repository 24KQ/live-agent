# -*- coding: utf-8 -*-
"""Phase 7B 操作员鉴权模块。

提供轻量本地鉴权：基于请求头 X-Operator-Id / X-Operator-Token 的身份认证
与基于角色的权限控制。不使用 OAuth/JWT，适合本地开发和生产硬化验证。

依赖配置：
    OPERATOR_AUTH_ENABLED: bool — 是否启用鉴权（默认 False，本地兼容旧行为）
    OPERATOR_TOKENS: str — 格式为 "operator_id:token:role;op2:tok2:admin"
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from src.config.settings import get_settings


class OperatorRole(str, enum.Enum):
    """操作员角色枚举，按权限升序排列。"""

    OPERATOR = "operator"       # 可审批 Harness pending 请求
    REVIEWER = "reviewer"       # 可提交 Evaluation Review
    ADMIN = "admin"             # 全部权限


# 角色层级映射（权限从低到高）
_ROLE_HIERARCHY: dict[OperatorRole, int] = {
    OperatorRole.OPERATOR: 10,
    OperatorRole.REVIEWER: 20,
    OperatorRole.ADMIN: 30,
}


class OperatorAuthError(Exception):
    """认证失败：身份无法识别或 token 不匹配。"""


class OperatorPermissionError(Exception):
    """授权失败：角色权限不足。"""


@dataclass(frozen=True)
class OperatorIdentity:
    """认证通过后的操作员身份凭证。"""

    operator_id: str
    role: OperatorRole
    display_name: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict，用于存入审计日志。"""
        return {
            "operator_id": self.operator_id,
            "role": self.role.value,
            "display_name": self.display_name,
        }


def _parse_operator_tokens(raw: str) -> dict[str, tuple[str, OperatorRole, str]]:
    """解析 OPERATOR_TOKENS 配置。

    格式：operator_id:token:role;op2:tok2:admin
    返回 dict[operator_id, (token, role, display_name)]
    """
    result: dict[str, tuple[str, OperatorRole, str]] = {}
    if not raw.strip():
        return result

    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        if len(parts) != 3:
            continue
        operator_id, token, role_str = parts
        try:
            role = OperatorRole(role_str.lower())
        except ValueError:
            # 未知角色跳过
            continue
        result[operator_id] = (token, role, operator_id)

    return result


def authenticate_request(headers: dict[str, str]) -> OperatorIdentity:
    """从请求头中认证操作员身份。

    读取 X-Operator-Id 和 X-Operator-Token，与配置的 OPERATOR_TOKENS 比对。
    未启用鉴权时返回默认 admin 身份。

    Args:
        headers: HTTP 请求头字典（大小写不敏感）。

    Returns:
        认证通过的操作员身份。

    Raises:
        OperatorAuthError: 缺少必要头字段、token 不匹配或未知 operator_id。
    """
    settings = get_settings()

    # 未启用鉴权：返回默认 admin（兼容本地测试和旧行为）
    if not settings.operator_auth_enabled:
        return OperatorIdentity(
            operator_id=headers.get("x-operator-id", "system"),
            role=OperatorRole.ADMIN,
            display_name="default_admin",
        )

    operator_id = headers.get("x-operator-id")
    token = headers.get("x-operator-token")

    if not operator_id:
        raise OperatorAuthError("缺少 X-Operator-Id 请求头")
    if not token:
        raise OperatorAuthError("缺少 X-Operator-Token 请求头")

    tokens_map = _parse_operator_tokens(settings.operator_tokens)

    if operator_id not in tokens_map:
        raise OperatorAuthError(f"未知 operator_id: {operator_id}")

    expected_token, role, display_name = tokens_map[operator_id]
    if token != expected_token:
        raise OperatorAuthError(f"operator_id {operator_id} token 不匹配")

    return OperatorIdentity(operator_id=operator_id, role=role, display_name=display_name)


def authorize_action(identity: OperatorIdentity, required_role: OperatorRole) -> None:
    """检查操作员是否有权执行指定角色要求的操作。

    权限判定规则：identity.role 的层级 >= required_role 的层级。

    Args:
        identity: 已认证的操作员身份。
        required_role: 执行操作所需的最低角色。

    Raises:
        OperatorPermissionError: 权限不足。
    """
    identity_level = _ROLE_HIERARCHY.get(identity.role, 0)
    required_level = _ROLE_HIERARCHY.get(required_role, 0)

    if identity_level < required_level:
        raise OperatorPermissionError(
            f"operator {identity.operator_id} 角色 {identity.role.value} "
            f"无权执行需要 {required_role.value} 角色的操作"
        )


def extract_idempotency_key(headers: dict[str, str]) -> str | None:
    """从请求头中提取幂等键。

    幂等键用于防止同一个审批请求被重复处理。

    Args:
        headers: HTTP 请求头字典。

    Returns:
        幂等键字符串，不存在则返回 None。
    """
    return headers.get("x-idempotency-key") or None
