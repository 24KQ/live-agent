"""Phase 17 holdout 原始响应 capture 与 artifact 摘要对账。

本模块只服务 Phase 17 独立执行路径，不修改 Phase 16 冻结 adapter。它把
``AsyncHttpResponse.body`` 在 transport 返回后立即写入本地未跟踪目录，并将
文件摘要返回给上层 attempt 事实。capture 失败时不允许 adapter 继续重试或换端，
避免出现“网络事实已经发生、审计正文却没有落盘”的不可解释状态。

artifact 目录不是版本库交付物，真实执行前由 CLI 确保 ``_probe_artifacts/`` 已
写入 ``.gitignore``。文件名只由受限的 run/case/stage/index 组成，拒绝路径穿越。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Iterator

from src.specialist_runtime.deepseek_adapter import (
    AsyncHttpResponse,
    AsyncHttpTransport,
)


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class Phase17ArtifactCaptureError(RuntimeError):
    """原始响应无法安全落盘或摘要无法对账。"""


@dataclass(frozen=True)
class Phase17CaptureContext:
    """一次 Analyst/Planner stage 的 artifact 路径身份。"""

    run_id: str
    case_id: str
    stage: str


@dataclass
class Phase17CaptureAttempt:
    """当前网络 attempt 的 capture 状态，生命周期只存在于一个 task 内。"""

    context: Phase17CaptureContext
    attempt_index: int
    captured: bool = False
    artifact_path: str | None = None
    artifact_digest: str | None = None
    error: str | None = None


def _validate_component(value: str, *, field_name: str) -> str:
    """限制路径组件为单层安全标识，拒绝 ``..``、斜杠和空字符串。"""

    if not value or _SAFE_COMPONENT.fullmatch(value) is None:
        raise Phase17ArtifactCaptureError(
            f"phase17 artifact {field_name} contains an unsafe path component"
        )
    return value


class Phase17ArtifactCapture:
    """把 transport 响应正文写入 artifact，并提供 task-local attempt 状态。"""

    def __init__(self, *, repository_root: Path) -> None:
        self._artifact_root = repository_root / "_probe_artifacts"
        self._stage_context: ContextVar[Phase17CaptureContext | None] = ContextVar(
            "phase17_capture_stage_context",
            default=None,
        )
        self._attempt_context: ContextVar[Phase17CaptureAttempt | None] = ContextVar(
            "phase17_capture_attempt_context",
            default=None,
        )

    @property
    def artifact_root(self) -> Path:
        """返回 capture 根目录，供 CLI/测试确认其为未跟踪目录。"""

        return self._artifact_root

    def _artifact_target(self, relative_path: Path) -> Path:
        """解析 artifact 目标并拒绝越界或符号链接。

        仅检查 ``..`` 不足以证明读取对象仍在 `_probe_artifacts/` 内；
        聚合阶段还必须防止有人把账本中的相对路径替换成指向仓库外部的
        符号链接。因此写入和复验统一经过同一个根目录约束。
        """

        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise Phase17ArtifactCaptureError("phase17 artifact path is unsafe")
        target = self._artifact_root / relative_path
        root = self._artifact_root.resolve()
        resolved = target.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise Phase17ArtifactCaptureError("phase17 artifact path escapes artifact root")
        if target.is_symlink():
            raise Phase17ArtifactCaptureError("phase17 artifact path cannot be a symlink")
        return target

    @contextmanager
    def bind_stage(
        self,
        *,
        run_id: str,
        case_id: str,
        stage: str,
    ) -> Iterator[None]:
        """绑定一个 stage；同一 event loop 中并发 task 不会互相覆盖身份。"""

        context = Phase17CaptureContext(
            run_id=_validate_component(run_id, field_name="run_id"),
            case_id=_validate_component(case_id, field_name="case_id"),
            stage=_validate_component(stage, field_name="stage"),
        )
        token = self._stage_context.set(context)
        try:
            yield
        finally:
            self._stage_context.reset(token)

    @contextmanager
    def begin_attempt(self, *, attempt_index: int) -> Iterator[Phase17CaptureAttempt]:
        """开始一个 attempt；退出前由 adapter 检查是否确实捕获到了正文。"""

        if attempt_index < 1:
            raise Phase17ArtifactCaptureError("phase17 artifact attempt index must be positive")
        context = self._stage_context.get()
        if context is None:
            raise Phase17ArtifactCaptureError(
                "phase17 artifact capture stage context is not bound"
            )
        attempt = Phase17CaptureAttempt(context=context, attempt_index=attempt_index)
        token = self._attempt_context.set(attempt)
        try:
            yield attempt
        finally:
            self._attempt_context.reset(token)

    def capture_response(self, body: bytes) -> Phase17CaptureAttempt:
        """写入原始 body，并回读文件确认 SHA-256 与内存正文一致。"""

        attempt = self._attempt_context.get()
        if attempt is None:
            raise Phase17ArtifactCaptureError(
                "phase17 artifact capture attempt context is not bound"
            )
        if attempt.captured:
            raise Phase17ArtifactCaptureError(
                "phase17 artifact capture received more than one response for an attempt"
            )
        try:
            relative_path = Path(
                attempt.context.run_id,
                attempt.context.case_id,
                attempt.context.stage,
                f"attempt-{attempt.attempt_index}.body",
            )
            target = self._artifact_target(relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            # 使用独占创建，重复 run/case/stage/index 不得覆盖既有正文。
            with target.open("xb") as stream:
                stream.write(body)
                stream.flush()
            written = target.read_bytes()
            expected_digest = sha256(body).hexdigest()
            actual_digest = sha256(written).hexdigest()
            if actual_digest != expected_digest:
                raise Phase17ArtifactCaptureError(
                    "phase17 artifact digest does not match response body"
                )
            attempt.captured = True
            attempt.artifact_path = relative_path.as_posix()
            attempt.artifact_digest = actual_digest
            return attempt
        except Phase17ArtifactCaptureError as exc:
            attempt.error = str(exc)
            raise
        except OSError as exc:
            attempt.error = f"phase17 artifact write failed: {exc}"
            raise Phase17ArtifactCaptureError(attempt.error) from exc

    def require_captured(self, attempt: Phase17CaptureAttempt) -> None:
        """在 adapter 允许进入下一次重试前强制确认正文已落盘。"""

        if not attempt.captured:
            attempt.error = attempt.error or (
                "phase17 artifact response body was unavailable for this network attempt"
            )
            raise Phase17ArtifactCaptureError(attempt.error)

    def artifact_digest(self, relative_path: str) -> str:
        """重新读取指定 artifact，供聚合前逐 attempt 对账。"""

        relative = Path(relative_path)
        target = self._artifact_target(relative)
        try:
            return sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            raise Phase17ArtifactCaptureError(
                f"phase17 artifact is missing or unreadable: {relative_path}"
            ) from exc


class Phase17CaptureTransport:
    """不改变 HTTP 语义的 transport 装饰器，只截取响应正文。"""

    def __init__(
        self,
        delegate: AsyncHttpTransport,
        *,
        capture: Phase17ArtifactCapture,
    ) -> None:
        self._delegate = delegate
        self._capture = capture

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> AsyncHttpResponse:
        """收到 HTTP response 后先 capture，再把原 response 原样交回 adapter。"""

        response = await self._delegate.post_json(
            url=url,
            headers=headers,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        self._capture.capture_response(response.body)
        return response
