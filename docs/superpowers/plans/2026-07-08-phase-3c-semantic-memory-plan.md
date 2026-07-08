# Phase 3C 语义记忆检索实施计划

## 交付清单

1. 数据库迁移：embedding vector(1024)
2. EmbeddingService + MockEmbeddingService
3. SemanticMemoryRetriever + mixed_retrieve()
4. MemoryStore 内部嵌入 embedding 调用
5. 测试：unit x3 + integration x1
6. CLI 演示：seed + demo
7. 留迹文档

## TDD 红灯 -> 绿灯顺序

1. test_embedding_service.py（mock 先失败）
2. test_semantic_retrieval.py（mock 先失败）
3. test_memory_store.py 扩展（embedding 写入验证）
4. test_semantic_retrieval_flow.py（集成）
   -> 写入 3 条记忆 + embedding，验证语义排序
   -> 验证混合加权融合
   -> 验证 API 失败降级

## 验收命令

pytest tests/unit/test_embedding_service.py -v
pytest tests/unit/test_semantic_retrieval.py -v
pytest tests/unit/test_memory_store.py -v -k "embedding"
pytest tests/integration/test_semantic_retrieval_flow.py -v
pytest -v
python scripts/seed_phase3c_embeddings.py
python scripts/run_phase3c_semantic_demo.py
python scripts/check_infra.py
