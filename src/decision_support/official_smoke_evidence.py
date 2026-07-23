"""Phase 16 正式真实模型 smoke 的离线身份、Profile 和预检契约。

本模块不读取 .env、不创建数据库连接，也不拥有模型端口。它只冻结正式 run 的公开身份，
让后续账本与 Runner 能在发送前验证价格、数据集、源码和运行环境是否仍为同一份事实。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Any
import weakref

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from src.decision_support.multi_agent import (
    PHASE16_SMOKE_EVIDENCE_ANALYST_PROFILE_ID,
    PHASE16_SMOKE_EVIDENCE_PLANNER_PROFILE_ID,
    build_phase16_smoke_evidence_analyst_profile,
    build_phase16_smoke_evidence_planner_profile,
)
from src.decision_support.multi_agent_evaluation import (
    Phase16EvaluationDataset,
    _validate_dataset_for_run,
)
from src.specialist_runtime.model_port import ModelSuccess
from src.specialist_runtime.models import StrictFrozenModel, _freeze_json, _plain_json, canonical_json_sha256
from src.specialist_runtime.profiles import FORMAL_ENDPOINT_HOST, FORMAL_MODEL_ID, normalize_endpoint_host
from src.specialist_runtime.registry import SpecialistProfileRegistry


PHASE16_OFFICIAL_SMOKE_RUN_ID = "phase16-official-smoke-v1"
PHASE16_OFFICIAL_SMOKE_EVIDENCE_MANIFEST_ID = "phase16-official-smoke-evidence-v1"
FORMAL_OFFICIAL_SMOKE_MANIFEST_PATH = Path(
    "evaluation/manifests/phase16-official-smoke-evidence-v1.json"
)
# v1 Manifest 已在唯一正式执行前冻结；其中的八个文件只标识执行入口、账本、Runner 和
# 模型端口的直接身份，不能再被描述为完整的一方源码闭包。历史完整闭包由下方独立审计
# 资产以执行提交的 Git blob 固化，绝不回写该 Manifest。
FORMAL_OFFICIAL_SMOKE_EXECUTION_IDENTITY_PATHS = (
    "src/decision_support/multi_agent.py",
    "src/decision_support/official_smoke_evidence.py",
    "src/decision_support/official_smoke_runner.py",
    "src/decision_support/official_smoke_ledger.py",
    "src/specialist_runtime/deepseek_adapter.py",
    "src/specialist_runtime/model_port.py",
    "src/specialist_runtime/profiles.py",
    "src/specialist_runtime/runner.py",
)
# 执行命令、六角色 Evidence 投影、共享 Runner、模型适配器及其模块级一方依赖的完整
# 历史闭包。列表按路径稳定排序；它只服务于既有 v1 运行的回溯审计，不会成为新的 LIVE
# 路由、自动发现规则或可重试 smoke 配置。
PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_PATHS = (
    "src/config/settings.py",
    "src/core/security_hooks.py",
    "src/decision_support/evidence.py",
    "src/decision_support/models.py",
    "src/decision_support/multi_agent.py",
    "src/decision_support/multi_agent_evaluation.py",
    "src/decision_support/official_smoke_evidence.py",
    "src/decision_support/official_smoke_ledger.py",
    "src/decision_support/official_smoke_runner.py",
    "src/decision_support/proposal.py",
    "src/decision_support/store.py",
    "src/plan_engine/event_state_machine.py",
    "src/plan_engine/events.py",
    "src/plan_engine/models.py",
    "src/skill_runtime/models.py",
    "src/skills/live_plan_generator.py",
    "src/skills/product_catalog.py",
    "src/specialist_runtime/budget.py",
    "src/specialist_runtime/deepseek_adapter.py",
    "src/specialist_runtime/evidence.py",
    "src/specialist_runtime/live_ops.py",
    "src/specialist_runtime/model_port.py",
    "src/specialist_runtime/models.py",
    "src/specialist_runtime/profiles.py",
    "src/specialist_runtime/registry.py",
    "src/specialist_runtime/runner.py",
    "src/specialist_runtime/scripted_model.py",
    "src/state/models.py",
)
PHASE16_OFFICIAL_SMOKE_EXECUTION_COMMIT = "a2e70a78301f10075b57040a5b8a1b9e2a34134d"
PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_AUDIT_ID = (
    "phase16-official-smoke-historical-closure-audit-v1"
)
PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_AUDIT_PATH = Path(
    "evaluation/manifests/phase16-official-smoke-historical-closure-audit-v1.json"
)
FORMAL_INPUT_PRICE_CNY_PER_MILLION = Decimal("1.000000")
FORMAL_OUTPUT_PRICE_CNY_PER_MILLION = Decimal("2.000000")
FORMAL_SMOKE_CASE_COUNT = 10
# 对外模块使用正式 evidence 名称；底层 Profile 工厂仍保留简短常量，避免生产协调器
# 误把 Smoke Profile 作为 LIVE Profile 身份。
PHASE16_OFFICIAL_SMOKE_EVIDENCE_ANALYST_PROFILE_ID = (
    PHASE16_SMOKE_EVIDENCE_ANALYST_PROFILE_ID
)
PHASE16_OFFICIAL_SMOKE_EVIDENCE_PLANNER_PROFILE_ID = (
    PHASE16_SMOKE_EVIDENCE_PLANNER_PROFILE_ID
)


class Phase16OfficialSmokeStatus(StrEnum):
    """正式 smoke 预检的封闭状态；它不表达生产路由或经营授权。"""

    READY = "READY"
    BLOCKED = "BLOCKED"


class Phase16OfficialSmokeReceiptError(ValueError):
    """正式 smoke 的已发送响应缺少必须审计的 Provider 回执。"""


def _digest(value: Any) -> str:
    """将只含公开元数据的 JSON 规范化并哈希，绝不接受 Prompt 或模型正文。"""

    encoded = json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _normalize_price(value: Decimal) -> Decimal:
    """统一到账本使用的六位小数，拒绝非有限或超精度价格。"""

    candidate = Decimal(value)
    if not candidate.is_finite() or candidate < 0:
        raise ValueError("official price must be finite and non-negative")
    normalized = candidate.quantize(Decimal("0.000001"))
    if candidate != normalized:
        raise ValueError("official price must use six decimal places or fewer")
    return normalized


class Phase16OfficialPriceEvidence(StrictFrozenModel):
    """官方价格的最小不可变快照，不保存网页正文、链接或认证信息。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(..., min_length=1)
    endpoint_host: str = Field(..., min_length=1)
    input_cny_per_million: Decimal = Field(..., ge=0)
    output_cny_per_million: Decimal = Field(..., ge=0)
    official_price_digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @field_validator("endpoint_host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        """价格身份也必须只接受正式 DNS host，不能混入 URL 或端口。"""

        return normalize_endpoint_host(value)

    @field_validator("input_cny_per_million", "output_cny_per_million")
    @classmethod
    def _validate_price(cls, value: Decimal) -> Decimal:
        """保存前规范价格精度，使不同 Decimal 表示不生成不同证据摘要。"""

        return _normalize_price(value)

    @model_validator(mode="after")
    def _verify_digest(self) -> "Phase16OfficialPriceEvidence":
        """把模型、端点和两项价格一起绑定到价格证据摘要。"""

        payload = {
            "model_id": self.model_id,
            "endpoint_host": self.endpoint_host,
            "input_cny_per_million": str(self.input_cny_per_million),
            "output_cny_per_million": str(self.output_cny_per_million),
        }
        calculated = _digest(payload)
        if self.official_price_digest and self.official_price_digest != calculated:
            raise ValueError("official_price_digest does not match price facts")
        object.__setattr__(self, "official_price_digest", calculated)
        return self

    @classmethod
    def create(
        cls,
        *,
        model_id: str,
        endpoint_host: str,
        input_cny_per_million: Decimal,
        output_cny_per_million: Decimal,
    ) -> "Phase16OfficialPriceEvidence":
        """显式构造经摘要校验的公开价格证据，调用方不能手填摘要。"""

        return cls(
            model_id=model_id,
            endpoint_host=endpoint_host,
            input_cny_per_million=input_cny_per_million,
            output_cny_per_million=output_cny_per_million,
        )


class Phase16OfficialSmokeEnvironment(StrictFrozenModel):
    """可信启动装配传入的非敏感环境身份，不承载 API key 内容。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(..., min_length=1)
    endpoint_host: str = Field(..., min_length=1)
    credential_configured: bool

    @field_validator("endpoint_host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        """预检仅比较规范 host，避免 URL 字符串表示差异绕过身份校验。"""

        return normalize_endpoint_host(value)


class Phase16OfficialSmokeEvidenceManifest(StrictFrozenModel):
    """正式十例 smoke 的版本化静态身份，不包含模型可见正文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = Field(..., min_length=1)
    schema_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    run_id: str = Field(..., min_length=1)
    parent_dataset_id: str = Field(..., min_length=1)
    parent_manifest_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    parent_dataset_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    case_ids: tuple[str, ...] = Field(..., min_length=1)
    case_digests: Mapping[str, str]
    profile_digests: Mapping[str, str]
    official_price_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    input_cny_per_million: Decimal = Field(..., ge=0)
    output_cny_per_million: Decimal = Field(..., ge=0)
    source_file_digests: Mapping[str, str]
    runner_contract_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @field_validator("case_ids")
    @classmethod
    def _validate_case_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """十例顺序本身是 run 事实，既不能重复也不能以集合比较掩盖调换。"""

        if len(value) != FORMAL_SMOKE_CASE_COUNT or len(value) != len(set(value)):
            raise ValueError("formal smoke manifest must contain exactly ten unique case IDs")
        if any(not item for item in value):
            raise ValueError("formal smoke case IDs cannot be empty")
        return value

    @field_validator("case_digests", "profile_digests", "source_file_digests", mode="after")
    @classmethod
    def _freeze_digest_map(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        """映射属于 Manifest 身份；深冻结防止摘要生成后被调用方替换。"""

        if not isinstance(value, Mapping) or not value:
            raise ValueError("formal smoke digest map must be a non-empty object")
        normalized = dict(sorted(value.items()))
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for key, digest in normalized.items()
        ):
            raise ValueError("formal smoke digest maps require non-empty keys and SHA-256 values")
        return _freeze_json(normalized)

    @field_serializer("case_digests", "profile_digests", "source_file_digests", when_used="json")
    def _serialize_digest_map(self, value: Mapping[str, str]) -> Mapping[str, str]:
        """把冻结映射还原为标准 JSON，保证文件摘要跨进程稳定。"""

        return _plain_json(value)

    @model_validator(mode="after")
    def _verify_identity(self) -> "Phase16OfficialSmokeEvidenceManifest":
        """校验固定 run 常量、映射覆盖和最终 Manifest 摘要。"""

        if self.manifest_id != PHASE16_OFFICIAL_SMOKE_EVIDENCE_MANIFEST_ID:
            raise ValueError("formal smoke manifest identity is frozen")
        if self.run_id != PHASE16_OFFICIAL_SMOKE_RUN_ID:
            raise ValueError("formal smoke run identity is frozen")
        if set(self.case_digests) != set(self.case_ids):
            raise ValueError("formal smoke case digests must exactly cover case IDs")
        if set(self.profile_digests) != {"analyst", "planner"}:
            raise ValueError("formal smoke manifest must bind exactly analyst and planner profiles")
        payload = self.model_dump(mode="json", exclude={"manifest_digest"})
        calculated = canonical_json_sha256(payload)
        if self.manifest_digest and self.manifest_digest != calculated:
            raise ValueError("formal smoke manifest_digest does not match facts")
        object.__setattr__(self, "manifest_digest", calculated)
        return self


class Phase16OfficialSmokeHistoricalClosureAudit(StrictFrozenModel):
    """对唯一 v1 执行提交的完整一方源码闭包进行只读复核。

    历史 Manifest 本身不能再被修订，因此该模型把“直接执行身份子集”与“完整模块级
    闭包”分开保存。两类摘要都必须从同一 Git commit 的 blob 重算，而不是读取当前
    工作树，避免后续安全整改意外重写过去真实请求的证据含义。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    audit_id: str = Field(..., min_length=1)
    schema_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    execution_commit: str = Field(..., pattern=r"^[0-9a-f]{40}$")
    execution_manifest_path: str = Field(..., min_length=1)
    execution_manifest_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    execution_identity_paths: tuple[str, ...] = Field(..., min_length=1)
    execution_identity_file_digests: Mapping[str, str]
    source_file_digests: Mapping[str, str]
    audit_digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @field_validator("execution_identity_file_digests", "source_file_digests", mode="after")
    @classmethod
    def _freeze_digest_map(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        """规范化摘要映射，禁止空键、非 SHA-256 值或调用方可变字典进入审计对象。"""

        if not isinstance(value, Mapping) or not value:
            raise ValueError("historical closure digest map must be a non-empty object")
        normalized = dict(sorted(value.items()))
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for key, digest in normalized.items()
        ):
            raise ValueError("historical closure digest maps require SHA-256 values")
        return _freeze_json(normalized)

    @field_serializer(
        "execution_identity_file_digests",
        "source_file_digests",
        when_used="json",
    )
    def _serialize_digest_map(self, value: Mapping[str, str]) -> Mapping[str, str]:
        """以稳定 JSON 还原深冻结映射，保证审计摘要在不同进程中保持一致。"""

        return _plain_json(value)

    @model_validator(mode="after")
    def _verify_identity(self) -> "Phase16OfficialSmokeHistoricalClosureAudit":
        """绑定唯一历史运行、原 Manifest 与预先审查的完整闭包，拒绝改名或缩减。"""

        if self.audit_id != PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_AUDIT_ID:
            raise ValueError("historical closure audit identity is frozen")
        if self.schema_version != "1.0.0":
            raise ValueError("historical closure audit schema version is frozen")
        if self.execution_commit != PHASE16_OFFICIAL_SMOKE_EXECUTION_COMMIT:
            raise ValueError("historical closure audit execution commit is frozen")
        if self.execution_manifest_path != FORMAL_OFFICIAL_SMOKE_MANIFEST_PATH.as_posix():
            raise ValueError("historical closure audit manifest path is frozen")
        if self.execution_identity_paths != FORMAL_OFFICIAL_SMOKE_EXECUTION_IDENTITY_PATHS:
            raise ValueError("historical closure audit execution identity paths are frozen")
        if set(self.execution_identity_file_digests) != set(self.execution_identity_paths):
            raise ValueError("historical closure audit identity digests must cover identity paths")
        if set(self.source_file_digests) != set(PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_PATHS):
            raise ValueError("historical closure audit must cover the complete frozen path set")
        if not set(self.execution_identity_paths) <= set(self.source_file_digests):
            raise ValueError("historical closure audit must include every execution identity path")
        if any(
            self.execution_identity_file_digests[path] != self.source_file_digests[path]
            for path in self.execution_identity_paths
        ):
            raise ValueError("historical closure identity digest conflicts with complete closure")
        payload = self.model_dump(mode="json", exclude={"audit_digest"})
        calculated = canonical_json_sha256(payload)
        if self.audit_digest and self.audit_digest != calculated:
            raise ValueError("historical closure audit_digest does not match facts")
        object.__setattr__(self, "audit_digest", calculated)
        return self


class Phase16OfficialSmokePreflight(StrictFrozenModel):
    """离线预检结果；后续正式 Runner 只能消费已验证的 READY 结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Phase16OfficialSmokeStatus
    can_send: bool
    reason_codes: tuple[str, ...] = ()
    manifest_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @property
    def provenance_verified(self) -> bool:
        """只有模块内预检工厂产生的结果才可被正式 Runner 视为可发送许可。"""

        # Pydantic 的 model_construct 可以绕过字段和 PrivateAttr 初始化；因此可信性不
        # 存在模型对象自身，而是由本模块弱引用表确认“此精确对象是否真的由工厂签发”。
        return _VERIFIED_PREFLIGHTS.get(id(self)) is self


_VERIFIED_PREFLIGHTS: weakref.WeakValueDictionary[int, Phase16OfficialSmokePreflight] = (
    weakref.WeakValueDictionary()
)


def _verified_preflight(**facts: Any) -> Phase16OfficialSmokePreflight:
    """集中设置私有 provenance，公共 Pydantic 构造不能绕过磁盘 Manifest 复验。"""

    result = Phase16OfficialSmokePreflight.model_validate(facts)
    # 只登记这个由工厂刚创建的实例；复制、反序列化和 model_construct 结果均不会继承。
    _VERIFIED_PREFLIGHTS[id(result)] = result
    return result


def build_phase16_official_smoke_profile_registry() -> SpecialistProfileRegistry:
    """构造只含两份 Smoke Profile 的独立 Registry，绝不修改生产 Registry。"""

    return SpecialistProfileRegistry(
        (
            build_phase16_smoke_evidence_analyst_profile(),
            build_phase16_smoke_evidence_planner_profile(),
        )
    )


def _source_digest(repository_root: Path, relative_path: str) -> str:
    """读取当前执行身份文件的规范摘要，供首次发送前的 v1 Manifest 重建使用。"""

    root = repository_root.resolve()
    raw_candidate = root / relative_path
    # 必须在 resolve 前检查 symlink；resolve 后目标文件会掩盖原路径的链接事实，
    # 使 Manifest 看似绑定仓库源码、实际却可读取仓库外可变内容。
    if raw_candidate.is_symlink():
        raise ValueError("formal execution identity file must not be a symlink")
    candidate = raw_candidate.resolve(strict=True)
    source_root = (root / "src").resolve(strict=True)
    if not candidate.is_relative_to(source_root) or candidate.suffix != ".py":
        raise ValueError("formal execution identity path must be a non-symlink Python file under src")
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative_path],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if tracked.returncode != 0:
        raise ValueError("formal execution identity file must be Git tracked")
    raw = candidate.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise ValueError("formal execution identity file must be UTF-8 without BOM and LF only")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("formal execution identity file must be UTF-8") from error
    return sha256(raw).hexdigest()


def _runner_contract_digest() -> str:
    """冻结后续 Runner 必须保留的验证链，不用自由文本描述代替可审计身份。"""

    return _digest(
        {
            "runner": "BoundedSpecialistRunner",
            "call_order": ["CONFLICT_ANALYSIS", "LIVE_DECISION_PLANNING"],
            "required_validations": ["AgentAction", "JSON_SCHEMA", "EVIDENCE_REF"],
            "route": "MULTI_AGENT_READY",
            "retry_policy": "ZERO_RETRY_AFTER_SEND",
        }
    )


def build_phase16_official_smoke_evidence_manifest(
    *,
    repository_root: Path,
    dataset: Phase16EvaluationDataset,
    official_price: Phase16OfficialPriceEvidence,
) -> Phase16OfficialSmokeEvidenceManifest:
    """从已有冻结数据集和当前执行身份子集重建唯一正式 Manifest。"""

    _validate_dataset_for_run(dataset)
    case_ids = dataset.manifest.smoke_eligible_case_ids
    if len(case_ids) != FORMAL_SMOKE_CASE_COUNT:
        raise ValueError("Phase 16 source dataset must expose exactly ten smoke cases")
    analyst = build_phase16_smoke_evidence_analyst_profile()
    planner = build_phase16_smoke_evidence_planner_profile()
    return Phase16OfficialSmokeEvidenceManifest(
        manifest_id=PHASE16_OFFICIAL_SMOKE_EVIDENCE_MANIFEST_ID,
        schema_version="1.0.0",
        run_id=PHASE16_OFFICIAL_SMOKE_RUN_ID,
        parent_dataset_id=dataset.manifest.dataset_id,
        parent_manifest_digest=dataset.manifest.manifest_digest,
        parent_dataset_digest=dataset.manifest.dataset_digest,
        case_ids=case_ids,
        case_digests={case_id: dataset.manifest.case_digests[case_id] for case_id in case_ids},
        profile_digests={"analyst": analyst.profile_digest, "planner": planner.profile_digest},
        official_price_digest=official_price.official_price_digest,
        input_cny_per_million=official_price.input_cny_per_million,
        output_cny_per_million=official_price.output_cny_per_million,
        source_file_digests={
            path: _source_digest(repository_root, path)
            for path in FORMAL_OFFICIAL_SMOKE_EXECUTION_IDENTITY_PATHS
        },
        runner_contract_digest=_runner_contract_digest(),
    )


def load_phase16_official_smoke_evidence_manifest(
    *,
    repository_root: Path,
    manifest_path: Path = FORMAL_OFFICIAL_SMOKE_MANIFEST_PATH,
) -> Phase16OfficialSmokeEvidenceManifest:
    """加载静态 Manifest；调用方随后必须与当前重建身份比较，不能只信文件自摘要。"""

    path = manifest_path if manifest_path.is_absolute() else repository_root / manifest_path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("formal smoke manifest is unreadable") from error
    return Phase16OfficialSmokeEvidenceManifest.model_validate(payload)


def _read_git_blobs_at_execution_commit(
    *,
    repository_root: Path,
    relative_paths: tuple[str, ...],
) -> dict[str, bytes]:
    """批量读取固定执行提交中的精确 blob，并拒绝缺失、目录、symlink 或路径替换。

    历史审计不能依赖当前工作树，也不能只读 Git object ID：object ID 的算法可能随仓库
    设置变化，而证据合同要求的是每个源码字节的 SHA-256。先用 ``ls-tree`` 锁定普通文件
    模式，再通过单个 ``cat-file --batch`` 读取内容，既保留 Git 的不可变语义，也避免每个
    路径各启一个子进程造成审计顺序和性能不稳定。
    """

    root = repository_root.resolve()
    if not relative_paths or len(relative_paths) != len(set(relative_paths)):
        raise ValueError("historical closure paths must be non-empty and unique")
    normalized_paths = tuple(Path(path).as_posix() for path in relative_paths)
    if any(
        path.startswith("/")
        or ".." in Path(path).parts
        or not (path.startswith("src/") and path.endswith(".py") or path == FORMAL_OFFICIAL_SMOKE_MANIFEST_PATH.as_posix())
        for path in normalized_paths
    ):
        raise ValueError("historical closure contains an unsafe Git path")

    tree = subprocess.run(
        [
            "git",
            "ls-tree",
            "-r",
            "--full-tree",
            PHASE16_OFFICIAL_SMOKE_EXECUTION_COMMIT,
            "--",
            *normalized_paths,
        ],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if tree.returncode != 0:
        raise ValueError("historical execution tree is unavailable")
    tree_entries: dict[str, str] = {}
    for raw_line in tree.stdout.splitlines():
        try:
            metadata, raw_path = raw_line.split(b"\t", maxsplit=1)
            mode, object_type, _object_id = metadata.split(maxsplit=2)
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("historical execution tree returned an invalid entry") from error
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError("historical closure path must be a regular Git blob")
        tree_entries[path] = mode.decode("ascii")
    if set(tree_entries) != set(normalized_paths):
        raise ValueError("historical execution tree does not contain every audited path")

    specifications = "".join(
        f"{PHASE16_OFFICIAL_SMOKE_EXECUTION_COMMIT}:{path}\n" for path in normalized_paths
    ).encode("utf-8")
    batch = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        input=specifications,
        check=False,
        capture_output=True,
    )
    if batch.returncode != 0:
        raise ValueError("historical execution blobs are unavailable")
    output = batch.stdout
    cursor = 0
    blobs: dict[str, bytes] = {}
    for path in normalized_paths:
        line_end = output.find(b"\n", cursor)
        if line_end < 0:
            raise ValueError("historical execution blob header is truncated")
        header = output[cursor:line_end].split()
        cursor = line_end + 1
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError("historical execution blob is missing or not a file")
        try:
            blob_size = int(header[2])
        except ValueError as error:
            raise ValueError("historical execution blob size is invalid") from error
        body_end = cursor + blob_size
        if body_end >= len(output) or output[body_end:body_end + 1] != b"\n":
            raise ValueError("historical execution blob content is truncated")
        blobs[path] = output[cursor:body_end]
        cursor = body_end + 1
    if cursor != len(output):
        raise ValueError("historical execution blob stream contains unexpected data")
    return blobs


def load_phase16_official_smoke_historical_closure_audit(
    *,
    repository_root: Path,
) -> Phase16OfficialSmokeHistoricalClosureAudit:
    """加载版本化审计资产本身，并拒绝 BOM、混合换行或工作树 symlink。"""

    root = repository_root.resolve()
    path = root / PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_AUDIT_PATH
    if path.is_symlink():
        raise ValueError("historical closure audit must not be a symlink")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError("historical closure audit is unreadable") from error
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise ValueError("historical closure audit must use UTF-8 LF without BOM")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("historical closure audit is invalid JSON") from error
    return Phase16OfficialSmokeHistoricalClosureAudit.model_validate(payload)


def verify_phase16_official_smoke_historical_closure_audit(
    *,
    repository_root: Path,
) -> Phase16OfficialSmokeHistoricalClosureAudit:
    """用执行提交 Git blob 复验审计资产与历史 Manifest，失败即不返回部分结果。"""

    audit = load_phase16_official_smoke_historical_closure_audit(repository_root=repository_root)
    manifest_path = FORMAL_OFFICIAL_SMOKE_MANIFEST_PATH.as_posix()
    blobs = _read_git_blobs_at_execution_commit(
        repository_root=repository_root,
        relative_paths=(manifest_path, *PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_PATHS),
    )
    try:
        manifest = Phase16OfficialSmokeEvidenceManifest.model_validate(
            json.loads(blobs[manifest_path].decode("utf-8"))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("historical execution manifest is invalid") from error
    if manifest.manifest_digest != audit.execution_manifest_digest:
        raise ValueError("historical closure audit does not bind the execution manifest")
    if dict(manifest.source_file_digests) != dict(audit.execution_identity_file_digests):
        raise ValueError("historical closure audit does not reproduce execution identity digests")
    actual_digests = {
        path: sha256(blobs[path]).hexdigest()
        for path in PHASE16_OFFICIAL_SMOKE_HISTORICAL_CLOSURE_PATHS
    }
    if actual_digests != dict(audit.source_file_digests):
        raise ValueError("historical closure audit source blobs do not match execution commit")
    return audit


def validate_phase16_official_smoke_receipt(success: ModelSuccess) -> None:
    """正式已发送调用必须带 Provider ID 与 finish reason，缺任一项即不能进入账本。"""

    if not success.provider_response_id:
        raise Phase16OfficialSmokeReceiptError("formal smoke receipt requires provider_response_id")
    if not success.finish_reason:
        raise Phase16OfficialSmokeReceiptError("formal smoke receipt requires finish_reason")


def preflight_phase16_official_smoke_evidence(
    *,
    dataset: Phase16EvaluationDataset,
    official_price: Phase16OfficialPriceEvidence,
    environment: Phase16OfficialSmokeEnvironment,
) -> Phase16OfficialSmokePreflight:
    """离线重验 formal identity；本函数不探测 endpoint、不读取密钥也不发送请求。"""

    reasons: list[str] = []
    repository_root = Path(__file__).resolve().parents[2]
    # 预检不接收调用方 Manifest。磁盘中的版本化冻结文件是唯一权威基线，避免
    # model_construct 之类的同进程对象构造绕过完整 case/profile/source 事实。
    try:
        stored_manifest = load_phase16_official_smoke_evidence_manifest(
            repository_root=repository_root,
        )
    except (OSError, UnicodeError, ValueError):
        stored_manifest = None
        reasons.append("FORMAL_MANIFEST_UNREADABLE")
    try:
        expected = build_phase16_official_smoke_evidence_manifest(
            repository_root=repository_root,
            dataset=dataset,
            official_price=official_price,
        )
    except (OSError, UnicodeError, ValueError):
        expected = None
        reasons.append("FORMAL_MANIFEST_REBUILD_FAILED")
    if (
        stored_manifest is not None
        and expected is not None
        and stored_manifest.manifest_digest != expected.manifest_digest
    ):
        reasons.append("FORMAL_MANIFEST_MISMATCH")
    if environment.model_id != FORMAL_MODEL_ID:
        reasons.append("MODEL_ID_MISMATCH")
    if environment.endpoint_host != FORMAL_ENDPOINT_HOST:
        reasons.append("ENDPOINT_MISMATCH")
    if not environment.credential_configured:
        reasons.append("CREDENTIAL_UNAVAILABLE")
    if (
        official_price.model_id != FORMAL_MODEL_ID
        or official_price.endpoint_host != FORMAL_ENDPOINT_HOST
    ):
        reasons.append("OFFICIAL_PRICE_IDENTITY_MISMATCH")
    if (
        official_price.input_cny_per_million != FORMAL_INPUT_PRICE_CNY_PER_MILLION
        or official_price.output_cny_per_million != FORMAL_OUTPUT_PRICE_CNY_PER_MILLION
    ):
        reasons.append("OFFICIAL_PRICE_MISMATCH")
    if (
        stored_manifest is not None
        and official_price.official_price_digest != stored_manifest.official_price_digest
    ):
        reasons.append("OFFICIAL_PRICE_DIGEST_MISMATCH")
    unique_reasons = tuple(sorted(set(reasons)))
    # 文件不可读时不接受调用方摘要；使用仅由固定 Manifest 标识派生的稳定占位摘要，
    # 它只能用于无敏感信息的阻断报告，不能被解释为某份已加载资产的身份。
    report_manifest_digest = (
        stored_manifest.manifest_digest
        if stored_manifest is not None
        else _digest({"manifest_id": PHASE16_OFFICIAL_SMOKE_EVIDENCE_MANIFEST_ID})
    )
    return _verified_preflight(
        status=Phase16OfficialSmokeStatus.BLOCKED if unique_reasons else Phase16OfficialSmokeStatus.READY,
        can_send=not unique_reasons,
        reason_codes=unique_reasons,
        manifest_digest=report_manifest_digest,
    )
