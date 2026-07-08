# Phase 3C 语义记忆检索设计文档

## 1. 概述

Phase 3C 在 Phase 3A/3B 结构化记忆检索基础上，引入语义向量检索能力。
目标：主播偏好记忆中相似语义的内容可通过 embedding 相似度召回，
不再仅依赖精确的类目/标签/商品 ID 匹配。

核心链路：
- 写入记忆时：调用智谱 embedding-3 API，生成 1024 维向量，写入 pgvector
- 检索记忆时：将查询文本转为向量，pgvector 余弦距离，返回 Top-K
- 混合排序：0.6 x 语义分 + 0.4 x 结构化分，融合两边结果

## 2. 技术选型

- Embedding API：智谱（bigmodel）embedding-3 模型，1024 维
- 向量数据库：PostgreSQL + pgvector 0.8.4（已有）
- 数据库字段：embedding vector(1024)，从 vector(1536) 迁移
- HTTP 客户端：urllib（标准库），不引入 langchain/openai

## 3. 模块设计

### 3.1 EmbeddingService

- 封装智谱 embedding API 调用：/embeddings 端点
- 支持单条（str）和批量（list[str]）
- MockEmbeddingService：hash(content) 生成确定性 1024 维向量
- 网络错误/API 错误：返回空列表，不抛异常

### 3.2 SemanticMemoryRetriever

- query_text -> 向量 -> pgvector <=> 余弦距离 -> Top-K
- embedding 为 NULL 的记忆自动跳过
- mixed_retrieve()：加权融合语义和结构化结果

### 3.3 MemoryStore 改造

- write_memory() 内部自动调用 embedding API
- API 失败时 embedding 为 NULL，不阻塞写入
