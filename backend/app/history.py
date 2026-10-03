"""Change history shared by everything that edits records in bulk.

Pairing regenerations and fetched details both keep what they REPLACED here so
a person can undo them. It is a plain JSON-lines file in a directory that both
the worker (jobs from the phone app) and taster-admin (the web page) mount, so
a change made from either can be undone from either — one history, not two.

Entries are appended with a single small write (atomic for a line this size), so
the two containers cannot corrupt each other's lines; the file is trimmed to the
newest HISTORY_KEEP entries when it grows well past that.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("worker.history")

HISTORY_KEEP = 500


def _path() -> Path:
    return Path(os.environ.get("ADMIN_DATA_DIR", "/data")) / "history.jsonl"


def append(entries: list[dict[str, Any]]) -> None:
    """Record applied changes. Undo is a convenience: a write failure is logged,
    never raised, so it can't fail an edit that already succeeded."""
    if not entries:
        return
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > HISTORY_KEEP * 2:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n", encoding="utf-8")
            tmp.replace(path)
    except OSError as e:
        logger.warning("could not write %s: %s", path, e)


def read(limit: int = HISTORY_KEEP) -> list[dict[str, Any]]:
    """Newest first."""
    path = _path()
    try:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return list(reversed(rows[-limit:]))
