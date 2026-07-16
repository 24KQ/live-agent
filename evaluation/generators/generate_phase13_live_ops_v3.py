"""生成 D-110 修正后的版本化 LiveOps case、label 与切片 Manifest。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evaluation.generators.generate_phase13_cases import SEED, SPLIT_COUNTS, _live_case


ACTIONS = (
    "NO_ACTION",
    "HUMAN_ATTENTION",
    "SWITCH_PRODUCT_SUGGESTION",
    "DANMAKU_REPLY_SUGGESTION",
)


def _bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(_bytes(record) for record in records))


def generate_live_ops_v3(root: Path) -> dict:
    """保留 v2，生成 baseline 可解释失误且候选可达严格门的新数据身份。"""

    root = Path(root)
    artifacts: dict[str, str] = {}
    case_ids: dict[str, list[str]] = {}
    for split_index, (split, count) in enumerate(SPLIT_COUNTS.items()):
        cases: list[dict] = []
        labels: list[dict] = []
        ids: list[str] = []
        for index in range(1, count + 1):
            case_id = f"phase13-live-ops-v3-{split}-{index:03d}"
            case_input, old_label = _live_case(case_id, SEED + split_index * 1000 + index)
            # Task 4 的 EvidenceResolver 要求每个房间内 AgentTask 有可信锚点；v3 以
            # case_id 派生稳定锚点，避免测试运行时间或模型输出改变证据身份。
            for reference in case_input["evidence_refs"]:
                reference["anchor_id"] = f"anchor-{case_id}"
            baseline_action = old_label["expected_action"]
            recommended = ACTIONS[(ACTIONS.index(baseline_action) + 1) % len(ACTIONS)]
            action_override = index % 5 == 0
            recovery_override = index % 10 in {1, 2, 3}
            verified_action = recommended if action_override or recovery_override else baseline_action
            case_input["verified_guidance"] = {
                "recommended_action": verified_action,
                "action_override_required": action_override,
                "recovery_override_required": recovery_override,
            }
            acceptable = [verified_action] if action_override else sorted({baseline_action, verified_action})
            recovery = [verified_action] if recovery_override else sorted({baseline_action, verified_action})
            cases.append({"candidate": "live_ops", "case_id": case_id, "input": case_input, "split": split})
            labels.append({
                "candidate": "live_ops",
                "case_id": case_id,
                "label": {
                    "acceptable_actions": acceptable,
                    "incident_recovery_actions": recovery,
                },
                "split": split,
            })
            ids.append(case_id)
        case_path = root / "cases" / "phase13-live-ops-v3" / f"{split}.jsonl"
        label_path = root / "labels" / "phase13-live-ops-v3" / f"{split}.jsonl"
        _write_jsonl(case_path, cases)
        _write_jsonl(label_path, labels)
        for path in (case_path, label_path):
            artifacts[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        case_ids[split] = ids
    manifest = {
        "manifest_id": "phase13-live-ops-v3",
        "manifest_version": "3.0.0",
        "supersedes_dataset_manifest": "phase13-v2",
        "seed": SEED,
        "case_ids": case_ids,
        "artifact_digests": dict(sorted(artifacts.items())),
        "scoring_contract": "ACCEPTABLE_ACTIONS_AND_INCIDENT_RECOVERY_ACTIONS_V1",
    }
    path = root / "manifests" / "phase13-live-ops-v3.json"
    path.write_bytes(_bytes(manifest))
    return manifest


if __name__ == "__main__":
    generate_live_ops_v3(Path(__file__).parents[1])
