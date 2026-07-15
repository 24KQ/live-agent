"""Phase 12B 售罄紧急 child DAG 的固定候选 Provider。"""

from src.plan_engine.models import (
    CandidatePlanNode,
    CandidatePlanProposal,
    EmergencySoldOutPlanningInput,
    InputBinding,
    InputBindingKind,
    PlanNodeKind,
)


class SoldOutEmergencyProposalProvider:
    """只生成审核过的五节点售罄 DAG，不接受动态执行控制字段。"""

    provider_id = "canonical-sold-out-emergency"
    provider_version = "1.0.0"

    async def propose(self, request: EmergencySoldOutPlanningInput) -> CandidatePlanProposal:
        """保持异步 Provider 契约，当前实现完全确定。"""
        return self.propose_sync(request)

    def propose_sync(self, request: EmergencySoldOutPlanningInput) -> CandidatePlanProposal:
        """返回唯一合法的验证、售罄、备选、提示和汇总链。"""
        if not isinstance(request, EmergencySoldOutPlanningInput):
            raise TypeError("request 必须是 EmergencySoldOutPlanningInput")

        def plan_input(path: str) -> InputBinding:
            """把业务参数限制为冻结紧急输入的单字段静态引用。"""
            return InputBinding(kind=InputBindingKind.PLAN_INPUT, path=(path,))

        return CandidatePlanProposal(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            nodes=(
                CandidatePlanNode(
                    logical_key="validate-sold-out-event",
                    node_kind=PlanNodeKind.CONTROL,
                ),
                CandidatePlanNode(
                    logical_key="mark-sold-out",
                    node_kind=PlanNodeKind.SKILL,
                    skill_id="handle_sold_out_event",
                    depends_on=("validate-sold-out-event",),
                    input_bindings={
                        "product_id": plan_input("product_id"),
                        "expected_version": plan_input("expected_version"),
                    },
                ),
                CandidatePlanNode(
                    logical_key="recommend-backup-product",
                    node_kind=PlanNodeKind.SKILL,
                    skill_id="recommend_backup_product",
                    depends_on=("mark-sold-out",),
                    input_bindings={
                        "room_id": plan_input("room_id"),
                        "sold_out_product_id": plan_input("product_id"),
                    },
                ),
                CandidatePlanNode(
                    logical_key="generate-sold-out-prompt",
                    node_kind=PlanNodeKind.SKILL,
                    skill_id="generate_on_live_prompt",
                    depends_on=("recommend-backup-product",),
                    input_bindings={
                        "room_id": plan_input("room_id"),
                        "sold_out_product_id": plan_input("product_id"),
                        "backup_product_id": InputBinding(
                            kind=InputBindingKind.NODE_OUTPUT,
                            upstream_logical_key="recommend-backup-product",
                            path=("backup_product", "product_id"),
                        ),
                    },
                ),
                CandidatePlanNode(
                    logical_key="collect-sold-out-response",
                    node_kind=PlanNodeKind.CONTROL,
                    depends_on=("generate-sold-out-prompt",),
                ),
            ),
        )
