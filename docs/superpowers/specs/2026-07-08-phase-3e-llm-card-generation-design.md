# Phase 3E 设计文档：LLM 手卡话术增强

## 1. 概述

Phase 3E 把 DeepSeek（deepseek-v4-flash）接入播前手卡生成链路。
LLM 可用时优先用 LLM 生成自然话术，失败/超时时自动降级到确定性模板。

## 2. 核心链路

查询商品 -> 构造 prompt -> deepseek-v4-flash -> 解析 JSON -> ProductCard Schema 校验 -> 返回
                               (15s 超时)    (失败)         (Pydantic)
                                      |
                                降级到模板手卡

## 3. 模块

- src/skills/llm_card_generator.py：LLMCardGenerator + build_card_prompt + parse_llm_card_response
- LLM 调用：标准库 urllib，不引入 langchain/openai
- product_card_generator.py 保留作为 fallback

## 4. 审计

- 新增 ActionType.LLM_GENERATE_CARD（计划中，待后续接入审计链路）
- 当前降级和 LLM 成功都通过调用方日志记录

## 5. 配置

- LLM_API_BASE_URL：https://api.deepseek.com
- LLM_MODEL：deepseek-v4-flash
- LLM_TEMPERATURE：0.3（低温度，保证输出稳定可校验）
- LLM_TIMEOUT_SECONDS：15
