"""Phase 5B 弹幕语义聚类。

把 embedding 相似度过 threshold 的弹幕归为同一簇。
不依赖 LLM，只做向量余弦相似度比较。

聚类流程：
1. 对每条弹幕生成 embedding
2. 两两计算余弦相似度
3. 相似度 >= threshold 的归为一簇
4. embedding 不可用时降级为每条独立成簇
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ClusterResult:
    """语义聚类结果。"""

    label: str
    messages: list[str] = field(default_factory=list)
    size: int = 0


class DanmakuSemanticClusterer:
    """弹幕语义聚类器。

    用法：
        clusterer = DanmakuSemanticClusterer(embedding_service=my_service)
        results = clusterer.cluster(messages=["多少钱", "几块钱"], threshold=0.75)
    """

    def __init__(self, embedding_service: Any = None) -> None:
        self._embedding_service = embedding_service

    def cluster(self, messages: list[str], threshold: float = 0.75) -> list[ClusterResult]:
        """对弹幕列表做语义聚类。

        threshold: 余弦相似度阈值，默认 0.75。
                   越高越容易分成独立簇，越低越容易归簇。
        """
        if not messages:
            return []

        if self._embedding_service is None:
            return self._fallback_single(messages)

        try:
            embeddings = self._embedding_service.embed(messages)
        except Exception:
            return self._fallback_single(messages)

        if not embeddings or len(embeddings) != len(messages):
            return self._fallback_single(messages)

        # 用并查集做聚类：相似度 >= threshold 的连接
        n = len(messages)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[py] = px

        for i in range(n):
            for j in range(i + 1, n):
                sim = self._cosine_similarity(embeddings[i], embeddings[j])
                if sim >= threshold:
                    union(i, j)

        # 收集结果
        groups: dict[int, list[str]] = {}
        for i, msg in enumerate(messages):
            root = find(i)
            if root not in groups:
                groups[root] = []
            groups[root].append(msg)

        results = []
        for idx, (root, msgs) in enumerate(sorted(groups.items())):
            results.append(ClusterResult(
                label=f"cluster_{idx}",
                messages=msgs,
                size=len(msgs),
            ))
        return results

    def _fallback_single(self, messages: list[str]) -> list[ClusterResult]:
        """embedding 不可用时每条独立成簇。"""
        return [
            ClusterResult(label=f"cluster_{i}", messages=[msg], size=1)
            for i, msg in enumerate(messages)
        ]

    def _cosine_similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        """计算两个向量的余弦相似度。"""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = sum(a * a for a in vec_a) ** 0.5
        norm_b = sum(b * b for b in vec_b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
