"""Phase 13 正式 Evaluation Manifest 的 Git 与源码可信预检。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from src.specialist_evaluation.models import (
    EvaluationManifest,
    EvaluationManifestKind,
    FormalManifestAuthorization,
    _build_formal_manifest_authorization,
)


def _normalized_source_bytes(path: Path) -> bytes:
    """按严格 UTF-8 读取源码，并只规范换行后计算跨平台稳定摘要。"""

    text = path.read_text(encoding="utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def calculate_source_code_digest(project_root: Path) -> str:
    """重算全部产品源码与评估代码的保守闭包摘要。"""

    root = Path(project_root).resolve()
    paths = tuple(
        sorted(
            path
            for source_root in (root / "src", root / "evaluation")
            if source_root.exists()
            for path in source_root.rglob("*.py")
        )
    )
    if not paths:
        raise ValueError("formal manifest source closure is empty")
    digests = {
        path.relative_to(root).as_posix(): hashlib.sha256(
            _normalized_source_bytes(path)
        ).hexdigest()
        for path in paths
    }
    encoded = (
        json.dumps(digests, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_git_tracked_source_closure(root: Path) -> None:
    """拒绝 symlink、ignored 或 untracked Python 进入正式运行源码闭包。"""

    source_roots = (root / "src", root / "evaluation")
    for source_root in source_roots:
        if source_root.is_symlink() or any(path.is_symlink() for path in source_root.rglob("*")):
            raise ValueError("formal manifest tracked source closure cannot contain symlinks")
    discovered_paths = {
        path.relative_to(root).as_posix()
        for source_root in source_roots
        if source_root.exists()
        for path in source_root.rglob("*.py")
    }
    tracked_output = subprocess.run(
        ["git", "ls-files", "--cached", "--", "src", "evaluation"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    tracked_paths = {
        line.strip().replace("\\", "/")
        for line in tracked_output.splitlines()
        if line.strip().endswith(".py")
    }
    if discovered_paths != tracked_paths:
        raise ValueError("formal manifest tracked source closure does not match disk")


def verify_formal_manifest_at_git_head(
    manifest: EvaluationManifest,
    project_root: Path,
) -> FormalManifestAuthorization:
    """仅在最终 Git HEAD、清洁源码和 code digest 全部一致时签发注册授权。"""

    if manifest.manifest_kind is not EvaluationManifestKind.FORMAL_EVALUATION:
        raise ValueError("only formal evaluation manifests can pass Git preflight")
    root = Path(project_root).resolve()
    _assert_git_tracked_source_closure(root)
    # 正式身份不能把未提交源码伪装成 HEAD；只检查参与 code_digest 的两个目录，
    # 文档或本地报告变更不会无关阻断模型评估。
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "src", "evaluation"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise ValueError("formal manifest requires a clean source closure")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if manifest.source_commit != head:
        raise ValueError("formal manifest source_commit does not match Git HEAD")
    if manifest.code_digest != calculate_source_code_digest(root):
        raise ValueError("formal manifest code_digest does not match source closure")
    return _build_formal_manifest_authorization(manifest)
