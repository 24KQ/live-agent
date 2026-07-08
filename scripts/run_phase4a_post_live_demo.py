"""Phase 4A 播后复盘 CLI 演示。

演示 POST_LIVE 锁定 -> 归因 -> 复盘 -> 信任更新 -> 报告输出的完整闭环。
"""

from decimal import Decimal
from src.state.models import LifecycleStage
from src.core.post_live_lock import post_live_tool_mask
from src.skills.post_live_attribution import PostLiveAttribution
from src.skills.post_live_review import PostLiveReview


def main():
    print("=" * 60)
    print("Phase 4A 播后复盘演示")
    print("=" * 60)

    # 1. 播后锁定验证
    print("\n[1] 播后锁定验证 (POST_LIVE)")
    blocked = {"set_product_price", "switch_product", "setup_live_session"}
    for tool in blocked:
        result = post_live_tool_mask(tool, LifecycleStage.POST_LIVE, trust_score=0.5)
        print(f"  {tool}: {result}")

    # 2. 模拟本场决策记录
    traces = [
        {"anchor_action": "accepted", "business_result": "good", "trust_delta": 0.05},
        {"anchor_action": "accepted", "business_result": "bad", "trust_delta": -0.10},
        {"anchor_action": "rejected", "business_result": "agent_right", "trust_delta": 0.03},
        {"anchor_action": "rejected", "business_result": "anchor_right", "trust_delta": -0.05},
    ]

    # 3. 归因
    print("\n[2] 数据归因")
    attr = PostLiveAttribution.calculate(traces)
    print(f"  总决策数: {attr.total_decisions}")
    print(f"  采纳率: {attr.adoption_rate}")
    print(f"  准确率: {attr.accuracy_rate}")
    if attr.notes:
        print(f"  注意事项: {attr.notes}")

    # 4. 复盘
    print("\n[3] 决策复盘")
    report = PostLiveReview.review(traces)
    print(f"  总决策数: {report['total_decisions']}")
    print(f"  trust 累计变化: {report['trust_delta_total']}")
    if report["issues"]:
        print(f"  发现问题: {report['issues']}")
    else:
        print("  无问题")

    print("\n" + "=" * 60)
    print("演示结束。")
    print("=" * 60)


if __name__ == "__main__":
    main()
