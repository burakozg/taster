"""Read back the JSON-lines logs the worker and taster-admin write (see
logging_setup.setup_logging) for the admin portal's Logs tab."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.logging_setup import log_dir

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_TAIL_BYTES = 1_500_000   # per file: plenty for the recent past, bounded in memory


def _tail_lines(path: Path) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > _TAIL_BYTES and lines else lines   # first line may be cut


def sources() -> list[str]:
    try:
        return sorted({p.name.split(".log")[0] for p in log_dir().glob("*.log")})
    except OSError:
        return []


def read_logs(*, source: str | None = None, level: str = "INFO", q: str = "", limit: int = 300) -> list[dict[str, Any]]:
    """Newest first. `level` is a minimum; `q` matches the message or logger,
    case-insensitively."""
    floor = _LEVELS.get(level.upper(), 20)
    needle = q.strip().lower()
    names = [source] if source else sources()
    rows: list[dict[str, Any]] = []
    for name in names:
        if not name or "/" in name or ".." in name:
            continue
        base = log_dir() / f"{name}.log"
        for path in (base.with_name(base.name + ".1"), base):   # older first
            for line in _tail_lines(path):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if _LEVELS.get(r.get("level", ""), 0) < floor:
                    continue
                if needle and needle not in f"{r.get('msg', '')} {r.get('logger', '')}".lower():
                    continue
                r["source"] = name
                rows.append(r)
    rows.sort(key=lambda r: r.get("t", ""), reverse=True)
    return rows[:limit]
