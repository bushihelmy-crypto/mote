from __future__ import annotations

from pathlib import Path

from mote.product.extensions.sources import ExtensionSourcePolicy
from mote.product.skills.skill_pool import SkillPool


def _pool(tmp_path: Path, body: str) -> SkillPool:
    root = tmp_path / "builtin"
    skill_dir = root / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    policy = ExtensionSourcePolicy(user_root=tmp_path / "user", builtin_roots=(root,))
    pool = SkillPool(source_dirs=[root], source_policy=policy)
    pool.load_all()
    return pool


def test_skill_activation_binds_manifest_source_and_runtime_generations(tmp_path: Path) -> None:
    pool = _pool(
        tmp_path,
        "---\nname: demo\ndescription: governed\ncontext: fork\nallowed-tools:\n  - Read\n"
        "model: route-1\neffort: high\n---\nUse the approved source.\n",
    )

    snapshot = pool.get("demo")
    assert snapshot is not None
    assert snapshot.manifest.allowed_tools == ("Read",)
    assert snapshot.source.canonical_path.is_absolute()
    assert snapshot.source.content_digest.startswith("sha256:")
    assert snapshot.source.approval_generation == "mote.extension-approvals/v1"
    assert snapshot.tool_binding_generation >= 1
    assert snapshot.tokenizer_identity == "mote.runtime.text-tokenizer/default-v1"
    assert snapshot.token_cost >= 1


def test_invalid_frontmatter_cannot_activate_capabilities(tmp_path: Path) -> None:
    pool = _pool(
        tmp_path,
        "---\nname: demo\ndescription: governed\ncontext: arbitrary\nunknown-capability: true\n---\nbody\n",
    )

    assert pool.get_skill_count() == 0
