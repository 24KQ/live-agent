"""从 Phase 16 正式 PostgreSQL 账本渲染脱敏真实模型证据 Addendum。

该脚本是正式 run 之后唯一的只读报告入口。它不创建模型适配器、不调用网络、
不读取或输出 API Key，也不修改 append-only 账本；仅查询预先白名单化的 receipt、
validation 和 outcome 字段，再生成可提交的 Markdown 证据文档。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from uuid import UUID

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    # 脚本可以从任意工作目录执行，但只允许导入当前仓库；不搜索用户目录、插件目录或
    # 外部脚本，从而避免报告过程被未知 Python 模块替换。
    sys.path.insert(0, str(_PROJECT_ROOT))

import psycopg
from psycopg.rows import dict_row

from src.decision_support.official_smoke_ledger import (
    Phase16OfficialSmokeDispatchStage,
    Phase16OfficialSmokeReceiptAuthenticator,
    PostgresPhase16OfficialSmokeLedger,
)

_FORMAL_RUN_ID = "phase16-official-smoke-v1"
_HISTORICAL_DIRECT_MODE_SOURCE = "HISTORICAL_DIRECT_MODE"
_REQUIRED_CASE_COUNT = 10
_REQUIRED_CALL_COUNT = 20


@dataclass(frozen=True)
class OfficialSmokeReceipt:
    """供文档展示的单条最小 Provider 回执，不包含原始 provider ID 或模型正文。"""

    case_id: str
    stage: str
    profile_digest: str
    provider_response_id_digest: str
    finish_reason: str
    model_id: str
    response_digest: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: Decimal
    input_cost_cny: Decimal
    output_cost_cny: Decimal
    total_cost_cny: Decimal


@dataclass(frozen=True)
class OfficialSmokeValidation:
    """账本中独立写入的结构、AgentAction 与 EvidenceRef 校验摘要。"""

    case_id: str
    stage: str
    verdict: str
    reason_code: str
    validation_digest: str


@dataclass(frozen=True)
class OfficialSmokeOutcome:
    """固定 slot 的唯一终态，只携带状态码和不可变摘要。"""

    case_id: str
    status: str
    reason_code: str
    outcome_digest: str


@dataclass(frozen=True)
class OfficialSmokeCaseClaim:
    """正式账本中已经预约的固定 case，不保留 claim UUID 或其他内部请求标识。"""

    case_id: str


@dataclass(frozen=True)
class OfficialSmokeDispatchAttempt:
    """正式账本中已经创建的调度事实，仅保留报告需要的无敏感完备性状态。"""

    case_id: str
    stage: str
    profile_digest: str
    has_provider_receipt: bool
    has_validation_fact: bool


@dataclass(frozen=True)
class OfficialSmokeEvidenceSnapshot:
    """从 append-only 表重建的正式报告投影，不能作为写入或重试输入。"""

    run_id: str
    manifest_digest: str
    total_budget_cny: Decimal
    historical_spend_cny: Decimal
    fixed_case_slot_count: int
    maximum_exposure_cny: Decimal
    receipts: tuple[OfficialSmokeReceipt, ...]
    validations: tuple[OfficialSmokeValidation, ...]
    outcomes: tuple[OfficialSmokeOutcome, ...]
    # claim/attempt 投影用于从账本事实推导已消费 slot 与已创建阶段，不能再靠报告正文硬编码。
    claims: tuple[OfficialSmokeCaseClaim, ...] = ()
    attempts: tuple[OfficialSmokeDispatchAttempt, ...] = ()
    # 只有正式账本的公开 HMAC 消费接口确认过的 PASS case 才能进入该集合。
    authenticated_pass_case_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OfficialSmokeEvidenceReport:
    """唯一正式读取路径产生的脱敏报告结果，不向调用方公开可写 snapshot。"""

    run_id: str
    status: str
    markdown: str


@dataclass(frozen=True)
class _ReadOnlyReportSettings:
    """报告器的窄数据库设置，强制每条连接以 PostgreSQL 只读事务启动。

    该类型故意不复用应用级 ``Settings``：应用配置包含模型和其他基础设施凭据，而报告器
    只需要 PostgreSQL 连接信息与独立 receipt HMAC。返回副本避免调用方在运行中篡改选项。
    """

    _connection_kwargs: Mapping[str, Any]

    @property
    def postgres_connection_kwargs(self) -> dict[str, Any]:
        """返回带 libpq 只读开关的连接参数副本，禁止共享可写字典。"""

        return dict(self._connection_kwargs)


def _decimal(value: object) -> Decimal:
    """把驱动返回的数值规范为 Decimal，拒绝空值以避免把缺失费用误写为零。"""

    if value is None:
        raise ValueError("formal evidence row contains a required null decimal")
    return Decimal(str(value))


def _text(row: dict[str, Any], key: str) -> str:
    """只接受非空字符串字段，防止数据库缺列或 NULL 被静默渲染成可接受证据。"""

    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"formal evidence row has no {key}")
    return value


def _uuid_text(row: dict[str, Any], key: str) -> str:
    """将 psycopg 的 UUID 对象或文本 UUID 规范为账本 HMAC 使用的小写字符串。

    PostgreSQL UUID 列由 psycopg 默认还原为 ``UUID``，不能交给只接受字符串的报告字段
    校验器。这里仍强制执行 UUID 语法复验，避免任意自由文本被带入 HMAC 认证边界。
    """

    value = row.get(key)
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"formal evidence row has no valid {key}") from error


def _formal_conclusion(snapshot: OfficialSmokeEvidenceSnapshot) -> str:
    """从不可变事实计算严格结论，绝不以部分成功或低费用替代 10/10 门槛。"""

    claim_case_ids = frozenset(claim.case_id for claim in snapshot.claims)
    expected_attempt_keys = frozenset(
        (case_id, stage)
        for case_id in claim_case_ids
        for stage in ("ANALYST", "PLANNER")
    )
    attempt_keys = tuple((attempt.case_id, attempt.stage) for attempt in snapshot.attempts)
    receipt_keys = tuple((receipt.case_id, receipt.stage) for receipt in snapshot.receipts)
    validation_keys = tuple((validation.case_id, validation.stage) for validation in snapshot.validations)
    outcome_case_ids = frozenset(outcome.case_id for outcome in snapshot.outcomes)
    passed_case_ids = frozenset(
        outcome.case_id for outcome in snapshot.outcomes if outcome.status == "PASS"
    )
    # 固定十例的 PASS 不仅需要数量正确，还需要每一例的 claim、双阶段 dispatch、receipt、
    # validation 与 outcome 一一闭合。任何重复、缺失或跨 case 引用都使成功证据 fail-closed。
    pass_fact_chain_coherent = (
        len(claim_case_ids) == len(snapshot.claims)
        and len(set(attempt_keys)) == len(attempt_keys)
        and len(set(receipt_keys)) == len(receipt_keys)
        and len(set(validation_keys)) == len(validation_keys)
        and len(outcome_case_ids) == len(snapshot.outcomes)
        and frozenset(attempt_keys) == expected_attempt_keys
        and frozenset(receipt_keys) == expected_attempt_keys
        and frozenset(validation_keys) == expected_attempt_keys
        and outcome_case_ids == claim_case_ids
        and all(
            attempt.has_provider_receipt and attempt.has_validation_fact
            for attempt in snapshot.attempts
        )
    )
    # 发送前阻断不是“缺半条 PASS 链”的失败：它可以在 claim 前发生、在 Analyst intent
    # 尚未离开进程时发生，或在 Analyst 已验证后 Planner 尚未发送时发生。三种形态都没有
    # 已发送且失败的调用，因此必须保持 BLOCKED + INCONCLUSIVE；其他不完整拓扑仍 fail-closed。
    attempts_by_stage = {attempt.stage: attempt for attempt in snapshot.attempts}
    validations_by_stage = {validation.stage: validation for validation in snapshot.validations}
    blocked_outcome = snapshot.outcomes[0] if len(snapshot.outcomes) == 1 else None
    no_formal_facts = not (
        snapshot.claims
        or snapshot.attempts
        or snapshot.receipts
        or snapshot.validations
        or snapshot.outcomes
    )
    # “尚无事实”只有在正式账本已经完整创建十个固定 slot 时才是合法的发送前状态。
    # 若迁移中断、旧 schema 残留或直接 SQL 只创建了部分 slot，不能把这种不完整初始化
    # 降级成 INCONCLUSIVE；它没有满足 formal run 的最小实验拓扑，必须 fail-closed。
    valid_empty_pre_send_run = (
        no_formal_facts
        and snapshot.run_id == _FORMAL_RUN_ID
        and snapshot.fixed_case_slot_count == _REQUIRED_CASE_COUNT
        and snapshot.maximum_exposure_cny <= snapshot.total_budget_cny
    )
    single_blocked_case = (
        len(snapshot.claims) == 1
        and len(claim_case_ids) == 1
        and blocked_outcome is not None
        and blocked_outcome.status == "BLOCKED"
        and blocked_outcome.case_id in claim_case_ids
        # 所有 pre-send 事实必须属于唯一已 claim 的 slot；否则攻击者可以把某 case 的
        # BLOCKED outcome 与另一 case 的 attempt/validation 拼接，伪装成未发送结论。
        and all(attempt.case_id in claim_case_ids for attempt in snapshot.attempts)
        and all(validation.case_id in claim_case_ids for validation in snapshot.validations)
        and all(receipt.case_id in claim_case_ids for receipt in snapshot.receipts)
        and len(set(attempt_keys)) == len(attempt_keys)
        and len(set(validation_keys)) == len(validation_keys)
        and all(attempt.has_validation_fact for attempt in snapshot.attempts)
        and len(snapshot.receipts) <= 1
    )
    analyst_attempt = attempts_by_stage.get("ANALYST")
    planner_attempt = attempts_by_stage.get("PLANNER")
    analyst_validation = validations_by_stage.get("ANALYST")
    planner_validation = validations_by_stage.get("PLANNER")
    valid_pre_send_blocked_chain = single_blocked_case and (
        # run 在 claim 后、首个 dispatch 前停止时没有 attempt 或 validation。
        (not snapshot.attempts and not snapshot.validations and not snapshot.receipts)
        # Analyst intent 已记录，但 ModelPort 明确没有发送时，只允许无 receipt 的 BLOCKED。
        or (
            set(attempts_by_stage) == {"ANALYST"}
            and set(validations_by_stage) == {"ANALYST"}
            and analyst_attempt is not None
            and analyst_validation is not None
            and not analyst_attempt.has_provider_receipt
            and analyst_validation.verdict == "BLOCKED"
            and not snapshot.receipts
        )
        # Analyst 已成功后，Planner 尚未创建 attempt 就被本地边界阻断；已发送的 Analyst
        # 不是失败，不能因此把整体证据伪装为 FAILED。
        or (
            set(attempts_by_stage) == {"ANALYST"}
            and set(validations_by_stage) == {"ANALYST"}
            and analyst_attempt is not None
            and analyst_validation is not None
            and analyst_attempt.has_provider_receipt
            and analyst_validation.verdict == "PASS"
            and len(snapshot.receipts) == 1
            and snapshot.receipts[0].stage == "ANALYST"
        )
        # Analyst PASS 后 Planner intent 明确未发送，仍是合法的 pre-send BLOCKED 终态。
        or (
            set(attempts_by_stage) == {"ANALYST", "PLANNER"}
            and set(validations_by_stage) == {"ANALYST", "PLANNER"}
            and analyst_attempt is not None
            and planner_attempt is not None
            and analyst_validation is not None
            and planner_validation is not None
            and analyst_attempt.has_provider_receipt
            and not planner_attempt.has_provider_receipt
            and analyst_validation.verdict == "PASS"
            and planner_validation.verdict == "BLOCKED"
            and len(snapshot.receipts) == 1
            and snapshot.receipts[0].stage == "ANALYST"
        )
    )
    all_cases_pass = (
        len(snapshot.outcomes) == _REQUIRED_CASE_COUNT
        and all(outcome.status == "PASS" for outcome in snapshot.outcomes)
    )
    all_calls_valid = (
        len(snapshot.receipts) == _REQUIRED_CALL_COUNT
        and len(snapshot.validations) == _REQUIRED_CALL_COUNT
        and all(validation.verdict == "PASS" for validation in snapshot.validations)
    )
    if (
        snapshot.fixed_case_slot_count == _REQUIRED_CASE_COUNT
        and len(snapshot.claims) == _REQUIRED_CASE_COUNT
        and len(snapshot.attempts) == _REQUIRED_CALL_COUNT
        and pass_fact_chain_coherent
        and all_cases_pass
        and all_calls_valid
        # 直写 PostgreSQL 可以伪造结构正确的 PASS 行；只有账本公开 HMAC 读路径已经
        # 复验的十个 outcome 才能成为正式成功证据，不能用 SQL 行数替代认证事实。
        and snapshot.authenticated_pass_case_ids == passed_case_ids
        and _current_known_actual_spend(snapshot) <= snapshot.total_budget_cny
    ):
        return "PASS"
    if valid_empty_pre_send_run or valid_pre_send_blocked_chain:
        return "INCONCLUSIVE"
    if (
        snapshot.receipts
        or any(validation.verdict == "FAILED" for validation in snapshot.validations)
        or any(outcome.status == "FAILED" for outcome in snapshot.outcomes)
    ):
        # 已有 receipt 表示真实请求已经发出。根据 D-170，后续任意失败不能降格为
        # INCONCLUSIVE，更不能通过再次调用选择性地寻找一个可通过的样本。
        return "FAILED"
    # 剩余没有 receipt 的组合也不是合法发送前 BLOCKED 链，例如重复 stage、缺 validation
    # 或跨 case 引用。它们不能被解释成安全的未发送，只能 fail-closed 为 FAILED。
    return "FAILED"


def _current_known_actual_spend(snapshot: OfficialSmokeEvidenceSnapshot) -> Decimal:
    """汇总历史直接模式支出和已落盘 receipt 的实际成本，不把预约额度伪装成实际消费。"""

    return snapshot.historical_spend_cny + sum(
        (receipt.total_cost_cny for receipt in snapshot.receipts),
        start=Decimal("0"),
    )


def _scripted_baseline_comparison(snapshot: OfficialSmokeEvidenceSnapshot) -> str:
    """只按正式账本判断是否存在可配对的双阶段实验，不读取或复写 ScriptedModel 正文。

    正式 run 与 ScriptedModel 基线的比较要求同一 case 完成 Analyst/Planner 两段。首段已发送
    后失败时，账本没有合法 Planner receipt，强行补写脚本输出会把确定性演练冒充成真实模型
    结果；因此必须把比较显式标为不可配对。
    """

    dispatched_stages = {attempt.stage for attempt in snapshot.attempts}
    failed_analyst = any(
        validation.stage == "ANALYST" and validation.verdict == "FAILED"
        for validation in snapshot.validations
    )
    if failed_analyst and "PLANNER" not in dispatched_stages:
        return "NOT_COMPARABLE_AFTER_ANALYST_FAILURE"
    if (
        len(snapshot.receipts) == _REQUIRED_CALL_COUNT
        and len(snapshot.validations) == _REQUIRED_CALL_COUNT
    ):
        return "COMPLETE_FORMAL_PAIR_SET"
    return "NO_COMPLETE_FORMAL_PAIR_SET"


def _verify_pass_outcomes(
    *,
    settings: Any,
    receipt_authenticator: Phase16OfficialSmokeReceiptAuthenticator,
    outcomes: tuple[OfficialSmokeOutcome, ...],
) -> frozenset[str]:
    """通过正式账本公开 HMAC 消费接口复验每条 PASS outcome。

    报告器不能复制或旁路账本的私有签名逻辑。这里复用 ``verify_case_outcome_receipts``，
    因而完整十例的 PASS 必须具有同 claim 下的 Analyst/Planner 两条可信 receipt；连接设置
    已由调用方冻结为 PostgreSQL 只读，复验过程没有写入、恢复或重试能力。
    """

    verified: set[str] = set()
    ledger = PostgresPhase16OfficialSmokeLedger(
        settings,
        receipt_authenticator=receipt_authenticator,
    )
    for outcome in outcomes:
        if outcome.status != "PASS":
            continue
        verified_outcome = ledger.verify_case_outcome_receipts(case_id=outcome.case_id)
        if (
            verified_outcome.case_id != outcome.case_id
            or str(verified_outcome.status) != "PASS"
        ):
            raise ValueError("formal PASS outcome authenticity verification failed")
        verified.add(outcome.case_id)
    return frozenset(verified)


def _verify_receipt_authenticity(
    *,
    receipt_rows: Sequence[dict[str, Any]],
    receipt_authenticator: Phase16OfficialSmokeReceiptAuthenticator,
) -> None:
    """逐条复验所有已发送 receipt，避免把伪造的失败或成功事实写进 Addendum。

    正式账本的 PASS 消费仍由 ``_verify_pass_outcomes`` 调用公开 API 完成；此处额外覆盖
    FAILED/BLOCKED 报告也会展示的 receipt，确保一条恶意直写 SQL 不能伪造“模型已经失败”
    的历史。认证标签、attempt UUID 与原始 provider ID 都只在进程内参与 HMAC，不进入快照或
    Markdown。
    """

    for row in receipt_rows:
        authentic = receipt_authenticator.verify(
            receipt_auth_tag=_text(row, "receipt_auth_tag"),
            attempt_id=_uuid_text(row, "attempt_id"),
            stage=Phase16OfficialSmokeDispatchStage(_text(row, "stage")),
            profile_digest=_text(row, "profile_digest"),
            provider_response_id_digest=_text(row, "provider_response_id_digest"),
            finish_reason=_text(row, "finish_reason"),
            model_id=_text(row, "model_id"),
            response_digest=_text(row, "response_digest"),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            total_tokens=int(row["total_tokens"]),
            latency_ms=_decimal(row["latency_ms"]),
            input_cost_cny=_decimal(row["input_cost_cny"]),
            output_cost_cny=_decimal(row["output_cost_cny"]),
            total_cost_cny=_decimal(row["total_cost_cny"]),
        )
        if not authentic:
            raise ValueError("formal receipt authenticity verification failed")


def _read_allowlisted_dotenv(
    path: Path,
    *,
    allowed_names: frozenset[str],
) -> dict[str, str]:
    """从本地 `.env` 逐行提取严格白名单，忽略模型字段且不写入进程环境。

    与通用 ``Settings``/``load_dotenv`` 不同，本解析器不把整份 `.env` 展开为配置对象或
    ``os.environ``。它只识别报告器实际需要的 PostgreSQL 和 receipt HMAC 名称，避免 API Key
    因一次离线文档渲染进入应用配置或子进程环境。
    """

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for name in allowed_names:
            prefix = f"{name}="
            if line.startswith(prefix):
                # `.env` 的报告专用键只允许简单的字面值或成对引号；注释和多余空白不能
                # 进入数据库连接参数，防止配置行被误解释为可执行文本。
                value = line[len(prefix) :].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                else:
                    value = value.split(" #", maxsplit=1)[0].rstrip()
                values[name] = value
                break
    return values


def _load_read_only_report_settings(
    *,
    dotenv_path: Path | None = None,
) -> tuple[_ReadOnlyReportSettings, Phase16OfficialSmokeReceiptAuthenticator | None]:
    """仅装配报告所需的数据库与 HMAC 白名单；绝不调用应用级 ``get_settings``。

    进程环境优先于本地 `.env`，方便 CI/部署将只读数据库凭据独立注入。HMAC 缺失时仍可读取
    无 receipt 的 ``INCONCLUSIVE`` 账本，但任何已发送的 receipt 都会在后续读路径 fail-closed。
    """

    allowed_names = frozenset(
        {
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_DB",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "PHASE16_OFFICIAL_SMOKE_RECEIPT_HMAC_HEX",
        }
    )
    dotenv_values = _read_allowlisted_dotenv(
        dotenv_path or _PROJECT_ROOT / ".env",
        allowed_names=allowed_names,
    )
    values = {
        name: os.environ.get(name, dotenv_values.get(name, "")).strip()
        for name in allowed_names
    }
    try:
        port = int(values["POSTGRES_PORT"] or "5432")
    except ValueError as error:
        raise ValueError("formal smoke report PostgreSQL port is invalid") from error
    if not 1 <= port <= 65535:
        raise ValueError("formal smoke report PostgreSQL port is invalid")
    settings = _ReadOnlyReportSettings(
        {
            "host": values["POSTGRES_HOST"] or "localhost",
            "port": port,
            "dbname": values["POSTGRES_DB"] or "postgres",
            "user": values["POSTGRES_USER"] or "postgres",
            "password": values["POSTGRES_PASSWORD"] or "change_me",
            # libpq 在事务创建前即固定只读默认值；下方 SQL 还会显式 SET TRANSACTION，
            # 防止未来代码新增写语句时仅依赖调用习惯而意外提交账本变更。
            "options": "-c default_transaction_read_only=on",
        }
    )
    raw_hmac = values["PHASE16_OFFICIAL_SMOKE_RECEIPT_HMAC_HEX"]
    if not raw_hmac:
        return settings, None
    try:
        signing_key = bytes.fromhex(raw_hmac)
    except ValueError as error:
        raise ValueError("formal smoke report receipt HMAC configuration is invalid") from error
    return settings, Phase16OfficialSmokeReceiptAuthenticator(signing_key)


def read_official_smoke_evidence(
    settings: Any,
    *,
    receipt_authenticator: Phase16OfficialSmokeReceiptAuthenticator | None,
) -> OfficialSmokeEvidenceSnapshot:
    """以固定 SQL 白名单和账本公开认证入口重建正式证据投影。

    查询显式运行在 PostgreSQL 只读事务中，不选择 attempt 内部 request ID、Prompt、模型输出、
    API Key、思维链或经营建议字段。若已经有 Provider receipt 却缺失 HMAC 认证器，报告宁可
    阻断，也不会把未经外部签名复验的 SQL 行写成正式真实模型证据。
    """

    read_only_settings = _ReadOnlyReportSettings(
        {
            **dict(settings.postgres_connection_kwargs),
            "options": "-c default_transaction_read_only=on",
        }
    )
    with psycopg.connect(
        **read_only_settings.postgres_connection_kwargs,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            # libpq 默认只读与显式事务只读双重绑定；后续新增错误 SQL 即使被提交也会被
            # PostgreSQL 拒绝，报告器不会成为 append-only 账本的写旁路。
            cursor.execute("SET TRANSACTION READ ONLY;")
            # 在读取历史事实之前先确认 append-only 触发器、外键和约束仍完整；schema 漂移
            # 时宁可阻止生成报告，也不把不可信数据库内容写成正式证据。
            cursor.execute("SELECT phase16_official_smoke_assert_schema_contract();")
            cursor.execute(
                """SELECT run_id, manifest_digest, total_budget_cny
                   FROM phase16_official_smoke_runs
                   WHERE run_id=%s;""",
                (_FORMAL_RUN_ID,),
            )
            run_row = cursor.fetchone()
            if run_row is None:
                raise ValueError("formal smoke run has not been initialized")
            cursor.execute(
                """SELECT amount_cny
                   FROM phase16_official_smoke_historical_spend
                   WHERE run_id=%s AND source=%s;""",
                (_FORMAL_RUN_ID, _HISTORICAL_DIRECT_MODE_SOURCE),
            )
            historical_row = cursor.fetchone()
            if historical_row is None:
                raise ValueError("formal historical spend is unavailable")
            cursor.execute(
                """SELECT count(*) AS slot_count,
                          COALESCE(sum(analyst_reservation_cny + planner_reservation_cny), 0)
                              AS total_reservation_cny
                   FROM phase16_official_smoke_case_slots
                   WHERE run_id=%s;""",
                (_FORMAL_RUN_ID,),
            )
            slot_row = cursor.fetchone()
            if slot_row is None:
                raise ValueError("formal smoke slots are unavailable")
            cursor.execute(
                """SELECT case_id
                   FROM phase16_official_smoke_case_claims
                   WHERE run_id=%s
                   ORDER BY case_id;""",
                (_FORMAL_RUN_ID,),
            )
            claim_rows = cursor.fetchall()
            cursor.execute(
                """SELECT attempt.case_id, attempt.stage, attempt.profile_digest,
                          receipt.attempt_id IS NOT NULL AS has_provider_receipt,
                          validation.attempt_id IS NOT NULL AS has_validation_fact
                   FROM phase16_official_smoke_dispatch_attempts AS attempt
                   LEFT JOIN phase16_official_smoke_provider_receipts AS receipt
                     ON receipt.attempt_id=attempt.attempt_id
                   LEFT JOIN phase16_official_smoke_validation_facts AS validation
                     ON validation.attempt_id=attempt.attempt_id
                   WHERE attempt.run_id=%s
                   ORDER BY attempt.case_id, attempt.stage;""",
                (_FORMAL_RUN_ID,),
            )
            attempt_rows = cursor.fetchall()
            cursor.execute(
                """SELECT attempt.attempt_id, attempt.case_id, attempt.stage, attempt.profile_digest,
                          receipt.provider_response_id_digest, receipt.finish_reason,
                          receipt.model_id, receipt.response_digest, receipt.input_tokens,
                          receipt.output_tokens, receipt.total_tokens, receipt.latency_ms,
                          receipt.input_cost_cny, receipt.output_cost_cny, receipt.total_cost_cny,
                          receipt.receipt_auth_tag
                   FROM phase16_official_smoke_dispatch_attempts AS attempt
                   JOIN phase16_official_smoke_provider_receipts AS receipt
                     ON receipt.attempt_id=attempt.attempt_id
                   WHERE attempt.run_id=%s
                   ORDER BY attempt.case_id, attempt.stage;""",
                (_FORMAL_RUN_ID,),
            )
            receipt_rows = cursor.fetchall()
            cursor.execute(
                """SELECT attempt.case_id, attempt.stage, validation.verdict,
                          validation.reason_code, validation.validation_digest
                   FROM phase16_official_smoke_dispatch_attempts AS attempt
                   JOIN phase16_official_smoke_validation_facts AS validation
                     ON validation.attempt_id=attempt.attempt_id
                   WHERE attempt.run_id=%s
                   ORDER BY attempt.case_id, attempt.stage;""",
                (_FORMAL_RUN_ID,),
            )
            validation_rows = cursor.fetchall()
            cursor.execute(
                """SELECT case_id, status, reason_code, outcome_digest
                   FROM phase16_official_smoke_case_outcomes
                   WHERE run_id=%s
                   ORDER BY case_id;""",
                (_FORMAL_RUN_ID,),
            )
            outcome_rows = cursor.fetchall()

    historical_spend = _decimal(historical_row["amount_cny"])
    total_reservation = _decimal(slot_row["total_reservation_cny"])
    if receipt_rows and receipt_authenticator is None:
        raise ValueError("formal receipt authentication configuration is unavailable")
    if receipt_rows:
        # 任何将被报告展示的 Provider receipt 都先经过进程外 HMAC 认证；即使终态已经
        # 是 FAILED，也不能让未签名的数据库直写行改变“真实调用发生过”的审计叙述。
        _verify_receipt_authenticity(
            receipt_rows=receipt_rows,
            receipt_authenticator=receipt_authenticator,
        )
    claims = tuple(
        OfficialSmokeCaseClaim(case_id=_text(row, "case_id"))
        for row in claim_rows
    )
    attempts = tuple(
        OfficialSmokeDispatchAttempt(
            case_id=_text(row, "case_id"),
            stage=_text(row, "stage"),
            profile_digest=_text(row, "profile_digest"),
            has_provider_receipt=bool(row["has_provider_receipt"]),
            has_validation_fact=bool(row["has_validation_fact"]),
        )
        for row in attempt_rows
    )
    receipts = tuple(
        OfficialSmokeReceipt(
            case_id=_text(row, "case_id"),
            stage=_text(row, "stage"),
            profile_digest=_text(row, "profile_digest"),
            provider_response_id_digest=_text(row, "provider_response_id_digest"),
            finish_reason=_text(row, "finish_reason"),
            model_id=_text(row, "model_id"),
            response_digest=_text(row, "response_digest"),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            total_tokens=int(row["total_tokens"]),
            latency_ms=_decimal(row["latency_ms"]),
            input_cost_cny=_decimal(row["input_cost_cny"]),
            output_cost_cny=_decimal(row["output_cost_cny"]),
            total_cost_cny=_decimal(row["total_cost_cny"]),
        )
        for row in receipt_rows
    )
    validations = tuple(
        OfficialSmokeValidation(
            case_id=_text(row, "case_id"),
            stage=_text(row, "stage"),
            verdict=_text(row, "verdict"),
            reason_code=_text(row, "reason_code"),
            validation_digest=_text(row, "validation_digest"),
        )
        for row in validation_rows
    )
    outcomes = tuple(
        OfficialSmokeOutcome(
            case_id=_text(row, "case_id"),
            status=_text(row, "status"),
            reason_code=_text(row, "reason_code"),
            outcome_digest=_text(row, "outcome_digest"),
        )
        for row in outcome_rows
    )
    # 对每个 PASS outcome 调用正式账本公开读 API。该 API 内部重建 HMAC payload 并验证
    # 两段 receipt；它是数据库直写伪造无法跨越的成功证据边界，不能由报告器自行替代。
    authenticated_pass_case_ids = (
        _verify_pass_outcomes(
            settings=read_only_settings,
            receipt_authenticator=receipt_authenticator,
            outcomes=outcomes,
        )
        if any(outcome.status == "PASS" for outcome in outcomes)
        else frozenset()
    )
    return OfficialSmokeEvidenceSnapshot(
        run_id=_text(run_row, "run_id"),
        manifest_digest=_text(run_row, "manifest_digest"),
        total_budget_cny=_decimal(run_row["total_budget_cny"]),
        historical_spend_cny=historical_spend,
        fixed_case_slot_count=int(slot_row["slot_count"]),
        maximum_exposure_cny=historical_spend + total_reservation,
        receipts=receipts,
        validations=validations,
        outcomes=outcomes,
        claims=claims,
        attempts=attempts,
        authenticated_pass_case_ids=authenticated_pass_case_ids,
    )


def _render_official_smoke_evidence_markdown(snapshot: OfficialSmokeEvidenceSnapshot) -> str:
    """将已认证读取路径交付的脱敏投影转换为 Markdown。

    这是无副作用的内部格式化函数，供正式读取路径和离线单测复用；它不是正式证据入口。
    D-121 规定任意同进程代码执行已等同服务失陷，因此不能把 Python 私有命名误当作插件
    沙箱。对外正式 API 必须使用下方 ``render_official_smoke_evidence_report``，其输入只能
    是数据库设置与 HMAC 认证器，永不接收调用方自造的 snapshot。
    """

    conclusion = _formal_conclusion(snapshot)
    actual_spend = _current_known_actual_spend(snapshot)
    analyst_attempt_count = sum(attempt.stage == "ANALYST" for attempt in snapshot.attempts)
    planner_attempt_count = sum(attempt.stage == "PLANNER" for attempt in snapshot.attempts)
    unclaimed_slot_count = max(snapshot.fixed_case_slot_count - len(snapshot.claims), 0)
    passed_outcome_count = sum(outcome.status == "PASS" for outcome in snapshot.outcomes)
    if not passed_outcome_count:
        pass_authentication = "NOT_APPLICABLE"
    elif len(snapshot.authenticated_pass_case_ids) == passed_outcome_count:
        pass_authentication = "VERIFIED"
    else:
        pass_authentication = "FAILED"
    failed_analyst_reasons = tuple(
        validation.reason_code
        for validation in snapshot.validations
        if validation.stage == "ANALYST" and validation.verdict == "FAILED"
    )
    lines = [
        "# Phase 16 Official Real-Model Smoke Evidence",
        "",
        "本 Addendum 仅从 PostgreSQL append-only formal ledger 的最小脱敏字段生成。它不保存或展示 API Key、Prompt、模型正文、思维链、原始 provider ID 或经营建议。",
        "",
        f"- Formal run: `{snapshot.run_id}`",
        f"- Formal manifest digest: `{snapshot.manifest_digest}`",
        f"- Formal evidence conclusion: `{conclusion}`",
        "- Production default route: `DETERMINISTIC_ONLY`",
        "- Phase state: `AWAITING_PHASE_17_GATE`",
        "",
        "## Strict Result",
        "",
        f"- Required cases / calls: `{_REQUIRED_CASE_COUNT} / {_REQUIRED_CALL_COUNT}`",
        f"- Completed cases / calls: `{len(snapshot.outcomes)} / {len(snapshot.receipts)}`",
        f"- Validation facts: `{len(snapshot.validations)}`",
        f"- Claimed / unclaimed fixed slots: `{len(snapshot.claims)} / {unclaimed_slot_count}`",
        f"- Dispatch attempts Analyst / Planner: `{analyst_attempt_count} / {planner_attempt_count}`",
        f"- Authenticated PASS outcomes: `{len(snapshot.authenticated_pass_case_ids)} / {passed_outcome_count}` (`{pass_authentication}`)",
        f"- ScriptedModel baseline comparison: `{_scripted_baseline_comparison(snapshot)}`",
        "- Retry policy: `ZERO_RETRY_AFTER_SEND`",
        "- Text repair or scripted substitution: `FORBIDDEN`",
        "",
        "## Budget",
        "",
        f"- Formal cap: `{snapshot.total_budget_cny:.6f} CNY`",
        f"- Historical direct-mode spend: `{snapshot.historical_spend_cny:.6f} CNY`",
        f"- Current known actual spend: `{actual_spend:.6f} CNY`",
        f"- Frozen fixed slots: `{snapshot.fixed_case_slot_count}`",
        f"- Frozen maximum exposure: `{snapshot.maximum_exposure_cny:.6f} CNY`",
        "",
        "## Receipt And Validation Facts",
        "",
    ]
    for receipt in snapshot.receipts:
        lines.extend(
            (
                f"### `{receipt.case_id}` / `{receipt.stage}`",
                "",
                f"- Profile digest: `{receipt.profile_digest}`",
                f"- Provider receipt digest: `{receipt.provider_response_id_digest}`",
                f"- Response digest: `{receipt.response_digest}`",
                f"- Model / finish reason: `{receipt.model_id}` / `{receipt.finish_reason}`",
                f"- Usage input / output / total: `{receipt.input_tokens} / {receipt.output_tokens} / {receipt.total_tokens}`",
                f"- Latency: `{receipt.latency_ms:.3f} ms`",
                f"- Cost input / output / total: `{receipt.input_cost_cny:.6f} / {receipt.output_cost_cny:.6f} / {receipt.total_cost_cny:.6f} CNY`",
                "",
            )
        )
    for validation in snapshot.validations:
        lines.extend(
            (
                f"- Validation `{validation.case_id}` / `{validation.stage}`: `{validation.verdict}` / `{validation.reason_code}` / `{validation.validation_digest}`",
            )
        )
    for outcome in snapshot.outcomes:
        lines.extend(
            (
                f"- Outcome `{outcome.case_id}`: `{outcome.status}` / `{outcome.reason_code}` / `{outcome.outcome_digest}`",
            )
        )
    lines.extend(("", "## Interpretation", ""))
    if failed_analyst_reasons and planner_attempt_count == 0:
        # 下面的数量全部来自 claim/attempt/validation 投影，不能把当前一次失败的偶然形态
        # 写死为未来每次 formal run 的结论。原因码只说明验证失败，不推断模型正文的根因。
        lines.extend(
            (
                "已记录的 Analyst validation 未通过正式校验，稳定原因码为 "
                + ", ".join(f"`{reason}`" for reason in sorted(set(failed_analyst_reasons)))
                + f"。账本记录 `{planner_attempt_count}` 个 Planner dispatch 与 `{unclaimed_slot_count}` 个未 claim 的固定 slot；"
                "D-170 要求在已发送后立即停止，不能重试、修补模型文本或以 ScriptedModel 代替。",
                "",
            )
        )
    elif conclusion == "PASS":
        lines.extend(
            (
                "十个固定 case 的双阶段 receipt、validation 和 outcome 均已由正式账本 HMAC 消费路径复验。"
                "该外部集成证据不改变生产默认路由或自动经营权限。",
                "",
            )
        )
    else:
        lines.extend(
            (
                "账本投影未形成可认证的 10/10 双阶段 PASS；报告不从缺失、部分或相互矛盾的"
                "调度事实推断未发送原因，也不会触发重试。",
                "",
            )
        )
    lines.extend(
        (
            "正式账本故意只保留稳定验证原因码与摘要，不保存模型正文或内部异常文本；因此本报告不把 token 数、Schema、AgentAction 或 EvidenceRef 中的任一可能原因推断为确定根因。该限制保护敏感载荷，也避免在没有原始证据时制造错误归因。",
            "",
            f"本 Addendum 取代此前对 Phase 16 真实模型证据“未执行/INCONCLUSIVE”的当前表述：本次正式 run 的外部结论为 `{conclusion}`。这不改变已通过的确定性工程验收，也不会开启 `DECISION_SUPPORT` 或自动经营动作。",
            "",
        )
    )
    return "\n".join(lines)


def render_official_smoke_evidence_report(
    *,
    settings: Any,
    receipt_authenticator: Phase16OfficialSmokeReceiptAuthenticator | None,
) -> OfficialSmokeEvidenceReport:
    """通过唯一认证读取路径生成正式报告，拒绝调用方注入任意 receipt 或 snapshot。

    ``read_official_smoke_evidence`` 在 PostgreSQL 只读事务中执行 schema、receipt HMAC 与 PASS
    outcome 复验；只有它返回的投影才会交给内部 Markdown 格式化函数。公开入口不接受 raw
    snapshot，从 API 形状上杜绝“手工数据直接写成正式 Addendum”的旁路。
    """

    snapshot = read_official_smoke_evidence(
        settings,
        receipt_authenticator=receipt_authenticator,
    )
    return OfficialSmokeEvidenceReport(
        run_id=snapshot.run_id,
        status=_formal_conclusion(snapshot),
        markdown=_render_official_smoke_evidence_markdown(snapshot),
    )


def _write_official_smoke_evidence_markdown(root: Path, markdown: str) -> Path:
    """将已经由正式读取路径生成的 Markdown 写入固定 Addendum，并强制 UTF-8/LF。"""

    output_root = Path(root)
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / "phase-16-official-smoke-evidence.md"
    output.write_text(markdown, encoding="utf-8", newline="\n")
    return output


def write_official_smoke_evidence_report(
    root: Path,
    *,
    settings: Any,
    receipt_authenticator: Phase16OfficialSmokeReceiptAuthenticator | None,
) -> tuple[Path, OfficialSmokeEvidenceReport]:
    """读取、认证、渲染并写出正式 Addendum；没有接受 raw snapshot 的公共写入旁路。"""

    report = render_official_smoke_evidence_report(
        settings=settings,
        receipt_authenticator=receipt_authenticator,
    )
    return _write_official_smoke_evidence_markdown(root, report.markdown), report


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """只暴露输出路径参数；没有 execute、重试或模型连接开关，避免报告器成为旁路。"""

    parser = argparse.ArgumentParser(
        description="Render the sanitized Phase 16 formal smoke evidence from PostgreSQL."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_PROJECT_ROOT / "docs" / "superpowers" / "reports",
        help="directory that receives phase-16-official-smoke-evidence.md",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """读取账本并写报告；数据库读取失败只返回稳定阻断摘要，不泄漏连接细节。"""

    arguments = _parse_arguments(argv)
    try:
        settings, receipt_authenticator = _load_read_only_report_settings()
        output, report = write_official_smoke_evidence_report(
            arguments.output,
            settings=settings,
            receipt_authenticator=receipt_authenticator,
        )
    except Exception:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason_code": "FORMAL_SMOKE_EVIDENCE_READ_FAILED",
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": report.status,
                "output": str(output),
                "run_id": report.run_id,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
