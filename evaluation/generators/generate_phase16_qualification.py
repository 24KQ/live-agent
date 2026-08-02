"""生成并重冻结 Phase 16 qualification policy 与公开 corpus 资产。"""

from __future__ import annotations

from pathlib import Path

from src.decision_support.phase16_qualification import (
    PHASE16_QUALIFICATION_ASSET_DIRECTORY,
    generate_phase16_qualification_corpus,
    write_phase16_qualification_policy,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    """仅重建公开 development/validation 资产；密封 holdout 由独立 release owner 管理。"""

    policy = write_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    manifest = generate_phase16_qualification_corpus(
        _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        repository_root=_PROJECT_ROOT,
        policy=policy,
    )
    print(f"policy_digest={policy.policy_digest}")
    print(f"manifest_digest={manifest.manifest_digest}")


if __name__ == "__main__":
    main()
