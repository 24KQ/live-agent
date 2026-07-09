"""Phase 5B 语义弹幕聚合 CLI 演示。

三阶段对比：
1. 纯关键词分类（已有 aggregate_danmaku_questions）
2. 加语义聚类（DanmakuSemanticClusterer）
3. 加 LLM 兜底（DanmakuLLMFallback）

用法：
    python scripts/run_phase5b_semantic_danmaku_demo.py
"""

from __future__ import annotations

import sys
import os

# 修复路径，确保可以直接运行
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone, timedelta

from src.skills.danmaku_events import DanmakuEvent
from src.skills.danmaku_aggregator import (
    DanmakuQuestionCategory,
    aggregate_danmaku_questions,
    aggregate_with_semantic_fallback,
    classify_danmaku_question,
)
from src.skills.danmaku_semantic_cluster import DanmakuSemanticClusterer
from src.skills.danmaku_llm_fallback import DanmakuLLMFallback
from src.skills.embedding_service import EmbeddingService, MockEmbeddingService


def _make_event(content: str, offset: int, room_id: str = "room-5b-demo") -> DanmakuEvent:
    basetime = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
    return DanmakuEvent(
        room_id=room_id,
        viewer_id=f"viewer_{offset:03d}",
        content=content,
        event_time=basetime + timedelta(seconds=offset),
        trace_id="trace-5b-demo",
    )


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def main():
    print(f"\n{'#' * 60}")
    print(f"  Phase 5B 语义弹幕聚合演示")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#' * 60}")

    # 构造测试弹幕数据
    test_danmaku = [
        # 关键词可分类的弹幕
        "这个多少钱",
        "价格是多少",
        "还有库存吗",
        "有优惠券吗",
        "怎么发货",
        # 关键词无法分类的弹幕（GENERAL）
        "这个怎么操作呀",
        "怎么弄的",
        "如何操作使用",
        "操作步骤是什么",
        "具体怎么搞",
        "你们这个到底怎么用啊",
        "操作流程发一下",
        "不会用怎么办",
        "讲解一下怎么用",
        "有没有教程",
    ]

    events = [_make_event(msg, i) for i, msg in enumerate(test_danmaku)]

    # === 阶段一：纯关键词分类 ===
    section("阶段一：纯关键词分类（aggregate_danmaku_questions）")
    kw_groups = aggregate_danmaku_questions(events, window_seconds=60)
    for g in kw_groups:
        print(f"  [{g.category.value:15s}] x{g.count:3d}  {g.summary}")
    general_kw = [g for g in kw_groups if g.category == DanmakuQuestionCategory.GENERAL]
    if general_kw:
        print(f"\n  ⚠ 仍有 {sum(g.count for g in general_kw)} 条未分类弹幕留存为 GENERAL")
    else:
        print(f"\n  ✓ 所有弹幕已分类")

    # === 阶段二：加语义聚类 ===
    section("阶段二：加语义聚类（DanmakuSemanticClusterer）")
    clusterer = DanmakuSemanticClusterer(embedding_service=MockEmbeddingService())
    sc_groups = aggregate_with_semantic_fallback(events, window_seconds=60, clusterer=clusterer)
    for g in sc_groups:
        print(f"  [{g.category.value:15s}] x{g.count:3d}  {g.summary}")
    general_sc = [g for g in sc_groups if g.category == DanmakuQuestionCategory.GENERAL]
    if general_sc:
        print(f"\n  ⚠ 仍有 {sum(g.count for g in general_sc)} 条未分类弹幕留存为 GENERAL")
    else:
        print(f"  ✓ 所有弹幕已分类")

    # === 阶段三：加 LLM 兜底 ===
    section("阶段三：加 LLM 兜底（DanmakuLLMFallback）")
    try:
        # 尝试加载真实配置
        from src.core.settings import Settings
        settings = Settings(_env_file=".env", _env_file_encoding="utf-8")
        api_base = settings.llm_api_base_url
        api_key = settings.llm_api_key
        model = settings.llm_model
    except Exception:
        # 回退到硬编码配置（仅用于演示）
        api_base = os.environ.get("LLM_API_BASE_URL", "https://api.deepseek.com")
        api_key = os.environ.get("LLM_API_KEY", "")
        model = os.environ.get("LLM_MODEL", "deepseek-v4-flash")

    if api_key:
        llm_fallback = DanmakuLLMFallback(api_base=api_base, api_key=api_key, model=model)
        llm_groups = aggregate_with_semantic_fallback(
            events, window_seconds=60, clusterer=clusterer, llm_fallback=llm_fallback
        )
        for g in llm_groups:
            print(f"  [{g.category.value:15s}] x{g.count:3d}  {g.summary}")
        general_llm = [g for g in llm_groups if g.category == DanmakuQuestionCategory.GENERAL]
        if general_llm:
            print(f"\n  ⚠ 仍有 {sum(g.count for g in general_llm)} 条未分类弹幕留存为 GENERAL")
        else:
            print(f"  ✓ 所有弹幕已分类")
    else:
        print("  ⚠ 未配置 LLM API key，跳过阶段三")
        print("  设置 LLM_API_KEY 环境变量或配置 .env 后再试")

    # === 对比总结 ===
    section("对比总结")
    kw_general_count = sum(g.count for g in general_kw) if general_kw else 0
    sc_general_count = sum(g.count for g in general_sc) if general_sc else 0
    print(f"  纯关键词未分类: {kw_general_count} 条")
    print(f"  加语义聚类未分类: {sc_general_count} 条")
    if api_key:
        llm_general_count = sum(g.count for g in general_llm) if general_llm else 0
        print(f"  加 LLM 兜底未分类: {llm_general_count} 条")

    print(f"\n{'#' * 60}")
    print(f"  演示完成")
    print(f"{'#' * 60}")


if __name__ == "__main__":
    main()
