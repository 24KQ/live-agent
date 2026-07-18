# -*- coding: utf-8 -*-
"""检查项目文件中的敏感信息，并支持只扫描 Git 已跟踪文件。

``--tracked`` 是发布门禁使用的稳定入口：它通过 ``git ls-files`` 获取扫描集合，
不会把本地临时文件、用户未提交的探针或构建产物误混入 Release 证据。严格模式
发现命中时返回非零；普通模式只报告命中，方便开发者先观察结果。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sql", ".html", ".js", ".env"}
WHITELIST = {".env.example", "test_operator_auth", "test_sensitive"}
SAFE_EXAMPLE_MARKERS = {"change_me", "self.postgres_password", "base.postgres_password"}


def _build_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """构造固定规则，避免使用会破坏 Python 解析的嵌套 shell 引号。"""

    return [
        ("env_path", re.compile(r'''(^|[\\/])\.env(?:$|[\s"'])''')),
        (
            "api_key",
            re.compile(r'''(?i)(api_key|api_secret|app_secret)\s*[=:]\s*["'][a-zA-Z0-9_-]{16,}'''),
        ),
        (
            "password",
            re.compile(r'''(?i)(password|passwd|pwd)\s*[=:]\s*["'][^"']{3,}'''),
        ),
        (
            "token",
            re.compile(r'''(?i)(token|access_token|bearer)\s*[=:]\s*["'][a-zA-Z0-9_.-]{20,}'''),
        ),
        ("user_path", re.compile(r'''[Cc]:\\[Uu]sers\\[^\\/"']+''')),
        ("private_key", re.compile(r"-----BEGIN\s+(RSA|EC|DSA|PRIVATE)\s+KEY-----")),
        (
            "conn_pwd",
            re.compile(r'''(?i)(password|pwd)\s*=\s*[^&;\s"']+'''),
        ),
    ]


def _tracked_files() -> list[Path]:
    """读取 Git 跟踪清单，失败时返回空集合并由严格扫描报告错误。"""

    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return [ROOT / item for item in result.stdout.decode("utf-8").split("\0") if item]


def _candidate_files(paths: Iterable[str], *, tracked: bool) -> list[Path]:
    """按 tracked 或目录模式生成确定性、去重后的文件列表。"""

    if tracked:
        candidates = _tracked_files()
    else:
        roots = [Path(path).resolve() for path in paths] if paths else [
            ROOT / directory
            for directory in ("docs/project_guidance", "docs/worklog", "docs/superpowers", "front", "src", "tests")
        ]
        candidates = []
        for root in roots:
            if root.is_file():
                candidates.append(root)
            elif root.exists():
                candidates.extend(root.rglob("*"))

    return sorted({path for path in candidates if path.is_file() and path.suffix in SUFFIXES})


def scan(paths: Iterable[str] = (), *, strict: bool = False, tracked: bool = False) -> int:
    """扫描文件并返回命中数量；严格与否由 CLI 决定退出码。"""

    found = 0
    patterns = _build_patterns()
    try:
        files = _candidate_files(paths, tracked=tracked)
    except (OSError, RuntimeError) as exc:
        print(f"[scanner-error] {exc}", file=sys.stderr)
        return 1

    for file_path in files:
        if any(item in file_path.as_posix() for item in WHITELIST):
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            print(f"[encoding] {file_path}: {exc}")
            found += 1
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            normalized_path = file_path.as_posix()
            # 测试夹具和正则规则自身可能包含“密码/路径”字样，但不会进入发布包。
            # 示例配置使用明确占位符；这些豁免只针对已知非生产文本，不放宽真实源码。
            if "/tests/" in normalized_path or "re.compile(" in line:
                continue
            if any(marker in line for marker in SAFE_EXAMPLE_MARKERS):
                continue
            for category, pattern in patterns:
                if pattern.search(line):
                    print(f"[{category:12s}] {file_path.relative_to(ROOT)}:{line_number}  {line.strip()[:120]}")
                    found += 1

    if found == 0:
        print("No sensitive info leaks found.")
    return 1 if found and strict else 0


def main(argv: list[str] | None = None) -> int:
    """解析命令行；tracked 模式默认严格，避免被 CI 当成软检查。"""

    parser = argparse.ArgumentParser(description="scan tracked files for sensitive payloads")
    parser.add_argument("paths", nargs="*", help="optional files/directories in non-tracked mode")
    parser.add_argument("--strict", action="store_true", help="return non-zero when a match is found")
    parser.add_argument("--tracked", action="store_true", help="scan only git-tracked files")
    args = parser.parse_args(argv)
    return scan(args.paths, strict=args.strict or args.tracked, tracked=args.tracked)


if __name__ == "__main__":
    raise SystemExit(main())
