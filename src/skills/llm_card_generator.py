"""Phase 3E LLM 手卡话术生成器。

封装 DeepSeek（deepseek-v4-flash）chat completions API，为播前商品生成
更自然、更个性化的主播讲解手卡。LLM 失败时自动降级到确定性模板。

不引入 langchain 或 openai 库，用标准库 urllib 直接调用。
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from typing import Any

from src.config.settings import Settings
from src.skills.product_card_generator import ProductCard, generate_product_card
from src.skills.product_catalog import CatalogProduct


def build_card_prompt(product: CatalogProduct) -> str:
    """为商品构造 LLM handcard 的 system + user prompt。

    system prompt 引导模型输出标准 JSON 格式；
    user prompt 包含商品字段和生成要求。
    """
    return f"""你是一个专业的电商直播话术策划师。请为以下商品生成主播讲解手卡。

商品信息：
- 名称：{product.name}
- 分类：{product.category}
- 价格：{product.price:.2f} 元
- 标签：{", ".join(product.tags) if product.tags else "无"}

请返回严格的 JSON 格式，字段如下：
{{
    "product_id": "{product.product_id}",
    "title": "吸引人的手卡标题",
    "talking_points": ["卖点1", "卖点2", "卖点3"],
    "opening_script": "亲切自然的开场话术",
    "price_hint": "价格相关的促单话术",
    "risk_tips": ["使用注意事项或售后提示"]
}}

要求：
- talking_points 至少 2 条，每条 10-40 字
- opening_script 30-80 字，口语化
- price_hint 突出性价比或优惠
- risk_tips 如有必要才写，否则空数组
- 只返回 JSON，不要加其他文字"""


def parse_llm_card_response(llm_output: str, expected_product_id: str) -> ProductCard:
    """解析 LLM 返回的 JSON 字符串，校验后返回 ProductCard。

    - 先去注释/首尾空白
    - 尝试从 markdown 代码块中提取 JSON
    - 通过 Pydantic ProductCard.model_validate 校验
    - product_id 必须与 expected_product_id 一致
    """
    text = llm_output.strip()

    # 处理 markdown 代码块包裹的情况
    if text.startswith("```"):
        lines = text.split("\n")
        # 去掉首尾的 ```
        json_lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(json_lines)

    # 查找第一个 { 和最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM response does not contain valid JSON: {text[:200]}")

    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"failed to parse LLM JSON: {exc}") from exc

    if raw.get("product_id") != expected_product_id:
        raise ValueError(
            f"LLM returned product_id={raw.get('product_id')}, expected={expected_product_id}"
        )

    return ProductCard.model_validate(raw)


class LLMCardGenerator:
    """DeepSeek chat completions API 封装。

    用法：
        gen = LLMCardGenerator(settings=settings)
        card = gen.generate_card_with_fallback(product)
    """

    def __init__(self, settings: Settings | None = None, api_key: str = "") -> None:
        if settings is None and not api_key:
            from src.config.settings import get_settings
            settings = get_settings()
        if settings:
            self._base_url = (settings.llm_api_base_url or "https://api.deepseek.com").rstrip("/")
            self._api_key = settings.llm_api_key or api_key
            self._model = settings.llm_model or "deepseek-v4-flash"
            self._max_tokens = settings.llm_max_tokens or 500
            self._temperature = settings.llm_temperature or 0.3
            self._timeout = settings.llm_timeout_seconds or 15
        else:
            self._base_url = "https://api.deepseek.com"
            self._api_key = api_key
            self._model = "deepseek-v4-flash"
            self._max_tokens = 500
            self._temperature = 0.3
            self._timeout = 15

    def generate_card_with_fallback(self, product: CatalogProduct) -> ProductCard:
        """先调 LLM，失败降级到确定性模板。

        无论成功或降级，都返回 ProductCard。
        """
        try:
            prompt = build_card_prompt(product)
            response = self._call_llm(prompt)
            return parse_llm_card_response(response, product.product_id)
        except Exception:
            # 任何失败都降级到模板
            return generate_product_card(product)

    def _call_llm(self, user_prompt: str) -> str:
        """调用 DeepSeek chat completions，返回模型生成的文本。"""
        url = f"{self._base_url}/chat/completions"
        body = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": "你是一个专业的电商直播话术策划师。请只返回 JSON，不要加其他文字。"},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"LLM API call failed: {exc}") from exc

        return data["choices"][0]["message"]["content"]
