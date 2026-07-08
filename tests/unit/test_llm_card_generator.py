"""Phase 3E LLM 手卡生成器单元测试。

验证 prompt 构建、LLM JSON 响应解析、Schema 校验和降级逻辑。
不依赖真实 DeepSeek API，用 mock 控制行为。
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from src.skills.product_card_generator import ProductCard
from src.skills.product_catalog import CatalogProduct


class TestBuildCardPrompt:
    """Prompt 构建测试。"""

    @staticmethod
    def _sample_product() -> CatalogProduct:
        return CatalogProduct(
            product_id="p001",
            name="智能净水壶",
            category="厨房电器",
            price=299.00,
            inventory=150,
            conversion_rate=0.12,
            commission_rate=0.10,
            tags=["高利润", "爆款"],
            description="三级过滤，大容量 3L，静音设计",
        )

    def test_prompt_contains_product_name_and_description(self):
        """prompt 应包含商品名称和描述。"""
        from src.skills.llm_card_generator import build_card_prompt
        product = self._sample_product()
        prompt = build_card_prompt(product)
        assert "智能净水壶" in prompt
        assert "299.00" in prompt
        assert "299.00" in prompt

    def test_prompt_asks_for_json_output(self):
        """prompt 应明确要求 JSON 格式输出。"""
        from src.skills.llm_card_generator import build_card_prompt
        product = self._sample_product()
        prompt = build_card_prompt(product)
        assert "JSON" in prompt or "json" in prompt.lower()


class TestParseLLMCardResponse:
    """LLM JSON 响应解析测试。"""

    def test_parses_valid_json_to_product_card(self):
        """合法 JSON 应正确解析为 ProductCard。"""
        from src.skills.llm_card_generator import parse_llm_card_response
        valid_json = json.dumps({
            "product_id": "p001",
            "title": "智能净水壶 健康饮水新选择",
            "talking_points": ["三级过滤，除菌率达99.9%", "3L大容量，一天一壶"],
            "opening_script": "宝宝们，今天给大家带来一款厨房神器！",
            "price_hint": "日常价399，今天直播间299到手",
            "risk_tips": ["滤芯每3个月更换一次"],
        }, ensure_ascii=False)
        card = parse_llm_card_response(valid_json, "p001")
        assert card.product_id == "p001"
        assert len(card.talking_points) == 2
        assert len(card.opening_script) > 0

    def test_rejects_json_with_wrong_product_id(self):
        """LLM 返回的 product_id 不对时必须拒绝。"""
        from src.skills.llm_card_generator import parse_llm_card_response
        wrong_json = json.dumps({
            "product_id": "p999",
            "title": "测试",
            "talking_points": ["点1"],
            "opening_script": "开场",
            "price_hint": "价格",
            "risk_tips": [],
        }, ensure_ascii=False)
        with pytest.raises(ValueError, match="product_id"):
            parse_llm_card_response(wrong_json, "p001")

    def test_rejects_invalid_json(self):
        """非 JSON 字符串应抛出 ValueError。"""
        from src.skills.llm_card_generator import parse_llm_card_response
        with pytest.raises(ValueError):
            parse_llm_card_response("not json at all", "p001")

    def test_rejects_missing_required_field(self):
        """缺少必要字段的 JSON 应被 Pydantic 拒绝。"""
        from src.skills.llm_card_generator import parse_llm_card_response
        incomplete = json.dumps({
            "product_id": "p001",
            # 缺少 title、talking_points 等
        }, ensure_ascii=False)
        with pytest.raises(ValueError):
            parse_llm_card_response(incomplete, "p001")


class TestLLMCardGeneratorFallback:
    """降级逻辑测试。"""

    @staticmethod
    def _sample_product() -> CatalogProduct:
        return CatalogProduct(
            product_id="p001",
            name="测试商品",
            category="测试",
            price=99.00,
            inventory=10,
            conversion_rate=0.01,
            commission_rate=0.05,
            tags=["测试"],
            description="测试描述",
        )

    def test_generate_card_with_fallback_returns_valid_card(self):
        """即使 LLM 不可用（用 mock 模拟失败），也应返回有效 ProductCard。"""
        from unittest.mock import patch
        from src.skills.llm_card_generator import LLMCardGenerator

        generator = LLMCardGenerator(api_key="bad_key")
        # 模拟 API 调用失败
        with patch.object(generator, "_call_llm", side_effect=Exception("API down")):
            card = generator.generate_card_with_fallback(self._sample_product())
        assert isinstance(card, ProductCard)
        assert card.product_id == "p001"
        assert len(card.talking_points) > 0
