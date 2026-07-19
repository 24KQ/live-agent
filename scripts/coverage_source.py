"""Phase 16 coverage source-closure 的稳定读取入口。

该模块只负责读取已经提交的 Manifest，并把经过校验的源码路径提供给
coverage。它不自动发现目录、不接受任意调用方追加路径，避免 CI 因为导入
顺序变化而悄悄改变覆盖率分母。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any


class CoverageSourceClosureError(ValueError):
    """源码闭包缺失、篡改或不满足 Git 边界时抛出的稳定错误。"""


def _canonical_source_digest(root: Path, source_paths: tuple[str, ...]) -> str:
    """按 Phase 16 Manifest 的既有算法计算跨平台稳定源码摘要。"""

    payload = bytearray()
    for relative_path in source_paths:
        path = root / Path(relative_path)
        # 源码摘要统一使用 UTF-8 LF，避免 Windows checkout 的换行投影改变身份。
        normalized = (
            path.read_text(encoding="utf-8-sig")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .encode("utf-8")
        )
        payload.extend(relative_path.encode("utf-8") + b"\0" + normalized + b"\0")
    return hashlib.sha256(bytes(payload)).hexdigest()


def _read_payload(manifest_path: Path) -> dict[str, Any]:
    """严格读取 JSON Manifest，拒绝 BOM、非对象根节点和非法 UTF-8。"""

    raw = manifest_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise CoverageSourceClosureError("coverage source Manifest must not contain BOM")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoverageSourceClosureError("coverage source Manifest is invalid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise CoverageSourceClosureError("coverage source Manifest root must be an object")
    return payload


def _assert_tracked(root: Path, relative_path: str) -> None:
    """要求每个 coverage 文件都是当前 Git 仓库已跟踪的普通文件。"""

    path = root / Path(relative_path)
    if path.is_symlink() or not path.is_file():
        raise CoverageSourceClosureError(f"coverage source path is not a regular file: {relative_path}")
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative_path],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip().replace("\\", "/") != relative_path:
        raise CoverageSourceClosureError(f"coverage source path is not Git-tracked: {relative_path}")


def load_source_closure(
    manifest_path: Path,
    *,
    project_root: Path | None = None,
) -> tuple[str, ...]:
    """加载并验证源码闭包，返回排序稳定的 POSIX 相对路径。"""

    manifest = Path(manifest_path).resolve()
    # 当前 Manifest 位于 evaluation/manifests，默认项目根目录是其上两级。
    root = (project_root or manifest.parents[2]).resolve()
    payload = _read_payload(manifest)
    source_paths_value = payload.get("source_paths")
    if not isinstance(source_paths_value, list) or not source_paths_value:
        raise CoverageSourceClosureError("coverage source Manifest requires source_paths")
    if any(not isinstance(item, str) for item in source_paths_value):
        raise CoverageSourceClosureError("coverage source paths must be strings")
    source_paths = tuple(source_paths_value)
    normalized_paths = tuple(path.replace("\\", "/") for path in source_paths)
    if normalized_paths != source_paths:
        raise CoverageSourceClosureError("coverage source paths must use POSIX separators")
    if len(set(normalized_paths)) != len(normalized_paths):
        raise CoverageSourceClosureError("coverage source paths must be unique")
    for relative_path in normalized_paths:
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or not relative_path.startswith("src/"):
            raise CoverageSourceClosureError(f"coverage source path escapes src/: {relative_path}")
        _assert_tracked(root, relative_path)
    expected_digest = payload.get("source_digest")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise CoverageSourceClosureError("coverage source Manifest requires source_digest")
    actual_digest = _canonical_source_digest(root, normalized_paths)
    if actual_digest != expected_digest:
        raise CoverageSourceClosureError("coverage source Manifest source_digest does not match disk")
    return normalized_paths


def _parser() -> argparse.ArgumentParser:
    """构造输出 coverage source/include 参数的 CLI。"""

    parser = argparse.ArgumentParser(description="Print a verified coverage source closure")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--format", choices=("include", "source-root"), default="include")
    return parser


def main(argv: list[str] | None = None) -> int:
    """输出逗号分隔的 include 路径，任何闭包错误均以稳定非零码退出。"""

    args = _parser().parse_args(argv)
    try:
        paths = load_source_closure(args.manifest)
        # coverage 的 --source 只接受包或目录；精确文件闭包必须交给 json
        # 阶段的 --include，避免把文件路径误解析成不存在的 Python 模块。
        print(",".join(paths if args.format == "include" else ("src",)))
    except (OSError, CoverageSourceClosureError) as exc:
        print(f"COVERAGE_SOURCE_CLOSURE_INVALID: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
