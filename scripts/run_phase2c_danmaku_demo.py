"""运行 Phase 2C 基础弹幕聚合与参考回复演示。

运行方式：
    python scripts/run_phase2c_danmaku_demo.py

脚本只使用本地脱敏模拟弹幕，不接 LLM、不接真实淘宝 API、不启动长期
Kafka consumer。生成的回复是给主播看的参考话术，不会自动发送给观众。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


# 直接执行脚本时，Python 默认只把 scripts 目录加入 sys.path。这里显式加入
# 仓库根目录，保证可以稳定导入 src 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.danmaku_flow import DanmakuFlowService
from src.skills.danmaku_events import DanmakuEvent
from src.skills.demo_data_seed import DEMO_ROOM_ID
from src.state.models import LifecycleStage, LiveRoomState


def build_demo_events(trace_id: str) -> list[DanmakuEvent]:
    """构造一批脱敏弹幕事件。

    `viewer_id` 使用本地模拟 hash，不代表真实平台用户。事件时间覆盖 5 秒窗口
    内的价格、库存、优惠问题，以及一个普通问题，方便展示分类和人工复核逻辑。
    """

    base_time = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)
    contents = [
        (0, "这个杯子多少钱？"),
        (1, "价格是多少呀？"),
        (2, "还有库存吗？"),
        (3, "今天有优惠券吗？"),
        (4, "主播这个适合办公室用吗？"),
        (7, "什么时候发货？"),
    ]
    return [
        DanmakuEvent(
            room_id=DEMO_ROOM_ID,
            viewer_id=f"viewer_hash_{index:03d}",
            content=content,
            event_time=base_time + timedelta(seconds=offset),
            trace_id=trace_id,
        )
        for index, (offset, content) in enumerate(contents, start=1)
    ]


def main() -> int:
    """执行弹幕聚合演示并返回进程退出码。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)

    trace_id = "trace-phase2c-demo"
    state = LiveRoomState(room_id=DEMO_ROOM_ID, lifecycle=LifecycleStage.ON_LIVE)
    events = build_demo_events(trace_id)
    result = DanmakuFlowService(ToolCallAuditStore(settings)).handle_danmaku_batch(state, events)

    print("Phase 2C danmaku aggregation demo")
    print(f"room_id: {DEMO_ROOM_ID}")
    print(f"trace_id: {trace_id}")
    print(f"event_count: {len(events)}")
    print(f"group_count: {len(result.groups)}")
    print("")
    print("aggregated_questions:")
    for group in result.groups:
        print(f"- {group.category.value}: {group.summary}, count={group.count}, samples={'; '.join(group.sample_contents)}")

    print("")
    print("reference_replies:")
    for reply in result.replies:
        review_flag = "requires_human_review" if reply.requires_human_review else "ready_for_anchor_reference"
        print(f"- {reply.category.value}: confidence={reply.confidence:.2f}, {review_flag}")
        print(f"  {reply.reply_text}")

    print("")
    print(f"audit_ids: {', '.join(result.audit_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
