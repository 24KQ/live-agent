# -*- coding: utf-8 -*-
"Phase 7B: HarnessSessionStore 锁/TTL/幂等能力测试。"
from __future__ import annotations
import pytest
from datetime import datetime, timedelta, timezone
from src.gateway.harness_session_store import (
    InMemoryHarnessSessionStore, HarnessSessionRecord,
    HarnessSessionNotFoundError,
)


@pytest.fixture
def store():
    s = InMemoryHarnessSessionStore()
    s.save_pending(HarnessSessionRecord(trace_id='t1', room_id='r1'))
    return s


class TestAcquireLock:
    def test_acquire_lock_success(self, store):
        ok, result = store.acquire_lock('t1', 'op1')
        assert ok is True
        assert result.locked_by == 'op1'

    def test_acquire_lock_blocked(self, store):
        store.acquire_lock('t1', 'op1')
        ok, msg = store.acquire_lock('t1', 'op2')
        assert ok is False
        assert 'locked by' in str(msg)

    def test_acquire_lock_not_found(self, store):
        with pytest.raises(HarnessSessionNotFoundError):
            store.acquire_lock('nonexistent', 'op1')


class TestRenewLock:
    def test_renew_lock_success(self, store):
        store.acquire_lock('t1', 'op1', lock_duration_seconds=30)
        ok, result = store.renew_lock('t1', 'op1')
        assert ok is True

    def test_renew_lock_wrong_operator(self, store):
        store.acquire_lock('t1', 'op1')
        ok, msg = store.renew_lock('t1', 'op2')
        assert ok is False


class TestExpireStalePending:
    def test_expire_stale(self):
        s = InMemoryHarnessSessionStore()
        old = datetime.now(timezone.utc) - timedelta(minutes=15)
        s.save_pending(HarnessSessionRecord(trace_id='old', room_id='r1', created_at=old))
        recent = datetime.now(timezone.utc)
        s.save_pending(HarnessSessionRecord(trace_id='recent', room_id='r1', created_at=recent))
        expired = s.expire_stale_pending(max_pending_minutes=10)
        assert 'old' in expired
        assert 'recent' not in expired
        assert s.get('old').status == 'expired'
        assert s.get('recent').status == 'pending_human'


class TestSubmitApprovalIdempotency:
    def test_idempotency_same_key(self, store):
        store.acquire_lock('t1', 'op1')
        r1 = store.submit_approval_with_idempotency("t1", "approved", "op1", "ok", idempotency_key="key1")
        r2 = store.submit_approval_with_idempotency("t1", "approved", "op1", "ok", idempotency_key="key1")
        assert r1.trace_id == r2.trace_id
        assert r1.operator_id == r2.operator_id

    def test_expired_session_rejected(self, store):
        store.acquire_lock('t1', 'op1')
        store.expire_stale_pending(max_pending_minutes=0)
        with pytest.raises(ValueError, match='expired'):
            store.submit_approval_with_idempotency("t1", "approved", "op1", "too late")

    def test_locked_by_other_rejected(self, store):
        store.acquire_lock('t1', 'op1')
        with pytest.raises(ValueError, match='locked by'):
            store.submit_approval_with_idempotency("t1", "approved", "op2", "not mine")

    def test_locked_by_other_rejected(self, store):
        store.acquire_lock('t1', 'op1')
        with pytest.raises(PermissionError, match='locked by'):
            store.submit_approval_with_idempotency("t1", "approved", "op2", "not mine")

    def test_expired_session_rejected(self, store):
        store.acquire_lock('t1', 'op1')
        # expire_stale_pending 检查的是 created_at，用15分钟前的记录
        from datetime import timedelta
        record = store.get('t1')
        from dataclasses import replace
        store._records['t1'] = replace(record, created_at=datetime.now(timezone.utc)-timedelta(minutes=15))
        store.expire_stale_pending(max_pending_minutes=10)
        with pytest.raises(ValueError, match='expired'):
            store.submit_approval_with_idempotency("t1", "approved", "op1", "too late")
