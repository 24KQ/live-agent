"""Phase 12A 的候选计划 Provider 边界与唯一规范实现。"""

from __future__ import annotations

from typing import Protocol

from src.plan_engine.models import (
    CandidatePlanNode,
    CandidatePlanProposal,
    CardBatchPlanningInput,
    InputBinding,
    InputBindingKind,
    PlanNodeKind,
)


class PlanProposalProvider(Protocol):
    """只读异步候选生成边界。

    Provider 只能根据已冻结的规划输入声明候选 DAG，不能创建 PlanRun、调用 Skill 或
    在错误时转向 Legacy。后续 LLM Provider 必须复用这一契约并接受同一校验器约束。
    """

    async def propose(self, request: CardBatchPlanningInput) -> CandidatePlanProposal:
        """基于可信冻结输入返回待校验候选，而不执行任何副作用。"""


class CanonicalCardBatchProposalProvider:
    """Phase 12A 唯一正式 Provider，生成确定性的最多三张手卡 DAG。

    固定 provider 身份是 PlanVersion 的审计证据。实现不尝试替代模型、动态排序或
    fallback：输入已经不合法会在 ``CardBatchPlanningInput`` 构造期被拒绝，候选若
    不合法则直接抛出校验错误并阻断 PlanRun 创建。
    """

    provider_id = "canonical-card-batch"
    provider_version = "1.0.0"

    async def propose(self, request: CardBatchPlanningInput) -> CandidatePlanProposal:
        """异步 Port 实现，保持未来远程 Provider 可替换但当前逻辑完全确定性。"""
        return self.propose_sync(request)

    def propose_sync(self, request: CardBatchPlanningInput) -> CandidatePlanProposal:
        """同步便利入口，供纯领域测试和不需要事件循环的装配代码使用。"""
        selected_product_ids = tuple(item.product_id for item in request.live_plan.items[:3])
        card_nodes = tuple(
            CandidatePlanNode(
                logical_key=f"card:{product_id}",
                node_kind=PlanNodeKind.SKILL,
                skill_id="generate_product_card",
                depends_on=("prepare-card-batch",),
                input_bindings={
                    "product": InputBinding(
                        kind=InputBindingKind.PLAN_INPUT,
                        path=("products_by_id", product_id),
                    )
                },
            )
            for product_id in selected_product_ids
        )
        return CandidatePlanProposal(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            nodes=(
                CandidatePlanNode(
                    logical_key="prepare-card-batch",
                    node_kind=PlanNodeKind.CONTROL,
                ),
                *card_nodes,
                CandidatePlanNode(
                    logical_key="collect-card-results",
                    node_kind=PlanNodeKind.CONTROL,
                    depends_on=tuple(node.logical_key for node in card_nodes),
                ),
            ),
        )
