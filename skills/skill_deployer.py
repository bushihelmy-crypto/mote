"""Deploy builtin Skills to project workspace and generate SKILLS.md index."""

import shutil
from datetime import datetime, timezone
from pathlib import Path

from metagpt.common.const import ATOMS_DIR_NAME
from metagpt.common.logs import logger
from metagpt.skills.skill_definition import SkillDefinition


class SkillDeployer:
    """Copy builtin Skills into {workspace}/.atoms/skills/ and generate an index."""

    def deploy(self, workspace: Path, skills: list[SkillDefinition]) -> list[str]:
        """Deploy Skills to the workspace.

        Copies Skill directories from builtin/ into {workspace}/.atoms/skills/.
        Only deploys Skills that are in the provided list (already filtered by role).
        Existing directories are NOT overwritten.

        Args:
            workspace: Project workspace root.
            skills: List of SkillDefinitions to deploy (pre-filtered by role).

        Returns:
            List of deployed Skill directory names.
        """
        target_dir = workspace / ATOMS_DIR_NAME / "skills"
        target_dir.mkdir(parents=True, exist_ok=True)

        deployed = []

        for skill in skills:
            src = skill.source_path.parent  # SKILL.md's parent dir
            dst = target_dir / skill.name

            if dst.exists():
                logger.debug(f"SkillDeployer: '{skill.name}' already deployed, skipping")
                deployed.append(skill.name)
                continue

            try:
                shutil.copytree(src, dst)
                deployed.append(skill.name)
            except FileExistsError:
                # Another process deployed the same skill concurrently — safe to skip
                logger.debug(f"SkillDeployer: '{skill.name}' deployed by concurrent process, skipping")
                deployed.append(skill.name)
            except Exception as e:
                logger.warning(f"SkillDeployer: failed to copy {src} → {dst}: {e}")

        return deployed

    def generate_index(self, workspace: Path, skills: list[SkillDefinition]) -> Path:
        """Generate the Skills index file.

        Generates {workspace}/.atoms/SKILLS.md listing every deployed Skill.

        Args:
            workspace: Project workspace root.
            skills: List of SkillDefinitions to include in the index.

        Returns:
            Path to the generated index file.
        """
        atoms_dir = workspace / ATOMS_DIR_NAME
        atoms_dir.mkdir(parents=True, exist_ok=True)
        index_path = atoms_dir / "SKILLS.md"

        # Use absolute paths so Editor.read() works regardless of the agent's
        # current working_dir (e.g. Alex switches to app/frontend).
        skills_base = atoms_dir / "skills"

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        lines = [
            "---",
            "auto_generated: true",
            f"last_updated: {now}",
            "---",
            "",
            "# Available Skills",
            "",
            "The following Skills are available in this project. Use `Editor.read()` to",
            "load the full SKILL.md for detailed instructions when needed.",
            "",
            "| Skill | Description | Path |",
            "|-------|-------------|------|",
        ]

        for s in skills:
            abs_path = skills_base / s.name / "SKILL.md"
            safe_desc = s.description.replace("\n", " ").replace("|", r"\|")
            lines.append(f"| {s.name} | {safe_desc} | {abs_path} |")

        lines.append("")
        index_path.write_text("\n".join(lines), encoding="utf-8")
        return index_path
