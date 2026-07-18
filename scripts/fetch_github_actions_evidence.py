"""Phase 15 Task 8 GitHub Actions 证据读取入口。

本地门禁只接受已经由托管环境固化的 JSON，不在测试或普通 CLI 中携带 token、
访问网络或把缺失 run 伪造成成功。真实 API 接入留给 Phase 15 Task 9 的工作流。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


class EvidenceValidationError(ValueError):
    """托管 run 身份、结论或 artifact 摘要不满足发布门禁。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _emit(payload: dict[str, object]) -> None:
    """以固定 key 顺序输出可审计的证据读取结果。"""

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    """构造证据文件、仓库和 run 身份校验参数。"""

    parser = argparse.ArgumentParser(description="Phase 15 GitHub Actions evidence")
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--repo")
    parser.add_argument("--run-id")
    parser.add_argument("--workflow")
    parser.add_argument("--commit-sha")
    parser.add_argument("--artifact-digest")
    parser.add_argument("--require-evidence", action="store_true")
    return parser


def _safe_evidence(evidence: dict[str, object]) -> dict[str, object]:
    """只输出身份和状态字段，防止未知托管响应中的 secret 被回显到日志。"""

    safe_keys = ("repo", "run_id", "status", "workflow", "event", "commit_sha", "artifact_digest")
    safe = {key: evidence[key] for key in safe_keys if key in evidence}
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    safe["evidence_digest"] = hashlib.sha256(canonical).hexdigest()
    return safe


def load_and_validate_evidence(
    path: Path | None,
    *,
    require: bool,
    repo: str | None = None,
    run_id: str | None = None,
    workflow: str | None = None,
    commit_sha: str | None = None,
    artifact_digest: str | None = None,
) -> dict[str, object]:
    """读取严格的托管证据，返回脱敏白名单；绝不回显原始 JSON。"""

    if path is None or not path.is_file():
        raise EvidenceValidationError("EXTERNAL_EVIDENCE_MISSING", "external Actions evidence is missing")
    try:
        if path.stat().st_size > 1_000_000:
            raise EvidenceValidationError("EXTERNAL_EVIDENCE_TOO_LARGE", "external Actions evidence is too large")
    except OSError as exc:
        raise EvidenceValidationError("EXTERNAL_EVIDENCE_INVALID", "external Actions evidence cannot be inspected") from exc
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(evidence, dict):
            raise TypeError("evidence must be an object")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError("EXTERNAL_EVIDENCE_INVALID", "external Actions evidence is invalid") from exc

    required_fields = ("repo", "workflow", "run_id", "commit_sha", "artifact_digest")
    if require and any(not evidence.get(field) for field in required_fields):
        raise EvidenceValidationError("EXTERNAL_EVIDENCE_INCOMPLETE", "external Actions identity fields are incomplete")
    expected_values = {
        "repo": repo,
        "run_id": run_id,
        "workflow": workflow,
        "commit_sha": commit_sha,
        "artifact_digest": artifact_digest,
    }
    if require and any(value is None for value in expected_values.values()):
        raise EvidenceValidationError("EXTERNAL_EVIDENCE_INCOMPLETE", "expected Actions identity is incomplete")
    forbidden = {"token", "secret", "authorization", "access_token", "log", "logs"}
    if any(str(key).lower() in forbidden for key in evidence):
        raise EvidenceValidationError("EXTERNAL_EVIDENCE_SENSITIVE", "external evidence contains sensitive fields")
    for field, expected in expected_values.items():
        if expected is not None and str(evidence.get(field)) != str(expected):
            raise EvidenceValidationError("EXTERNAL_EVIDENCE_IDENTITY_MISMATCH", f"external field mismatch: {field}")
    if str(evidence.get("status", evidence.get("conclusion", ""))).lower() != "success":
        raise EvidenceValidationError("EXTERNAL_RUN_NOT_SUCCESS", "external Actions run is not successful")
    if require:
        digest = str(evidence.get("artifact_digest", ""))
        commit = str(evidence.get("commit_sha", ""))
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            raise EvidenceValidationError("EXTERNAL_EVIDENCE_INVALID", "artifact digest is not SHA-256")
        if len(commit) < 7 or any(char not in "0123456789abcdef" for char in commit.lower()):
            raise EvidenceValidationError("EXTERNAL_EVIDENCE_INVALID", "commit SHA is not hexadecimal")
    return _safe_evidence(evidence)


def main(argv: list[str] | None = None) -> int:
    """读取并验证托管证据；缺失、身份漂移或非成功 run 均为 BLOCKED。"""

    args = _parser().parse_args(argv)
    try:
        safe = load_and_validate_evidence(
            args.evidence_file,
            require=args.require_evidence,
            repo=args.repo,
            run_id=args.run_id,
            workflow=args.workflow,
            commit_sha=args.commit_sha,
            artifact_digest=args.artifact_digest,
        )
    except EvidenceValidationError as exc:
        _emit({"status": "BLOCKED", "reason_code": exc.code, "external_calls": False})
        return 3
    _emit({"status": "PASS", "reason_code": "", "external_calls": False, "evidence": safe})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
