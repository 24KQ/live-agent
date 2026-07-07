"""Phase 3A 主播记忆与 trust_score PostgreSQL Store。

Store 只负责读写数据，不包含排品策略和信任分规则。这样后续切换到 embedding 检索或
增加缓存时，不会影响上层 MemoryAwarePlanService 的业务接口。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import Settings
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource, TrustState


class MemoryStore:
    """PostgreSQL 记忆仓储。

    settings 允许为 None 是为了单元测试可以只验证纯校验和过滤条件构造；真正执行数据库
    读写时会显式检查 settings，避免误以为写入成功。
    """

    def __init__(self, settings: Settings | None) -> None:
        self._settings = settings

    @staticmethod
    def build_query_filters(
        anchor_id: str,
        room_id: str | None = None,
        layer: MemoryLayer | None = None,
    ) -> dict[str, str]:
        """构建参数化查询条件。

        该方法只返回参数字典，不拼接用户输入到 SQL 字符串，防止后续扩展过滤条件时引入
        SQL 注入风险。空 anchor_id 会被拒绝，避免误扫全表记忆。
        """

        if not anchor_id or not anchor_id.strip():
            raise ValueError("anchor_id must not be empty")
        filters = {"anchor_id": anchor_id}
        if room_id is not None:
            if not room_id.strip():
                raise ValueError("room_id must not be empty")
            filters["room_id"] = room_id
        if layer is not None:
            filters["layer"] = layer.value
        return filters

    def write_memory(self, entry: AnchorMemoryEntry) -> str:
        """写入或更新一条主播记忆，并返回 memory_id。

        memory_key 存在时使用 upsert，保证 seed 脚本和演示可以重复执行；memory_key 为空时
        会插入一条新记忆，适合未来真实运行时记录新的观察结果。
        """

        # 即使调用方用 model_construct 绕过 Pydantic，也要在 Store 入口重新校验一遍，
        # 防止脏数据进入数据库层。
        validated = AnchorMemoryEntry.model_validate(entry.model_dump(mode="python"))
        self._require_settings()
        self._ensure_memory_key_not_moved(validated)
        self._ensure_room_belongs_to_anchor(validated.anchor_id, validated.room_id)
        sql = """
            INSERT INTO live_agent_anchor_memories (
                memory_key,
                anchor_id,
                room_id,
                layer,
                content,
                metadata,
                confidence,
                evidence_weight,
                source
            )
            VALUES (
                %(memory_key)s,
                %(anchor_id)s,
                %(room_id)s,
                %(layer)s,
                %(content)s,
                %(metadata)s,
                %(confidence)s,
                %(evidence_weight)s,
                %(source)s
            )
            ON CONFLICT (memory_key)
            DO UPDATE SET
                room_id = EXCLUDED.room_id,
                layer = EXCLUDED.layer,
                content = EXCLUDED.content,
                metadata = EXCLUDED.metadata,
                confidence = EXCLUDED.confidence,
                evidence_weight = EXCLUDED.evidence_weight,
                source = EXCLUDED.source
            WHERE live_agent_anchor_memories.anchor_id = EXCLUDED.anchor_id
            RETURNING memory_id::text;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, self._memory_to_params(validated))
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("memory_key already exists for a different anchor_id")
                memory_id = row[0]
            connection.commit()
        return str(memory_id)

    def list_memories(
        self,
        anchor_id: str,
        room_id: str | None = None,
        layer: MemoryLayer | None = None,
    ) -> list[AnchorMemoryEntry]:
        """按主播、可选直播间和可选层级读取记忆。

        room_id 为空时读取该主播下全部记忆；room_id 存在时同时包含该直播间专属记忆和
        anchor 级长期记忆，便于播前既考虑长期偏好，也考虑当前场次上下文。
        """

        filters = self.build_query_filters(anchor_id=anchor_id, room_id=room_id, layer=layer)
        self._require_settings()
        clauses = ["anchor_id = %(anchor_id)s"]
        if "room_id" in filters:
            clauses.append("(room_id = %(room_id)s OR room_id IS NULL)")
        if "layer" in filters:
            clauses.append("layer = %(layer)s")
        sql = f"""
            SELECT
                memory_id::text,
                memory_key,
                anchor_id,
                room_id,
                layer,
                content,
                metadata,
                confidence,
                evidence_weight,
                source,
                created_at
            FROM live_agent_anchor_memories
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, memory_id ASC;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, filters)
                rows = cursor.fetchall()
        return [self._row_to_memory(row) for row in rows]

    def get_trust_state(self, anchor_id: str) -> TrustState:
        """读取主播 trust_score；不存在时返回默认状态但不隐式写库。"""

        if not anchor_id or not anchor_id.strip():
            raise ValueError("anchor_id must not be empty")
        self._require_settings()
        sql = """
            SELECT anchor_id, trust_score, updated_at
            FROM live_agent_anchor_trust_state
            WHERE anchor_id = %(anchor_id)s;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, {"anchor_id": anchor_id})
                row = cursor.fetchone()
        if row is None:
            return TrustState(anchor_id=anchor_id)
        return TrustState(
            anchor_id=row["anchor_id"],
            trust_score=Decimal(str(row["trust_score"])),
            updated_at=row["updated_at"],
        )

    def upsert_trust_state(self, state: TrustState) -> None:
        """写入主播 trust_score 最新状态。"""

        validated = TrustState.model_validate(state)
        self._require_settings()
        sql = """
            INSERT INTO live_agent_anchor_trust_state(anchor_id, trust_score, updated_at)
            VALUES (%(anchor_id)s, %(trust_score)s, NOW())
            ON CONFLICT (anchor_id)
            DO UPDATE SET trust_score = EXCLUDED.trust_score, updated_at = NOW();
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql,
                    {
                        "anchor_id": validated.anchor_id,
                        "trust_score": validated.trust_score,
                    },
                )
            connection.commit()

    def _require_settings(self) -> None:
        """确保数据库操作有 Settings 可用。"""

        if self._settings is None:
            raise RuntimeError("MemoryStore requires Settings for database operations")

    def _ensure_memory_key_not_moved(self, entry: AnchorMemoryEntry) -> None:
        """阻止同一个 memory_key 被移动到另一个主播名下。

        记忆属于主播画像的一部分，跨主播复用 key 会污染后续排品和信任评估；因此这里在
        upsert 之前先做显式检查，给调用方返回领域错误，而不是暴露底层外键异常。
        """

        if entry.memory_key is None:
            return
        sql = """
            SELECT anchor_id
            FROM live_agent_anchor_memories
            WHERE memory_key = %(memory_key)s;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, {"memory_key": entry.memory_key})
                row = cursor.fetchone()
        if row is not None and row[0] != entry.anchor_id:
            raise ValueError("memory_key already exists for a different anchor_id")

    def _ensure_room_belongs_to_anchor(self, anchor_id: str, room_id: str | None) -> None:
        """校验直播间是否属于当前主播。

        PostgreSQL 里也有组合外键兜底；Store 层提前校验是为了返回更清晰的领域错误，并避免
        上层 CLI 或测试暴露底层数据库异常细节。
        """

        if room_id is None:
            return
        sql = """
            SELECT 1
            FROM live_agent_live_rooms
            WHERE room_id = %(room_id)s AND anchor_id = %(anchor_id)s;
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, {"room_id": room_id, "anchor_id": anchor_id})
                row = cursor.fetchone()
        if row is None:
            raise ValueError("room_id does not belong to anchor_id")

    @staticmethod
    def _memory_to_params(entry: AnchorMemoryEntry) -> dict[str, Any]:
        """把领域模型转换为 psycopg 参数字典。"""

        return {
            "memory_key": entry.memory_key,
            "anchor_id": entry.anchor_id,
            "room_id": entry.room_id,
            "layer": entry.layer.value,
            "content": entry.content,
            "metadata": Jsonb(entry.metadata),
            "confidence": entry.confidence,
            "evidence_weight": entry.evidence_weight,
            "source": entry.source.value,
        }

    @staticmethod
    def _row_to_memory(row: dict[str, Any]) -> AnchorMemoryEntry:
        """把数据库行转换为 AnchorMemoryEntry。"""

        return AnchorMemoryEntry(
            memory_id=row["memory_id"],
            memory_key=row["memory_key"],
            anchor_id=row["anchor_id"],
            room_id=row["room_id"],
            layer=MemoryLayer(row["layer"]),
            content=row["content"],
            metadata=dict(row["metadata"] or {}),
            confidence=Decimal(str(row["confidence"])),
            evidence_weight=Decimal(str(row["evidence_weight"])),
            source=MemorySource(row["source"]),
            created_at=row["created_at"],
        )
