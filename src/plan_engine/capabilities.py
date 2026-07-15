"""Phase 12A 候选节点到可信执行能力事实的收敛层。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote

from src.skill_runtime.models import SkillManifest
from src.state.models import LifecycleStage, RiskLevel


class PlanCapabilityError(ValueError):
    """表示候选节点请求了 PlanEngine 白名单外的能力或控制节点。"""


@dataclass(frozen=True)
class ResolvedPlanCapability:
    """由可信 Catalog 和固定并发策略导出的不可覆盖执行事实。

    Provider 候选没有入口设置这些字段。后续 Store 只能持久化此对象的投影，
    Worker 也只能据此创建 Runtime 调用，确保版本、风险和超时不受候选影响。
    """

    node_type: str
    skill_id: str | None
    skill_version: str | None
    lifecycle: frozenset[LifecycleStage]
    risk_level: RiskLevel | None
    max_attempt_seconds: int | None
    resource_keys: tuple[str, ...]
    max_concurrency: int


class PlanCapabilityProfile:
    """Phase 12A 的最小能力白名单和每类节点的并发语义。

    控制节点只在 PlanEngine 内部编排，因而没有外部资源锁；手卡节点则对同一
    房间和商品施加唯一资源键，防止两个 PlanRun 并发覆盖同一商品的可见结果。
    """

    PREPARE_CARD_BATCH = "PREPARE_CARD_BATCH"
    COLLECT_CARD_RESULTS = "COLLECT_CARD_RESULTS"
    GENERATE_PRODUCT_CARD = "generate_product_card"
    VALIDATE_SOLD_OUT_EVENT = "VALIDATE_SOLD_OUT_EVENT"
    COLLECT_SOLD_OUT_RESPONSE = "COLLECT_SOLD_OUT_RESPONSE"
    HANDLE_SOLD_OUT_EVENT = "handle_sold_out_event"
    RECOMMEND_BACKUP_PRODUCT = "recommend_backup_product"
    GENERATE_ON_LIVE_PROMPT = "generate_on_live_prompt"
    CARD_MAX_CONCURRENCY = 4

    def __init__(self, catalog: Sequence[SkillManifest]) -> None:
        """从启动期已校验的 Catalog 快照提取唯一允许的 Skill Manifest。"""
        manifests = tuple(catalog)
        self._manifests_by_skill_id = {
            manifest.skill_id: manifest for manifest in manifests
        }
        matches = tuple(
            manifest
            for manifest in manifests
            if manifest.skill_id == self.GENERATE_PRODUCT_CARD
        )
        if len(matches) != 1:
            raise PlanCapabilityError("可信 Catalog 必须包含且仅包含一个 generate_product_card")
        manifest = matches[0]
        if not manifest.version:
            raise PlanCapabilityError("可信 Catalog 中的手卡 Skill 版本不能为空")
        self._card_manifest = manifest

    @classmethod
    def default(cls, catalog: Sequence[SkillManifest]) -> "PlanCapabilityProfile":
        """构建首期固定白名单 Profile，不允许动态注册额外能力。"""
        return cls(catalog=catalog)

    def resolve_skill_node(
        self,
        *,
        skill_id: str,
        product_id: str,
        room_id: str,
    ) -> ResolvedPlanCapability:
        """解析唯一允许的手卡 Skill，并从 Manifest 补全全部执行事实。"""
        if skill_id != self.GENERATE_PRODUCT_CARD:
            raise PlanCapabilityError(f"PlanEngine 不允许 Skill: {skill_id}")
        if not room_id or not product_id:
            raise PlanCapabilityError("手卡资源键需要非空 room_id 和 product_id")
        manifest = self._card_manifest
        encoded_room_id = self._encode_resource_key_segment(room_id)
        encoded_product_id = self._encode_resource_key_segment(product_id)
        return ResolvedPlanCapability(
            node_type="SKILL",
            skill_id=manifest.skill_id,
            skill_version=manifest.version,
            lifecycle=manifest.lifecycle,
            risk_level=manifest.risk_level,
            max_attempt_seconds=manifest.max_attempt_seconds,
            resource_keys=(f"room:{encoded_room_id}:product:{encoded_product_id}",),
            max_concurrency=self.CARD_MAX_CONCURRENCY,
        )

    @staticmethod
    def _encode_resource_key_segment(value: str) -> str:
        """对资源键中的不可信动态段执行确定且可逆的 percent-encoding。

        ``:`` 属于资源键静态语法，若允许 room_id/product_id 原样携带会改变分段
        边界；``%`` 又是编码前缀，若不同时编码会让原始 ``%3A`` 与冒号形成别名。
        RFC 3986 的普通字母、数字和 ``-._~`` 保持原样，因此既有普通资源键格式
        完全兼容，而所有分隔符都被隔离在可信静态模板内。
        """
        return quote(value, safe="-._~")

    def resolve_control_node(self, *, control_type: str) -> ResolvedPlanCapability:
        """解析两个内部控制节点，明确它们不持有任何外部资源锁。"""
        if control_type not in {self.PREPARE_CARD_BATCH, self.COLLECT_CARD_RESULTS}:
            raise PlanCapabilityError(f"PlanEngine 不允许控制节点: {control_type}")
        return ResolvedPlanCapability(
            node_type=control_type,
            skill_id=None,
            skill_version=None,
            lifecycle=frozenset(),
            risk_level=None,
            max_attempt_seconds=None,
            resource_keys=(),
            max_concurrency=self.CARD_MAX_CONCURRENCY,
        )

    def resolve_emergency_control_node(
        self,
        *,
        logical_key: str,
    ) -> ResolvedPlanCapability:
        """将固定紧急 DAG 的首尾节点解析为无副作用控制能力。"""
        control_type_by_key = {
            "validate-sold-out-event": self.VALIDATE_SOLD_OUT_EVENT,
            "collect-sold-out-response": self.COLLECT_SOLD_OUT_RESPONSE,
        }
        control_type = control_type_by_key.get(logical_key)
        if control_type is None:
            raise PlanCapabilityError(f"PlanEngine 不允许紧急控制节点: {logical_key}")
        return ResolvedPlanCapability(
            node_type=control_type,
            skill_id=None,
            skill_version=None,
            lifecycle=frozenset(),
            risk_level=None,
            max_attempt_seconds=None,
            resource_keys=(),
            max_concurrency=self.CARD_MAX_CONCURRENCY,
        )

    def resolve_emergency_skill_node(
        self,
        *,
        skill_id: str,
        room_id: str,
        product_id: str,
    ) -> ResolvedPlanCapability:
        """从 Catalog 解析紧急 Skill，并只为售罄 CAS 写施加商品级互斥。"""
        allowed = {
            self.HANDLE_SOLD_OUT_EVENT,
            self.RECOMMEND_BACKUP_PRODUCT,
            self.GENERATE_ON_LIVE_PROMPT,
        }
        if skill_id not in allowed:
            raise PlanCapabilityError(f"PlanEngine 不允许紧急 Skill: {skill_id}")
        manifest = self._manifests_by_skill_id.get(skill_id)
        if manifest is None or not manifest.version:
            raise PlanCapabilityError(f"可信 Catalog 缺少紧急 Skill: {skill_id}")
        if not room_id or not product_id:
            raise PlanCapabilityError("紧急 Skill 资源身份需要非空 room_id 和 product_id")
        resource_keys: tuple[str, ...] = ()
        max_concurrency = self.CARD_MAX_CONCURRENCY
        if skill_id == self.HANDLE_SOLD_OUT_EVENT:
            resource_keys = (
                "room:"
                f"{self._encode_resource_key_segment(room_id)}:product:"
                f"{self._encode_resource_key_segment(product_id)}",
            )
            max_concurrency = 1
        return ResolvedPlanCapability(
            node_type="SKILL",
            skill_id=manifest.skill_id,
            skill_version=manifest.version,
            lifecycle=manifest.lifecycle,
            risk_level=manifest.risk_level,
            max_attempt_seconds=manifest.max_attempt_seconds,
            resource_keys=resource_keys,
            max_concurrency=max_concurrency,
        )
