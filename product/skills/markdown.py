"""Markdown and YAML frontmatter parser."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from mote.runtime.telemetry.logging import logger


@dataclass
class MarkdownDocument:
    """Parsed Markdown document with optional YAML frontmatter."""

    metadata: dict = field(default_factory=dict)
    content: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    source_path: Path = field(default_factory=lambda: Path())


class MarkdownMetaParser:
    """Parse Markdown files with YAML frontmatter and section splitting."""

    FRONTMATTER_DELIMITER = "---"

    def parse(self, file_path: Path) -> MarkdownDocument:
        """Parse a Markdown file into a MarkdownDocument.

        Extracts YAML frontmatter (between --- delimiters) and splits
        the body into sections by ## headings.

        Args:
            file_path: Path to the Markdown file.

        Returns:
            MarkdownDocument with metadata, content, and sections.
        """
        try:
            text = file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"MarkdownMetaParser: failed to read {file_path}: {e}")
            return MarkdownDocument(source_path=file_path)

        if not text.strip():
            return MarkdownDocument(source_path=file_path)

        metadata, body = self._extract_frontmatter(text)
        sections = self._split_sections(body)

        return MarkdownDocument(
            metadata=metadata,
            content=body,
            sections=sections,
            source_path=file_path,
        )

    def _extract_frontmatter(self, text: str) -> tuple[dict, str]:
        """Extract YAML frontmatter and return (metadata, body)."""
        stripped = text.strip()
        if not stripped.startswith(self.FRONTMATTER_DELIMITER):
            return {}, text

        # Find the closing delimiter
        after_first = stripped[len(self.FRONTMATTER_DELIMITER) :]
        end_idx = after_first.find(f"\n{self.FRONTMATTER_DELIMITER}")
        if end_idx == -1:
            # Only frontmatter, no body
            yaml_text = after_first.strip()
            body = ""
        else:
            yaml_text = after_first[:end_idx].strip()
            # Body starts after the closing ---
            rest = after_first[end_idx + len(f"\n{self.FRONTMATTER_DELIMITER}") :]
            body = rest.lstrip("\n")

        metadata = {}
        if yaml_text:
            try:
                parsed = yaml.safe_load(yaml_text)
                if isinstance(parsed, dict):
                    metadata = parsed
            except yaml.YAMLError as e:
                logger.warning(f"MarkdownMetaParser: YAML parse error: {e}")

        return metadata, body

    def _split_sections(self, body: str) -> dict[str, str]:
        """Split body into sections by ## headings.

        Content before the first ## heading (including # headings) is stored
        under the key "_preamble".
        """
        if not body.strip():
            return {}

        sections = {}
        current_heading = None
        current_lines = []
        preamble_lines = []

        for line in body.split("\n"):
            if line.startswith("## ") and not line.startswith("### "):
                # Save previous section
                if current_heading is not None:
                    sections[current_heading] = "\n".join(current_lines).strip()
                else:
                    # Lines before the first ## heading
                    preamble = "\n".join(preamble_lines).strip()
                    if preamble:
                        sections["_preamble"] = preamble
                current_heading = line[3:].strip()
                current_lines = []
            elif current_heading is not None:
                current_lines.append(line)
            else:
                preamble_lines.append(line)

        # Save last section
        if current_heading is not None:
            sections[current_heading] = "\n".join(current_lines).strip()
        elif preamble_lines:
            preamble = "\n".join(preamble_lines).strip()
            if preamble:
                sections["_preamble"] = preamble

        return sections

    @staticmethod
    def upsert_section(text: str, section_name: str, content: str) -> str:
        """Insert or replace content in a ## section of a markdown document.

        If the section contains only an HTML comment placeholder (<!-- ... -->),
        the comment is replaced with the new content. Otherwise, the content is
        appended right after the section heading line.

        Args:
            text: Full markdown text.
            section_name: Name of the ## section (without the ## prefix).
            content: Content to insert.

        Returns:
            Updated markdown text, or original text if section not found.
        """
        marker = f"## {section_name}"
        idx = text.find(marker)
        if idx == -1:
            return text

        # Find end of the marker line
        line_end = text.find("\n", idx)
        if line_end == -1:
            line_end = len(text)

        # Find the next section or end of file
        next_section = text.find("\n## ", line_end)
        if next_section == -1:
            next_section = len(text)

        # Check for existing comment placeholder
        existing = text[line_end:next_section].strip()
        if existing.startswith("<!--"):
            comment_end = text.find("-->", line_end)
            if comment_end != -1:
                comment_end += 3
                return text[: line_end + 1] + content + "\n" + text[comment_end:].lstrip("\n")

        # Append after the section heading
        insert_pos = line_end + 1
        return text[:insert_pos] + content + "\n\n" + text[insert_pos:]
