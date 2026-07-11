# -*- coding: utf-8 -*-
"Phase 7B: Evaluation Worker 恢复与告警测试。"
from __future__ import annotations
import pytest
from datetime import datetime, timedelta, timezone
from src.gateway.agent_evaluation_store import (
    EvaluationRunRecord, InMemoryAgentEvaluationStore,
    InMemoryOperationalAlertStore, EvaluationRunCreate,
    ALERT_TYPES,
)
from uuid import uuid4


@pytest.fixture
def store():
    s = InMemoryAgentEvaluationStore()
    s.create_or_reuse_run(EvaluationRunCreate(trace_id="t1", evaluator_version="v1", input_fingerprint="f1"))
    return s


class TestRecoverStaleRuns:
    def test_no_stale_runs(self, store):
        rec, fail = store.recover_stale_runs()
        assert rec == 0
        assert fail == 0

    def test_recover_stale_under_retry_limit(self, store):
        run = store._runs[list(store._runs.keys())[0]]
        old_lease = datetime.now(timezone.utc) - timedelta(minutes=10)
        store._runs[run.evaluation_id] = EvaluationRunRecord(
            evaluation_id=run.evaluation_id, trace_id=run.trace_id,
            evaluator_version=run.evaluator_version, input_fingerprint=run.input_fingerprint,
            profile=run.profile, status='running', retry_count=1,
            lease_until=old_lease,
        )
        rec, fail = store.recover_stale_runs()
        assert rec == 1
        assert store._runs[run.evaluation_id].status == "queued"
        assert store._runs[run.evaluation_id].retry_count == 2

    def test_fail_exhausted_retries(self, store):
        run = store._runs[list(store._runs.keys())[0]]
        old_lease = datetime.now(timezone.utc) - timedelta(minutes=10)
        exhausted = EvaluationRunRecord(
            evaluation_id=run.evaluation_id, trace_id=run.trace_id,
            evaluator_version=run.evaluator_version, input_fingerprint=run.input_fingerprint,
            profile=run.profile, status='running', retry_count=3,
            lease_until=old_lease,
        )
        store._runs[run.evaluation_id] = exhausted
        rec, fail = store.recover_stale_runs()
        assert rec == 0
        assert fail == 1
        assert store._runs[run.evaluation_id].status == "failed"
        assert "max_retries" in (store._runs[run.evaluation_id].error or "")


class TestOperationalAlertStore:
    @pytest.fixture
    def alert_store(self):
        return InMemoryOperationalAlertStore()

    def test_create_and_list(self, alert_store):
        a1 = alert_store.create_alert(ALERT_TYPES['APPROVAL_EXPIRED'], 'warning', 'test', 'msg1')
        a2 = alert_store.create_alert(ALERT_TYPES['EVAL_RETRY_EXHAUSTED'], 'error', 'worker', 'msg2')
        all_alerts = alert_store.list_alerts()
        assert len(all_alerts) == 2
        warnings = alert_store.list_alerts(alert_type=ALERT_TYPES['APPROVAL_EXPIRED'])
        assert len(warnings) == 1

    def test_acknowledge_and_resolve(self, alert_store):
        a = alert_store.create_alert('approval_expired', 'error', 'test', 'msg')
        alert_store.acknowledge_alert(a['alert_id'])
        assert alert_store._alerts[a["alert_id"]]["status"] == "acknowledged"
        alert_store.resolve_alert(a['alert_id'])
        assert alert_store._alerts[a["alert_id"]]["status"] == "resolved"

    def test_acknowledge_and_resolve(self, alert_store):
        a = alert_store.create_alert('approval_expired', 'error', 'test', 'msg')
        alert_store.acknowledge_alert(a.alert_id)
        updated = alert_store.list_alerts()[0]
        assert updated.status == 'acknowledged'
        alert_store.resolve_alert(a.alert_id)
        updated2 = alert_store.list_alerts()[0]
        assert updated2.status == 'resolved'
