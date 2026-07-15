"""启动冻结 Specialist Profile Registry 与确定性任务路由。"""

from __future__ import annotations

from collections.abc import Iterable
from threading import RLock
from types import MappingProxyType
from typing import Mapping

from src.specialist_runtime.models import AgentTask, SpecialistTaskKind
from src.specialist_runtime.profiles import SpecialistProfile


class SpecialistProfileError(RuntimeError):
    """Profile 注册或解析失败的公共基类。"""


class SpecialistProfileConflictError(SpecialistProfileError):
    """相同 profile_id/version 被绑定到不同冻结事实。"""


class SpecialistProfileResolutionError(SpecialistProfileError):
    """任务无法解析到唯一且 task_kind 一致的 Profile。"""


class SpecialistProfileRegistry:
    """保存精确 Profile 版本，不提供动态覆盖或模型自选入口。"""

    def __init__(self, profiles: Iterable[SpecialistProfile] = ()) -> None:
        self._lock = RLock()
        self._profiles: dict[tuple[str, str], SpecialistProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: SpecialistProfile) -> SpecialistProfile:
        """幂等注册同摘要 Profile，并拒绝身份相同但事实不同的配置。"""

        key = (profile.profile_id, profile.profile_version)
        with self._lock:
            existing = self._profiles.get(key)
            if existing is None:
                self._profiles[key] = profile
                return profile
            if existing.profile_digest != profile.profile_digest:
                raise SpecialistProfileConflictError(
                    f"profile identity conflict: {profile.profile_id}@{profile.profile_version}"
                )
            return existing

    def resolve_identity(self, profile_id: str, profile_version: str) -> SpecialistProfile:
        """按启动配置给出的精确身份读取 Profile，不接受整个 AgentTask。"""

        key = (profile_id, profile_version)
        with self._lock:
            profile = self._profiles.get(key)
        if profile is None:
            raise SpecialistProfileResolutionError(
                f"未知 Specialist Profile: {profile_id}@{profile_version}"
            )
        return profile

    def list_profiles_for_task_kind(
        self,
        task_kind: SpecialistTaskKind,
    ) -> tuple[SpecialistProfile, ...]:
        """返回某生命周期的全部版本，供 Orchestrator 检测歧义而非猜版本。"""

        return tuple(
            profile
            for profile in self.list_profiles()
            if profile.task_kind is task_kind
        )

    def list_profiles(self) -> tuple[SpecialistProfile, ...]:
        """按稳定身份排序返回不可变 Profile 元组，供启动审计使用。"""

        with self._lock:
            return tuple(
                self._profiles[key]
                for key in sorted(self._profiles)
            )


class SpecialistOrchestrator:
    """只做 Task 到一个 Profile 的确定性解析，不执行或串联 Agent。"""

    def __init__(
        self,
        registry: SpecialistProfileRegistry,
        *,
        routes: Mapping[SpecialistTaskKind, tuple[str, str]] | None = None,
    ) -> None:
        self._registry = registry
        validated_routes: dict[SpecialistTaskKind, tuple[str, str]] = {}
        explicit_routes = dict(routes or {})
        for task_kind, identity in explicit_routes.items():
            if (
                not isinstance(task_kind, SpecialistTaskKind)
                or type(identity) is not tuple
                or len(identity) != 2
                or any(type(part) is not str or not part for part in identity)
            ):
                raise SpecialistProfileResolutionError(
                    "explicit route requires SpecialistTaskKind and a two-string tuple"
                )
            # 即使调用方传入 tuple，也重新构造身份值，确保内部只保存规范快照。
            normalized_identity = (identity[0], identity[1])
            profile = registry.resolve_identity(*normalized_identity)
            if profile.task_kind is not task_kind:
                raise SpecialistProfileResolutionError(
                    "task_kind does not match the configured frozen route"
                )
            validated_routes[task_kind] = normalized_identity

        # 没有显式配置时，只允许从启动时唯一的 Profile 推导路由。推导结果与
        # “当前无路由”同样冻结，后续 Registry 注册不能改变运行中的选择。
        for task_kind in SpecialistTaskKind:
            if task_kind in explicit_routes:
                continue
            candidates = registry.list_profiles_for_task_kind(task_kind)
            if len(candidates) > 1:
                raise SpecialistProfileResolutionError(
                    f"ambiguous task_kind route: {task_kind}"
                )
            if candidates:
                selected = candidates[0]
                validated_routes[task_kind] = (
                    selected.profile_id,
                    selected.profile_version,
                )
        # 路由在装配后只读；AgentTask 和模型输出都没有修改或追加路由的入口。
        self._routes = MappingProxyType(validated_routes)

    def resolve_profile(self, task: AgentTask) -> SpecialistProfile:
        """先按生命周期选择冻结路由，再核对 Task 钉住的身份，拒绝调用方自选。"""

        identity = self._routes.get(task.task_kind)
        if identity is None:
            raise SpecialistProfileResolutionError(
                f"未知 task_kind route: {task.task_kind}"
            )

        if identity != (task.profile_id, task.profile_version):
            raise SpecialistProfileResolutionError(
                "未知或不匹配 task_kind frozen route: "
                f"{task.profile_id}@{task.profile_version}"
            )
        return self._registry.resolve_identity(*identity)
