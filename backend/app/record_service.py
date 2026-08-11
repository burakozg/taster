"""Direct edit / delete of a single record — behind the Search item detail
view. Distinct from the AI `manage` flow (that plans knowledge-based bulk
edits); this is the user changing or removing one record by hand.

Both go through the schema (`parse_any_note`), so a hand edit can't write a
document the capture path wouldn't accept, and both keep the Obsidian file in
step: an edit re-projects it, a delete soft-deletes it (which also stops
reverse-sync from resurrecting the record from a now-stale file).
"""
from __future__ import annotations

import logging
from typing import Any

from app.couchdb_client import CouchDBClient, vault_path
from app.markdown import render_markdown
from app.schema import parse_any_note
from app.sync_service import _fold_legacy

logger = logging.getLogger("worker.record")


def _note_from_doc(doc: dict):
    # Fold legacy fields so an older record (e.g. cigar with origin_country)
    # still parses when we need its note form.
    return parse_any_note(_fold_legacy(doc))


async def update_record(db: CouchDBClient, doc_id: str, fields: dict[str, Any]) -> dict:
    """Apply hand edits to one record: merge fields, re-validate, rewrite the
    JSON doc and re-project the vault file."""
    target = await db.get_document(doc_id)
    if target is None:
        raise ValueError(f"record not found: {doc_id}")

    old_note = None
    try:
        old_note = _note_from_doc(target)
    except Exception:  # noqa: BLE001 — only needed for rename cleanup; ignore if unparseable
        pass

    # Fold legacy fields on the stored record first (so editing an un-normalized
    # old record doesn't drop e.g. origin_country's value), then apply the edits.
    data = _fold_legacy({k: v for k, v in target.items() if k not in ("_rev", "markdown")})
    data.update(fields)  # explicit user edits win

    note = parse_any_note(data)  # schema enforcement (raises on an invalid edit)
    markdown = render_markdown(note)
    await db.update_note(note, markdown, doc_id=target["_id"], rev=target["_rev"])

    # If the edit changed name/producer/date, the vault path moved — remove the
    # stale file so Obsidian doesn't keep a duplicate and reverse-sync doesn't
    # re-import the old content over this edit.
    if old_note is not None and vault_path(old_note) != vault_path(note):
        try:
            await db.soft_delete_vault_file(old_note)
        except Exception as e:  # noqa: BLE001
            logger.warning("update_record: could not remove stale file for %s: %s", doc_id, e)

    logger.info("update_record: updated doc_id=%s fields=%s", doc_id, list(fields.keys()))
    return {"ok": True, "doc_id": target["_id"]}


async def delete_record(db: CouchDBClient, doc_id: str) -> dict:
    """Delete one record: remove the JSON doc and soft-delete its vault file."""
    target = await db.get_document(doc_id)
    if target is None:
        return {"ok": True, "already_gone": True}

    await db.delete_document(doc_id, target["_rev"])
    try:
        await db.soft_delete_vault_file(_note_from_doc(target))
    except Exception as e:  # noqa: BLE001 — JSON doc already gone; file cleanup is best-effort
        logger.warning("delete_record: could not remove vault file for %s: %s", doc_id, e)

    logger.info("delete_record: deleted doc_id=%s", doc_id)
    return {"ok": True, "doc_id": doc_id}
