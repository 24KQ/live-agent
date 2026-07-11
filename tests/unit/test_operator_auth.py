# -*- coding: utf-8 -*-
"Phase 7B: 操作员鉴权模块单元测试。"
from __future__ import annotations
import pytest
from unittest.mock import patch
from src.config.settings import Settings
from src.gateway.operator_auth import (
    OperatorRole, OperatorIdentity, authenticate_request,
    authorize_action, extract_idempotency_key,
    OperatorAuthError, OperatorPermissionError,
)

# Settings validation_alias 优先于 Python 字段名，所以传 OPERATOR_xxx
_AUTH_SETTINGS = Settings(
    OPERATOR_AUTH_ENABLED="true",
    OPERATOR_TOKENS="anchor01:tok1:operator;reviewer01:tok2:reviewer;admin01:tok3:admin",
)
_NOAUTH_SETTINGS = Settings(OPERATOR_AUTH_ENABLED='false')


class TestAuthenticateRequest:
    @patch('src.gateway.operator_auth.get_settings', return_value=_AUTH_SETTINGS)
    def test_valid_operator(self, mock_s):
        ident = authenticate_request({"x-operator-id": "anchor01", "x-operator-token": "tok1"})
        assert ident.operator_id == "anchor01"
        assert ident.role == OperatorRole.OPERATOR

    @patch('src.gateway.operator_auth.get_settings', return_value=_AUTH_SETTINGS)
    def test_valid_admin(self, mock_s):
        ident = authenticate_request({"x-operator-id": "admin01", "x-operator-token": "tok3"})
        assert ident.role == OperatorRole.ADMIN

    @patch('src.gateway.operator_auth.get_settings', return_value=_AUTH_SETTINGS)
    def test_wrong_token_raises(self, mock_s):
        with pytest.raises(OperatorAuthError):
            authenticate_request({"x-operator-id": "anchor01", "x-operator-token": "wrong"})

    @patch('src.gateway.operator_auth.get_settings', return_value=_AUTH_SETTINGS)
    def test_missing_headers_raises(self, mock_s):
        with pytest.raises(OperatorAuthError):
            authenticate_request({})

    @patch('src.gateway.operator_auth.get_settings', return_value=_NOAUTH_SETTINGS)
    def test_auth_disabled_returns_admin(self, mock_s):
        ident = authenticate_request({})
        assert ident.role == OperatorRole.ADMIN


class TestAuthorizeAction:
    def test_operator_can_operator(self):
        authorize_action(OperatorIdentity("op", OperatorRole.OPERATOR, ""), OperatorRole.OPERATOR)

    def test_operator_cannot_reviewer(self):
        with pytest.raises(OperatorPermissionError):
            authorize_action(OperatorIdentity("op", OperatorRole.OPERATOR, ""), OperatorRole.REVIEWER)

    def test_admin_can_all(self):
        for role in OperatorRole:
            authorize_action(OperatorIdentity("ad", OperatorRole.ADMIN, ""), role)


class TestExtractIdempotencyKey:
    def test_present(self):
        assert extract_idempotency_key({"x-idempotency-key": "abc123"}) == "abc123"

    def test_absent(self):
        assert extract_idempotency_key({}) is None
