"""文档编码检查脚本。

用法：
    python scripts/check_doc_encoding.py

默认会扫描项目里的文档目录，检查三类风险：
1. UTF-8 解码失败。
2. 已经写入文件的 U+FFFD 替换字符。
3. 高置信度的 mojibake 片段。

脚本只读，不会修改任何文件。它的作用是把“终端显示乱码”和“文件内容真的
坏了”分开，避免后续再靠目测猜编码。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = (
    PROJECT_ROOT / "docs" / "project_guidance",
    PROJECT_ROOT / "docs" / "worklog",
    PROJECT_ROOT / "docs" / "superpowers" / "specs",
    PROJECT_ROOT / "docs" / "superpowers" / "plans",
)

# 这些片段是典型的 UTF-8 被错误转写后留下的高风险字符。
# 这里只保留少量高置信片段，宁可少报一点，也不要把正常中文误判太多。
# Python docstring 中常见的中文 mojibake 片段（UTF-8 被误认为 GBK/Latin-1 解码后的残留）
# 来源：反复出现的 docstring 乱码如 "þ����ڵ㡣"、"���ֻþ�" 等
# 只保留高置信片段，宁可漏报也不误报
MOJIBAKE_FRAGMENTS = (
    "锟斤拷",
    "鏂",
    "鍓",
    "绔",
    "閫",
    "鍚",
    "杩",
    "鐩",
    "鍐",
    "浠",
    "瀹",
    "瑙",
    "鎾",
    "銆",
    "锛",
    "鍗",
    "鏁",
    "鍙",
    "鐪",
    # Python docstring 特有乱码（UTF-8 bytes 被 Latin-1 解码）
    "þ",    # \xfe 或 \xc3\xbe 的残留
    "��",   # Latin-1 解码 U+FFFD 后的二次乱码
    "�þ�", # 常见 docstring 开头乱码模式
    "���",  # 三个连续 replacement char（常见于大段中文损坏）
)


@dataclass(frozen=True)
class DocIssue:
    """单条文档编码问题。"""

    path: Path
    line_no: int | None
    severity: str
    category: str
    detail: str


def _display_path(path: Path) -> str:
    """把绝对路径尽量收敛成相对路径，方便人工阅读。"""

    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _summarize_line(line: str, limit: int = 160) -> str:
    """把过长的行裁短，避免终端输出被一大段内容淹没。"""

    compact = line.replace("\t", " ").strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _safe_print(message: str) -> None:
    """终端编码不一致时仍能打印诊断信息。

    Windows PowerShell 默认可能使用 GBK，遇到 U+FFFD 等字符会抛
    UnicodeEncodeError。这里用 backslashreplace 把不可编码字符转成
    `\\uXXXX` 形式，保证扫描脚本本身不会因为要报告乱码而崩溃。
    """

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_message = message.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")
    print(safe_message)


def scan_file(path: Path) -> list[DocIssue]:
    """扫描单个文件的编码风险。"""

    issues: list[DocIssue] = []
    raw = path.read_bytes()

    if raw.startswith(b"\xef\xbb\xbf"):
        issues.append(
            DocIssue(
                path=path,
                line_no=1,
                severity="warning",
                category="utf8_bom",
                detail="文件带有 UTF-8 BOM，项目规范建议使用无 BOM UTF-8。",
            )
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        issues.append(
            DocIssue(
                path=path,
                line_no=None,
                severity="error",
                category="utf8_decode_error",
                detail=str(exc),
            )
        )
        text = raw.decode("utf-8", errors="replace")

    for line_no, line in enumerate(text.splitlines(), start=1):
        if "\ufffd" in line:
            issues.append(
                DocIssue(
                    path=path,
                    line_no=line_no,
                    severity="error",
                    category="replacement_char",
                    detail=_summarize_line(line),
                )
            )

        fragments = [fragment for fragment in MOJIBAKE_FRAGMENTS if fragment in line]
        if fragments:
            issues.append(
                DocIssue(
                    path=path,
                    line_no=line_no,
                    severity="warning",
                    category="mojibake_fragment",
                    detail=f"{_summarize_line(line)} | 命中片段: {', '.join(fragments[:4])}",
                )
            )

    return issues




def scan_py_file(path: Path) -> list[DocIssue]:
    """扫描 Python 文件的编码和 docstring 风险。

    检查 BOM、换行符一致性、以及 docstring 中的 mojibake 片段。
    """
    issues: list[DocIssue] = []
    raw = path.read_bytes()

    # 检查 UTF-8 BOM
    if raw.startswith(b"\xef\xbb\xbf"):
        issues.append(
            DocIssue(
                path=path,
                line_no=1,
                severity="warning",
                category="utf8_bom",
                detail="文件带有 UTF-8 BOM，项目规范建议使用无 BOM UTF-8。",
            )
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        issues.append(
            DocIssue(
                path=path,
                line_no=None,
                severity="error",
                category="utf8_decode_error",
                detail=str(exc),
            )
        )
        text = raw.decode("utf-8", errors="replace")

    # 换行符一致性检查
    crlf_count = text.count("\r\n")
    lf_only_count = text.count("\n") - crlf_count
    if crlf_count > 0 and lf_only_count > 0:
        issues.append(
            DocIssue(
                path=path,
                line_no=None,
                severity="warning",
                category="mixed_line_endings",
                detail=f"文件同时包含 CRLF ({crlf_count} 行) 和 LF ({lf_only_count} 行) 换行符。",
            )
        )

    # 逐行扫描 docstring 中的 mojibake
    # 对扫描脚本自身跳过 mojibake 检查（因为检查列表中的字符串字面量会被误报）
    is_self = path.name == "check_doc_encoding.py"
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        if "\ufffd" in line:
            issues.append(
                DocIssue(
                    path=path,
                    line_no=line_no,
                    severity="error",
                    category="replacement_char",
                    detail=_summarize_line(line),
                )
            )

        if not is_self:
            fragments = [f for f in MOJIBAKE_FRAGMENTS if f in line]
            if fragments:
                issues.append(
                    DocIssue(
                        path=path,
                        line_no=line_no,
                        severity="warning",
                        category="mojibake_fragment",
                        detail=f"{_summarize_line(line)} | 命中片段: {', '.join(fragments[:4])}",
                    )
                )

    return issues


def scan_roots(roots: Iterable[Path]) -> list[DocIssue]:
    """扫描多个根目录。"""

    issues: list[DocIssue] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            issues.extend(scan_file(path))
    return issues


def print_report(issues: list[DocIssue]) -> None:
    """打印可读报告。"""

    if not issues:
        _safe_print("未发现 UTF-8 解码错误、替换字符或高置信 mojibake 片段。")
        return

    error_count = sum(1 for issue in issues if issue.severity == "error")
    warning_count = sum(1 for issue in issues if issue.severity == "warning")
    _safe_print(f"发现 {error_count} 个错误、{warning_count} 个警告：")

    for issue in issues:
        level = "ERROR" if issue.severity == "error" else "WARN "
        location = _display_path(issue.path)
        if issue.line_no is None:
            _safe_print(f"[{level}] {location} :: {issue.category} :: {issue.detail}")
        else:
            _safe_print(f"[{level}] {location}:{issue.line_no} :: {issue.category} :: {issue.detail}")


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="扫描项目文档的 UTF-8 编码风险。")
    parser.add_argument(
        "paths",
        nargs="*",
        help="可选扫描路径；不传时默认扫描 docs/project_guidance、docs/worklog、docs/superpowers/specs、docs/superpowers/plans。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """脚本入口。"""

    parser = build_parser()
    args = parser.parse_args(argv)

    roots = [Path(path).resolve() for path in args.paths] if args.paths else list(DEFAULT_ROOTS)
    issues = scan_roots(roots)

    # 也扫描 .py 文件（src/ tests/ scripts/）
    py_roots = [
        PROJECT_ROOT / "src",
        PROJECT_ROOT / "tests",
        PROJECT_ROOT / "scripts",
    ]
    for root in py_roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.py")):
            issues.extend(scan_py_file(p))

    print_report(issues)
    return 1 if any(issue.severity == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
