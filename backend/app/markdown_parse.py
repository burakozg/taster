"""Inverse of markdown.render_markdown — parse a vault note's markdown back
into a raw note dict, so reverse-sync (reconcile.py) can fold a human's
Obsidian edit into the queryable JSON document.

The forward shape (see markdown.py) is:

    ---
    <yaml frontmatter: every field except notes/common_notes>
    ---
    ## My notes

    <the user's own impressions>

    ## Tasting profile (web/common)

    <the vendor/common profile>

This parser is deliberately lenient about the body: a human editing in
Obsidian may reorder, drop, or lightly reword the section bodies, but the
two headings are stable text we emit ourselves. Anything that fails to parse
raises ValueError, which the reconciler logs and skips (the vault file is
left untouched — the JSON doc is the only thing that goes stale).
"""
from __future__ import annotations

import re

import yaml

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_MY_NOTES = "My notes"
_PROFILE = "Tasting profile (web/common)"


def _extract_section(body: str, title: str) -> str:
    # Capture everything under `## <title>` up to the next `## ` or the end.
    m = re.search(
        rf"^##\s+{re.escape(title)}\s*\n(.*?)(?=\n##\s|\Z)",
        body,
        re.DOTALL | re.MULTILINE,
    )
    return m.group(1).strip() if m else ""


def parse_markdown(md: str) -> dict:
    """Return a raw note dict suitable for schema.parse_any_note. Raises
    ValueError if the frontmatter is missing or not a mapping."""
    m = _FRONTMATTER_RE.match(md)
    if not m:
        raise ValueError("note has no YAML frontmatter block")

    frontmatter, body = m.group(1), m.group(2)
    data = yaml.safe_load(frontmatter)
    if not isinstance(data, dict):
        raise ValueError("frontmatter did not parse to a mapping")

    # The two body dimensions live outside the frontmatter (markdown.py splits
    # them out); fold them back in. Pairing notes have no common_notes — only
    # set it when the section is actually present.
    data["notes"] = _extract_section(body, _MY_NOTES)
    profile = _extract_section(body, _PROFILE)
    if profile:
        data["common_notes"] = profile

    return data
