from __future__ import annotations

from pathlib import Path

from scripts.check_doc_encoding import scan_file, scan_py_file


def test_scan_file_accepts_clean_utf8(tmp_path: Path) -> None:
    """正常 UTF-8 文档不应被误报。"""

    path = tmp_path / "clean.md"
    path.write_text("# 标题\n这是正常中文内容。\n", encoding="utf-8")

    issues = scan_file(path)

    assert issues == []


def test_scan_file_reports_utf8_decode_error(tmp_path: Path) -> None:
    """非法 UTF-8 字节应当被报告。"""

    path = tmp_path / "broken.md"
    path.write_bytes(b"# title\n\xff\xfe\xfd\n")

    issues = scan_file(path)

    assert any(issue.category == "utf8_decode_error" and issue.severity == "error" for issue in issues)


def test_scan_file_reports_replacement_character(tmp_path: Path) -> None:
    """已经写入文件的 U+FFFD 替换字符应当被报告。"""

    path = tmp_path / "replacement.md"
    path.write_text("这里有一个坏字符：\ufffd\n", encoding="utf-8")

    issues = scan_file(path)

    assert any(issue.category == "replacement_char" and issue.severity == "error" for issue in issues)


def test_scanner_does_not_report_its_own_replacement_character_detection_literals() -> None:
    """扫描器内置的检测样例不是被扫描项目的损坏内容，不能让全仓门禁永久自失败。"""

    scanner_path = Path(__file__).resolve().parents[2] / "scripts" / "check_doc_encoding.py"

    issues = scan_py_file(scanner_path)

    assert not [issue for issue in issues if issue.severity == "error"]
