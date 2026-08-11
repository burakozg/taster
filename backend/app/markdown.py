"""Render a validated note model into the markdown+frontmatter shape
described in tasting-log-design.md §5."""
from __future__ import annotations

import yaml

from app.schema import AnyNote


def render_markdown(note: AnyNote) -> str:
    data = note.model_dump(
        mode="json", exclude={"notes", "common_notes"}, exclude_none=True, exclude_defaults=False
    )
    # yaml.safe_dump with sort_keys=False preserves a stable, deterministic
    # field order (matters for readability in Obsidian and for cache-friendly
    # re-renders); we control the field order via the model, not the dumper.
    frontmatter = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)

    # Two note dimensions in the body: the user's own impressions, and the
    # vendor/web/common-knowledge profile. Only render sections that exist.
    sections = []
    if note.notes.strip():
        sections.append(f"## My notes\n\n{note.notes.strip()}")
    common = getattr(note, "common_notes", "")
    if common.strip():
        sections.append(f"## Tasting profile (web/common)\n\n{common.strip()}")
    body = "\n\n".join(sections)
    return f"---\n{frontmatter}---\n{body}\n"
