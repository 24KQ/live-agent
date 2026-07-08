# -*- coding: utf-8 -*-
"""Phase 4E 记忆回写演示脚本。读取数据库中的 DecisionTrace 记录，同步到记忆层并展示结果。"""
from src.config.settings import get_settings
from src.skills.post_live_memory_sync import PostLiveMemorySyncService

def main():
    settings = get_settings()
    service = PostLiveMemorySyncService(settings)
    test_trace_id = "trace-phase3a-memory-demo"
    anchor_id = "anchor-demo-001"
    room_id = "room-demo-001"
    print("Phase 4E 记忆回写演示")
    print("主播:", anchor_id, "直播间:", room_id, "Trace:", test_trace_id)
    result = service.sync_room_traces(anchor_id=anchor_id, room_id=room_id, trace_id=test_trace_id)
    print("同步结果:")
    print("  写入记忆:", result["memories_written"], "条")
    print("  信任更新:", result["trust_updated"])
    print("  信任分变化:", result["trust_before"], "->", result["trust_after"])
    print("  错误数:", result["errors"])

if __name__ == "__main__":
    main()
