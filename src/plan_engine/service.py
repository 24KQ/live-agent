"""Phase 12A PlanEngine 生命周期装配入口。

该服务不创建隐藏后台线程，也不拥有第二份计划状态。应用启动器负责在启动时调用
``startup``，现有调度器负责每 30 秒调用 ``run_reconciliation_tick``；两者都委托
同一个 PlanReconciliationService。
"""

from __future__ import annotations

from typing import Any, Protocol

from src.plan_engine.reconciliation import RECONCILIATION_INTERVAL_SECONDS


class LifecycleReconciler(Protocol):
    """生命周期服务依赖的最小对账接口。"""

    def reconcile_startup(self) -> tuple[Any, ...]:
        """执行一次启动扫描。"""

    def reconcile_active_plans_once(self) -> tuple[Any, ...]:
        """执行一次周期扫描。"""


class PlanEngineService:
    """向应用装配层暴露显式启动和周期入口。"""

    def __init__(self, *, reconciler: LifecycleReconciler) -> None:
        """冻结唯一 Reconciliation Service；不复制 Store 或 checkpoint。"""
        self._reconciler = reconciler
        self.reconciliation_interval_seconds = RECONCILIATION_INTERVAL_SECONDS

    def startup(self) -> tuple[Any, ...]:
        """服务启动后立即执行一次幂等对账扫描。"""
        return self._reconciler.reconcile_startup()

    def run_reconciliation_tick(self) -> tuple[Any, ...]:
        """由外部调度器按固定周期触发一次扫描。"""
        return self._reconciler.reconcile_active_plans_once()


__all__ = ["PlanEngineService"]
