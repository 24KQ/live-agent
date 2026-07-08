"""Phase 3C Embedding 服务。

封装智谱（bigmodel）embedding-3 API，支持单条和批量文本向量化。
同时提供 MockEmbeddingService，用于单元测试的确定性验证。
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import urllib.request
import urllib.error
from typing import Any

from src.config.settings import Settings


class EmbeddingService:
    """智谱 embedding API 调用封装。

    使用 settings 中的 embedding_api_base_url + embedding_embeddings_path 组装
    完整的 /embeddings 端点 URL。批量调用时，发送 list[str] 到 input 字段，
    API 返回同一批次的多个向量。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        if settings is None:
            from src.config.settings import get_settings
            settings = get_settings()
        self._base_url: str = (settings.embedding_api_base_url or "").rstrip("/")
        self._path: str = (settings.embedding_embeddings_path or "/embeddings").lstrip("/")
        self._api_key: str = settings.embedding_api_key or ""
        self._model: str = settings.embedding_model or "embedding-3"
        self._dimensions: int = settings.embedding_dimensions or 2048

    def embed(self, text: str) -> list[float]:
        """对单条文本生成 2048 维向量。空文本直接抛出 ValueError。"""
        if not text or not text.strip():
            raise ValueError("embedding input must not be empty")
        return self._call_api([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量生成向量。空列表直接返回 []，网络错误返回 []（降级）。"""
        if not texts:
            return []
        non_empty = [t for t in texts if t.strip()]
        if not non_empty:
            raise ValueError("all embedding inputs are empty")
        return self._call_api(non_empty)

    # ---------- 内部实现 ----------

    def _call_api(self, inputs: list[str]) -> list[list[float]]:
        """调用智谱 /embeddings 端点。网络/API 错误返回 [] 不抛异常。"""
        url = f"{self._base_url}/{self._path}"
        body = json.dumps({
            "model": self._model,
            "input": inputs,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            # 嵌入失败不阻塞主流程，调用方自行降级
            print(f"[EmbeddingService] API call failed: {exc}")
            return []
        # 智谱返回格式：{"data": [{"embedding": [...]}, ...]}
        embeddings = data.get("data", [])
        return [item["embedding"] for item in embeddings]


class MockEmbeddingService:
    """确定性 Mock Embedding 服务。

    用 hash(content) 生成 2048 维确定性向量：
    - 同一 query 始终返回相同向量
    - 不同 query 返回不同向量
    - 用于单元测试验证语义排序、混合检索等逻辑
    """

    DIM = 2048

    def embed(self, text: str) -> list[float]:
        """对单条文本生成确定性 2048 维向量。"""
        if not text or not text.strip():
            raise ValueError("mock embedding input must not be empty")
        return self._hash_to_vector(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量生成确定性向量。"""
        return [self.embed(t) for t in texts]

    @classmethod
    def _hash_to_vector(cls, text: str) -> list[float]:
        """用 SHA-256 哈希生成 2048 个确定性 float。

        流程：SHA-256(text) -> 32 字节 -> 重复展开 2048 个数值 -> L2 归一化。
        归一化保证向量在单位球面上，余弦距离等价于内积。
        """
        h = hashlib.sha256(text.encode("utf-8")).digest()
        # 用 struct 把 hash 展开为 32 个 float-like 数，再扩展到 2048 维
        seed = list(struct.unpack(f">{len(h)}B", h))
        # 扩展到 DIM 维
        vector: list[float] = []
        for i in range(cls.DIM):
            # 每个维度由两个 seed 值组合而成
            a = seed[i % len(seed)]
            b = seed[(i * 3 + 7) % len(seed)]
            value = float(a * 256 + b) / 65535.0 - 0.5
            vector.append(value)
        # L2 归一化
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0:
            return [0.0] * cls.DIM
        return [v / norm for v in vector]
