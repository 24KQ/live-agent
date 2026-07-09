"""Phase 5D LLM 播后复盘总结 CLI 演示。

演示三种场景：
1. LLM 可用时生成自然语言复盘总结
2. LLM 不可用时降级到结构化报告
3. 无决策数据时返回基础报告

用法：
    python scripts/run_phase5d_llm_review_demo.py
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime

from src.skills.llm_post_live_summary import (
    LLMPostLiveSummary,
    build_review_prompt,
    build_structured_fallback,
)


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def main():
    print(f"\n{'#' * 60}")
    print(f"  Phase 5D LLM 播后复盘总结演示")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#' * 60}")

    summarizer = LLMPostLiveSummary()

    # 场景 1：LLM 可用
    section("场景 1：LLM 复盘总结")
    attribution1 = {
        "total_decisions": 12,
        "adoption_rate": 0.75,
        "accuracy_rate": 0.83,
        "unattributable_count": 0,
    }
    issues1 = ["主播拒绝了 Agent 关于价格调整的有效建议"]

    print("归因数据：")
    print(f"  总决策：{attribution1['total_decisions']}")
    print(f"  采纳率：{attribution1['adoption_rate'] * 100:.1f}%")
    print(f"  准确率：{attribution1['accuracy_rate'] * 100:.1f}%")
    print(f"  问题：{issues1}")
    print()

    report1 = summarizer.generate(attribution1, issues1)
    print("复盘结果：")
    print(report1)

    # 场景 2：LLM 不可用（降级）
    section("场景 2：结构化降级")
    attribution2 = {
        "total_decisions": 5,
        "adoption_rate": 0.6,
        "accuracy_rate": 0.8,
        "unattributable_count": 1,
    }
    issues2 = ["Agent 建议被采纳但效果不佳"]

    print("归因数据：")
    print(f"  总决策：{attribution2['total_decisions']}")
    print(f"  采纳率：{attribution2['adoption_rate'] * 100:.1f}%")
    print(f"  问题：{issues2}")
    print()

    # 模拟 LLM 不可用
    old_key = summarizer._api_key
    summarizer._api_key = ""
    report2 = summarizer.generate(attribution2, issues2)
    summarizer._api_key = old_key
    print("降级报告：")
    print(report2)

    # 场景 3：空数据
    section("场景 3：无决策数据")
    report3 = summarizer.generate({}, [])
    print("结果：")
    print(report3)

    print(f"\n{'#' * 60}")
    print(f"  演示完成")
    print(f"{'#' * 60}")


if __name__ == "__main__":
    main()
