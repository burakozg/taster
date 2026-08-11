"""Reverse-sync: fold a human's Obsidian edit back into the queryable JSON doc.

The worker writes each note twice (see couchdb_client.py): a structured JSON
document that every query reads, and a LiveSync file projection that Obsidian
reads/writes. Editing the note in Obsidian only changes the LiveSync copy, so
without this the JSON doc — and therefore /items, /lookup, repeat-detection —
goes stale. This module closes that loop.

Each pass consumes one page of CouchDB's `_changes` feed since a persisted
cursor, keeps only edited vault-file entries under `Tastings/`, reassembles
their markdown from the referenced chunks, parses it back to a note dict
(markdown_parse), and updates the matching JSON doc. The match is by `uid`
(stable across renames — that's the whole point of the field), falling back
to the derived `_id` for older notes that predate uid.

Safety properties:
- Idempotent: if the reassembled markdown equals what the JSON doc already
  stores, nothing is written — so the worker's own capture-time writes (which
  also appear in _changes) are no-ops here, no feedback loop.
- Non-destructive: a vault-file deletion is logged and skipped, not mirrored
  into a JSON-doc delete (append-only philosophy; the user didn't ask to
  delete records by removing files).
- Fail-soft: a single unparseable/invalid edit is logged and skipped; the
  vault file is never modified, so the human can fix it and it re-syncs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.couchdb_client import CouchDBClient
from app.markdown_parse import parse_markdown
from app.schema import parse_any_note

logger = logging.getLogger("worker.reconcile")

_VAULT_PREFIX = "Tastings/"
# LiveSync entry docs (the file-level documents) carry these `type` values;
# "leaf" chunks and our own JSON note docs are deliberately excluded.
_ENTRY_TYPES = ("plain", "newnote")


def _is_vault_entry(doc: dict | None) -> bool:
    return bool(
        doc
        and doc.get("type") in _ENTRY_TYPES
        and isinstance(doc.get("children"), list)
        and str(doc.get("path", "")).startswith(_VAULT_PREFIX)
        # LiveSync soft-deletes set `deleted: true` on the entry (distinct from
        # CouchDB's own `_deleted` on the _changes row).
        and not doc.get("deleted")
    )


async def _reassemble_markdown(db: CouchDBClient, children: list[str]) -> str:
    """Concatenate the leaf chunks an entry points at, in order — the inverse
    of write_note()'s content-addressed chunking. Obsidian may split a large
    file across several chunks; small tasting notes are usually one."""
    parts: list[str] = []
    for chunk_id in children:
        chunk = await db.get_document(chunk_id)
        if chunk and "data" in chunk:
            parts.append(chunk["data"])
    return "".join(parts)


async def _reconcile_entry(db: CouchDBClient, entry: dict) -> bool:
    """Fold one edited vault entry into its JSON doc. Returns True if a doc
    was actually updated."""
    path = entry.get("path")
    markdown = await _reassemble_markdown(db, entry["children"])
    if not markdown.strip():
        return False

    data = parse_markdown(markdown)  # may raise ValueError -> caller logs/skips

    # Locate the target record: by stable uid first, else by the id derived
    # from the (possibly edited) content.
    uid = data.get("uid")
    target: dict | None = None
    if uid:
        matches = await db.find({"uid": uid}, limit=1)
        target = matches[0] if matches else None
    if target is None:
        target = await db.get_document(parse_any_note(data).doc_id())

    if target is None:
        logger.info("reconcile: no matching record for path=%s uid=%s — skipping", path, uid)
        return False

    # No semantic change (e.g. this is the worker's own just-written note
    # coming back around) — nothing to do.
    if target.get("markdown") == markdown:
        return False

    # An older note whose vault file predates uid: adopt the record's uid (if
    # it has one) so future edits match by uid even after a rename.
    if not uid and target.get("uid"):
        data["uid"] = target["uid"]

    note = parse_any_note(data)  # re-validate the human's edit before storing
    new_doc = note.model_dump(mode="json", exclude_none=True)
    new_doc["_id"] = target["_id"]        # CouchDB _id is immutable; keep it
    new_doc["_rev"] = target["_rev"]
    new_doc["markdown"] = markdown        # store the human's text verbatim
    new_doc["updated"] = datetime.now(timezone.utc).isoformat()
    if target.get("uid") and not new_doc.get("uid"):
        new_doc["uid"] = target["uid"]

    await db.update_document(new_doc)
    logger.info(
        "reconcile: updated record id=%s uid=%s from vault edit path=%s",
        target["_id"], new_doc.get("uid"), path,
    )
    return True


async def reconcile_vault_edits(db: CouchDBClient) -> int:
    """Run one reverse-sync pass. Returns the number of JSON docs updated.

    On the very first run (no cursor yet) it bookmarks the current update_seq
    and returns without touching anything — reverse-sync reacts to edits made
    from deployment onward, not the whole history."""
    state = await db.load_reconcile_state()
    rev = state.get("_rev")
    since = state.get("seq")

    if since is None:
        seq0 = await db.current_update_seq()
        await db.save_reconcile_state(seq0, rev)
        logger.info("reconcile: cursor initialized at seq=%s (future edits only)", seq0)
        return 0

    changes = await db.changes_since(since)
    updated = 0
    for row in changes.get("results", []):
        doc = row.get("doc")
        if not _is_vault_entry(doc):
            continue
        try:
            if await _reconcile_entry(db, doc):
                updated += 1
        except Exception as e:  # noqa: BLE001 — one bad edit must not stall the feed
            logger.warning("reconcile: skipped path=%s: %s", (doc or {}).get("path"), e)

    # Advance the cursor even past entries we skipped: at-most-once, so a
    # genuinely broken edit doesn't wedge the feed. Re-saving the same note in
    # Obsidian re-triggers it.
    await db.save_reconcile_state(str(changes.get("last_seq")), rev)
    if updated:
        logger.info("reconcile: applied %d Obsidian edit(s)", updated)
    return updated
