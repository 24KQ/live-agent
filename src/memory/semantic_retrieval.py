"""Phase 3C 语义记忆检索器。

提供 pgvector 余弦距离语义检索和混合加权融合检索。
对 embedding 为 NULL 的记忆自动跳过，API 不可用时降级为纯结构化排序。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from src.memory.models import AnchorMemoryEntry
from src.skills.embedding_service import EmbeddingService, MockEmbeddingService


@dataclass(order=True)
class SemanticResult:
    """语义检索结果。

    排序字段 similarity_score 支持混合加权后的综合分。
    """

    sort_score: float = field(init=False, repr=False)
    memory_id: str
    content: str
    anchor_id: str
    memory_key: str | None
    similarity_score: float

    def __post_init__(self) -> None:
        self.sort_score = self.similarity_score


class SemanticMemoryRetriever:
    """基于 pgvector + embedding 的语义记忆检索器。

    用法：
        retriever = SemanticMemoryRetriever(embedding_service=MockEmbeddingService())
        results = retriever.mixed_retrieve("利润高的产品", rows, structural_scores, top_k=5)
    """

    def __init__(self, embedding_service: EmbeddingService | MockEmbeddingService | None = None) -> None:
        """注入 embedding 服务；为 None 时用 Mock（降级纯结构化）。"""
        self._embedding = embedding_service or MockEmbeddingService()

    def _semantic_top_k(
        self,
        query_text: str,
        rows: list[dict],
        top_k: int,
    ) -> list[SemanticResult]:
        """对 rows 做语义检索，返回 top_k 个 SemanticResult。

        query_text 和 rows 由调用方校验；rows 必须包含 embedding 字段。
        embedding 为 None 的行自动跳过。
        """
        if not query_text or not query_text.strip():
            raise ValueError("query_text must not be empty")

        query_vector = self._embedding.embed(query_text)
        results: list[SemanticResult] = []

        for row in rows:
            emb = row.get("embedding")
            if emb is None:
                continue
            if not isinstance(emb, list) or len(emb) == 0:
                continue
            score = _cosine_similarity(query_vector, emb)
            results.append(SemanticResult(
                memory_id=row["memory_id"],
                content=row["content"],
                anchor_id=row["anchor_id"],
                memory_key=row.get("memory_key"),
                similarity_score=score,
            ))

        # 按相似度降序，取 top_k
        results.sort(key=lambda r: r.similarity_score, reverse=True)
        return results[:top_k]

    def mixed_retrieve(
        self,
        query_text: str,
        rows: list[dict],
        structural_scores: dict[str, float],
        top_k: int = 5,
    ) -> list[SemanticResult]:
        """混合检索：语义分 0.6 + 结构化分 0.4 加权融合。

        语义分来自 pgvector 余弦距离，结构化分由调用方提供（如 MemoryRetriever 的
        类目/标签/evidence_weight 排序分）。两者合并时：
        - 两边共有的 memory_id：0.6 * 语义分 + 0.4 * 结构化分
        - 仅语义命中：保留语义分
        - 仅结构化命中：保留结构化分（生成 SemanticResult 同构返回）
        返回按加权分降序的 top_k 条。
        """
        semantic = self._semantic_top_k(query_text, rows, top_k=len(rows))

        # 构建加权分映射
        weighted: dict[str, float] = {}
        # 先放语义分
        for sr in semantic:
            weighted[sr.memory_id] = sr.similarity_score

        # 融入结构化分
        for mem_id, struct_score in structural_scores.items():
            sem = weighted.get(mem_id, 0.0)
            # 如果结构化里没有对应语义结果，纯用结构化分
            weighted[mem_id] = 0.6 * sem + 0.4 * struct_score

        # 按加权分排序，取 top_k
        sorted_ids = sorted(weighted, key=lambda mid: weighted[mid], reverse=True)[:top_k]

        results: list[SemanticResult] = []
        for mem_id in sorted_ids:
            # 找原始行信息填充 content 等
            matched = next((r for r in rows if r["memory_id"] == mem_id), None)
            if matched:
                results.append(SemanticResult(
                    memory_id=mem_id,
                    content=matched["content"],
                    anchor_id=matched["anchor_id"],
                    memory_key=matched.get("memory_key"),
                    similarity_score=weighted[mem_id],
                ))
        return results


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """两个向量的 L2 归一化后余弦相似度。

    返回值在 [-1, 1]，越高越相似。零向量的相似度为 0。
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
