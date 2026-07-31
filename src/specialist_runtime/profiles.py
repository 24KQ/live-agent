"""Specialist Profile 的启动冻结配置与稳定身份。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
import hashlib
import re
from typing import Any

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from src.specialist_runtime.models import (
    SpecialistTaskKind,
    StrictFrozenModel,
    _freeze_json,
    _plain_json,
    canonical_json_sha256,
)


FORMAL_ENDPOINT_HOST = "api.deepseek.com"
# 正式 smoke（V1/V2 账本）模型身份：真实历史 run 的 receipts 均为 deepseek-v4-pro
# （2026-07-24 封印证据，成本按 3.0/6.0 定价），V1 账本 CHECK 与成本触发器同源，
# Python 侧身份常量必须与其一致，否则 Python 校验（flash）与 SQL 校验（pro）漂移。
FORMAL_MODEL_ID = "deepseek-v4-pro"
# 兼容历史的 Flash 常量仍是独立白名单成员；只允许列出的模型进入冻结 Profile，避免调用方
# 通过自由字符串把未审计的供应商或模型送入正式运行时。
DEEPSEEK_V4_FLASH_MODEL_ID = "deepseek-v4-flash"
DEEPSEEK_V4_PRO_MODEL_ID = "deepseek-v4-pro"
GPT_5_6_LUNA_MODEL_ID = "gpt-5.6-luna"
GPT_5_6_SOL_MODEL_ID = "gpt-5.6-sol"
GPT_5_6_TERRA_MODEL_ID = "gpt-5.6-terra"
FORMAL_ENDPOINT_HOSTS = frozenset({"api.deepseek.com", "synapse-ai.uk", "api.imagebridge.top"})
FORMAL_MODEL_IDS = frozenset({
    DEEPSEEK_V4_FLASH_MODEL_ID,
    DEEPSEEK_V4_PRO_MODEL_ID,
    GPT_5_6_LUNA_MODEL_ID,
    GPT_5_6_SOL_MODEL_ID,
    GPT_5_6_TERRA_MODEL_ID,
})
# 思考强度白名单：digest 认证集合，运行时只能在集合内挑选（env 透传到网关）。
# 集合之外的值由闭包内 V5 adapter 与 campaign 校验共同 fail-fast 拒绝。
FORMAL_REASONING_EFFORTS = frozenset({"medium", "high", "xhigh", "max"})


class FinalEvidenceBindingMode(StrEnum):
    """FINAL 输出与权威 EvidenceRef 的冻结绑定方式。"""

    SYSTEM_MANAGED_IDS = "SYSTEM_MANAGED_IDS"


def normalize_endpoint_host(value: str) -> str:
    """校验并规范化仅含 ASCII DNS hostname 的模型端点身份。"""

    labels = value.split(".")
    valid_labels = all(
        label
        and len(label) <= 63
        and label.isascii()
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )
    if (
        value != value.strip()
        or len(value) > 253
        or len(labels) < 2
        or not valid_labels
        or not labels[-1].isalpha()
    ):
        raise ValueError("endpoint_host must be a valid DNS hostname")
    return value.lower()


class SpecialistProfile(StrictFrozenModel):
    """某类 Specialist 的模型、权限、Schema 和绝对预算快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(..., min_length=1)
    profile_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    task_kind: SpecialistTaskKind
    model_id: str = Field(..., min_length=1)
    endpoint_host: str = Field(..., min_length=1)
    temperature: Decimal = Field(..., ge=Decimal("0"), le=Decimal("2"))
    prompt_text: str = Field(..., min_length=1)
    prompt_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    result_schema_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    result_schema: Any
    allowed_skill_ids: tuple[str, ...] = ()
    skill_versions: Mapping[str, str] = Field(default_factory=dict)
    max_model_calls: int = Field(..., ge=1, strict=True)
    max_skill_calls: int = Field(..., ge=0, strict=True)
    max_total_tokens: int = Field(..., ge=1, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    deadline_seconds: int = Field(..., ge=1, strict=True)
    max_case_cost_cny: Decimal = Field(..., gt=Decimal("0"))
    # None 表示历史完整 EvidenceRef 契约。摘要计算使用 exclude_none，因此新增字段不会
    # 追溯改变 V1 或生产 Profile 的既有身份。
    final_evidence_binding_mode: FinalEvidenceBindingMode | None = None
    profile_digest: str = ""

    @field_validator("endpoint_host")
    @classmethod
    def _validate_endpoint_host(cls, value: str) -> str:
        # 这里只接受 DNS hostname，不接受 URL authority、用户信息、端口、查询或锚点。
        # Task 2 Adapter 会在此可信值外拼接固定 HTTPS scheme 与固定 API path。
        normalized = normalize_endpoint_host(value)
        if normalized not in FORMAL_ENDPOINT_HOSTS:
            raise ValueError(f"endpoint_host must be one of {sorted(FORMAL_ENDPOINT_HOSTS)}")
        return normalized

    @field_validator("model_id")
    @classmethod
    def _validate_model_id(cls, value: str) -> str:
        if value not in FORMAL_MODEL_IDS:
            raise ValueError("model_id must be an approved DeepSeek formal model")
        return value

    @field_validator("temperature")
    @classmethod
    def _temperature_is_deterministic(cls, value: Decimal) -> Decimal:
        if value != Decimal("0"):
            raise ValueError("formal Specialist Profile requires temperature 0")
        return value

    @field_validator("allowed_skill_ids")
    @classmethod
    def _validate_skill_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("allowed_skill_ids cannot contain empty values")
        if len(value) != len(set(value)):
            raise ValueError("allowed_skill_ids must be unique")
        # 白名单是集合语义；规范排序避免仅配置顺序不同就产生新 Profile 身份。
        return tuple(sorted(value))

    @field_validator("skill_versions", mode="after")
    @classmethod
    def _freeze_skill_versions(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("skill_versions must be an object")
        if any(
            not isinstance(skill_id, str)
            or not skill_id
            or not isinstance(version, str)
            or re.fullmatch(r"\d+\.\d+\.\d+", version) is None
            for skill_id, version in value.items()
        ):
            raise ValueError("skill_versions must map non-empty skill IDs to semantic versions")
        # 版本映射属于 Profile 权限事实；深冻结可阻止调用方在摘要计算后替换版本。
        return _freeze_json(dict(sorted(value.items())))

    @field_validator("result_schema", mode="after")
    @classmethod
    def _freeze_result_schema(cls, value: Any) -> Any:
        return _freeze_json(value)

    @field_serializer("result_schema", when_used="json")
    def _serialize_result_schema(self, value: Any) -> Any:
        return _plain_json(value)

    @field_serializer("skill_versions", when_used="json")
    def _serialize_skill_versions(self, value: Any) -> Any:
        return _plain_json(value)

    @model_validator(mode="after")
    def _verify_profile_digest(self) -> "SpecialistProfile":
        calculated_prompt_hash = hashlib.sha256(self.prompt_text.encode("utf-8")).hexdigest()
        if self.prompt_hash != calculated_prompt_hash:
            raise ValueError("prompt_hash does not match prompt_text")
        if set(self.skill_versions) != set(self.allowed_skill_ids):
            raise ValueError("skill_versions must exactly cover allowed_skill_ids")

        # 输出 Schema 的独立哈希会写入评估 Manifest；必须先绑定真实 Schema，
        # 否则攻击者可以保留旧哈希却替换约束内容，使审计身份失去意义。
        calculated_schema_hash = canonical_json_sha256(self.result_schema)
        if self.result_schema_hash != calculated_schema_hash:
            raise ValueError("result_schema_hash does not match result_schema")

        if (
            self.max_output_tokens is not None
            and self.max_output_tokens > self.max_total_tokens
        ):
            raise ValueError("max_output_tokens cannot exceed max_total_tokens")

        # 旧 Profile 没有独立输出上限。为了使新增的可选字段不重签历史 Profile
        # 身份，摘要显式排除值为 None 的字段；Smoke Profile 设置数值后则必然被摘要绑定。
        payload = self.model_dump(
            mode="json",
            exclude={"profile_digest"},
            exclude_none=True,
        )
        calculated = canonical_json_sha256(payload)
        if self.profile_digest and self.profile_digest != calculated:
            raise ValueError("profile_digest does not match profile facts")
        object.__setattr__(self, "profile_digest", calculated)
        return self
