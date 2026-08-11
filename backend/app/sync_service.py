"""Forced full-vault sync — the manual escape hatch for when the DB and the
Obsidian vault have drifted apart (see ARCHITECTURE.md's two-document model).

Four operations, all dispatched from worker.py:

- **status** (`sync_status`) — count the JSON note records vs. the live
  (non-deleted) LiveSync file entries under Tastings/, AND report the actual
  delta behind the counts: records with no vault file, files with no record,
  and records that *collide* onto one vault path (which is how the two counts
  can disagree while both one-sided deltas look empty).
- **rebuild_vault** (`rebuild_vault`) — DB → Obsidian. Re-project every
  record's LiveSync file, resurrecting any a human deleted in Obsidian. Safe:
  records are the source of truth, and this only writes the vault projection,
  never a record.
- **rebuild_records** (`rebuild_records`) — Obsidian → DB, UPSERT-ONLY. Parse
  every live vault file back into its JSON record; never deletes.
- **normalize** (`normalize_records`) — deterministic schema-drift fix (fold
  legacy field names, re-validate, rewrite). Idempotent.
"""
from __future__ import annotations

import logging

from app.categories import NOTE_TYPES
from app.couchdb_client import CouchDBClient, vault_path
from app.markdown import render_markdown
from app.markdown_parse import parse_markdown
from app.schema import parse_any_note

logger = logging.getLogger("worker.sync")

_NOTE_TYPES = list(NOTE_TYPES)
# Legacy field renames folded on normalize (see normalize_records). Kept as a
# list so future schema renames just append here.
_LEGACY_FIELD_MOVES = [
    ("origin_country", "country_of_origin"),
    ("grind_setting", "grind_size"),  # grinder-specific dial number → free-text grind
]
# LiveSync file entries carry these `type` values (confirmed against the live
# DB); "leaf" chunks and our JSON note docs are excluded.
_ENTRY_TYPES = ["plain", "newnote"]
_VAULT_PREFIX = "Tastings/"
_BIG = 100000  # personal-vault scale: one page covers everything


def _is_live_entry(doc: dict) -> bool:
    return (
        not doc.get("deleted")
        and str(doc.get("path", "")).startswith(_VAULT_PREFIX)
        and isinstance(doc.get("children"), list)
    )


async def count_state(db: CouchDBClient) -> dict:
    """{records, vault_files} — records are our JSON note docs; vault_files are
    the live LiveSync file entries under Tastings/ (tombstoned entries, chunks
    and note docs excluded). Selects on the indexed `type` field."""
    records = await db.find({"type": {"$in": _NOTE_TYPES}}, limit=_BIG, fields=["_id"])
    entries = await db.find({"type": {"$in": _ENTRY_TYPES}}, limit=_BIG, fields=["_id", "path", "deleted", "children"])
    vault_files = sum(1 for e in entries if _is_live_entry(e))
    return {"records": len(records), "vault_files": vault_files}


async def _reassemble_markdown(db: CouchDBClient, children: list[str]) -> str:
    parts: list[str] = []
    for chunk_id in children:
        chunk = await db.get_document(chunk_id)
        if chunk and "data" in chunk:
            parts.append(chunk["data"])
    return "".join(parts)


def _record_brief(doc: dict) -> dict:
    return {
        "_id": doc.get("_id"),
        "type": doc.get("type"),
        "name": doc.get("name") or doc.get("_id"),
        "producer": doc.get("producer"),
    }


async def _diff_records_and_files(db: CouchDBClient) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (records_without_file, files_without_record, colliding_records) —
    the actual delta behind the counts. A record's file entry is keyed by
    `vault_path(note).lower()` (see couchdb_client.project_vault_file), so the
    match is exact: compute that id for every record and compare to the live
    LiveSync entry ids. An unparseable record (can't derive a path) is reported
    as having no file rather than silently matched.

    `colliding_records` names the case where two+ records map to the SAME vault
    path — they can't each own a distinct file, so `records` (N) exceeds
    `vault_files` (1) even though every record technically has *a* file and
    every file has *a* record. Without this bucket that gap is invisible, which
    is exactly the "counts disagree but the delta shows nothing" confusion."""
    records = await db.find({"type": {"$in": _NOTE_TYPES}}, limit=_BIG)
    entries = await db.find(
        {"type": {"$in": _ENTRY_TYPES}}, limit=_BIG,
        fields=["_id", "path", "deleted", "children"],
    )
    live = [e for e in entries if _is_live_entry(e)]
    live_ids = {e["_id"] for e in live}

    # entry_id -> the records that project onto it, so a >1 group is a collision.
    records_by_id: dict[str, list[dict]] = {}
    records_without_file: list[dict] = []
    for doc in records:
        try:
            entry_id = vault_path(parse_any_note(_fold_legacy(doc))).lower()
        except Exception:  # noqa: BLE001 — a record we can't project can't be located
            entry_id = None
        if entry_id:
            records_by_id.setdefault(entry_id, []).append(doc)
        if not entry_id or entry_id not in live_ids:
            records_without_file.append(_record_brief(doc))

    files_without_record = [
        {"path": e.get("path")} for e in live if e["_id"] not in records_by_id
    ]
    colliding_records = [
        {"path": entry_id, "records": [_record_brief(d) for d in docs]}
        for entry_id, docs in records_by_id.items()
        if len(docs) > 1
    ]
    return records_without_file, files_without_record, colliding_records


async def sync_status(db: CouchDBClient) -> dict:
    state = await count_state(db)
    records_without_file, files_without_record, colliding_records = await _diff_records_and_files(db)
    logger.info(
        "sync status: records=%d vault_files=%d records_without_file=%d "
        "files_without_record=%d colliding_records=%d",
        state["records"], state["vault_files"],
        len(records_without_file), len(files_without_record), len(colliding_records),
    )
    return {
        **state,
        "records_without_file": records_without_file[:100],
        "files_without_record": files_without_record[:100],
        "colliding_records": colliding_records[:100],
    }


async def rebuild_vault(db: CouchDBClient) -> dict:
    """Re-project every record into its Obsidian file (DB → Obsidian).

    Always RE-RENDERS the markdown from the record's fields rather than reusing
    the stored blob — that's the whole point of this direction (the DB is the
    source of truth), and it's what cleans stale frontmatter (e.g. a lingering
    origin_country, or a missing country_of_origin) in files that normalize
    skipped because their JSON was already clean. Folds legacy fields first so
    an un-normalized record still renders cleanly."""
    docs = await db.find({"type": {"$in": _NOTE_TYPES}}, limit=_BIG)
    rebuilt = failed = 0
    errors: list[str] = []
    for doc in docs:
        try:
            note = parse_any_note(_fold_legacy(doc))  # _id/_rev/markdown are ignored extras
            markdown = render_markdown(note)
            await db.project_vault_file(note, markdown)
            rebuilt += 1
        except Exception as e:  # noqa: BLE001 — one bad record must not stop the rebuild
            failed += 1
            errors.append(f"{doc.get('_id')}: {e}")
            logger.warning("rebuild_vault: failed for id=%s: %s", doc.get("_id"), e)

    state = await count_state(db)
    logger.info("rebuild_vault: rebuilt=%d failed=%d -> %s", rebuilt, failed, state)
    return {"rebuilt": rebuilt, "failed": failed, "errors": errors[:20], **state}


async def rebuild_records(db: CouchDBClient) -> dict:
    """Obsidian → DB, UPSERT-ONLY. Parse every live vault file back into its
    JSON record (bulk reverse-sync). Matches by `uid`, else the derived id.

    Deliberately never deletes: a record whose file is missing is left alone.
    That is what makes this safe to run even against an empty vault — an empty
    vault simply produces no entries and changes nothing."""
    entries = await db.find({"type": {"$in": _ENTRY_TYPES}}, limit=_BIG)
    upserted = skipped = failed = 0
    errors: list[str] = []

    for entry in entries:
        if not _is_live_entry(entry):
            continue
        path = entry.get("path")
        try:
            markdown = await _reassemble_markdown(db, entry["children"])
            if not markdown.strip():
                skipped += 1
                continue
            data = parse_markdown(markdown)

            uid = data.get("uid")
            target = None
            if uid:
                matches = await db.find({"uid": uid}, limit=1)
                target = matches[0] if matches else None
            if target is None:
                target = await db.get_document(parse_any_note(data).doc_id())

            # Unchanged (e.g. a worker-projected file that never got edited).
            if target is not None and target.get("markdown") == markdown:
                skipped += 1
                continue

            # Adopt the record's uid for an older file that predates the field.
            if not uid and target is not None and target.get("uid"):
                data["uid"] = target["uid"]

            note = parse_any_note(data)  # re-validate the human's file
            new_doc = note.model_dump(mode="json", exclude_none=True)
            new_doc["_id"] = target["_id"] if target else note.doc_id()
            if target is not None:
                new_doc["_rev"] = target["_rev"]  # update in place; else create
            new_doc["markdown"] = markdown
            await db.update_document(new_doc)  # PUT: creates when no _rev, updates with one
            upserted += 1
        except Exception as e:  # noqa: BLE001 — one bad file must not stop the import
            failed += 1
            errors.append(f"{path}: {e}")
            logger.warning("rebuild_records: failed for path=%s: %s", path, e)

    state = await count_state(db)
    logger.info("rebuild_records: upserted=%d skipped=%d failed=%d -> %s", upserted, skipped, failed, state)
    return {"upserted": upserted, "skipped": skipped, "failed": failed, "errors": errors[:20], **state}


def _fold_legacy(doc: dict) -> dict:
    """Move any legacy field's value into its current name before revalidation
    (so the value survives when the old field gets dropped). Prefers a real
    value over an "unknown"/empty placeholder on the new field."""
    d = dict(doc)
    for old, new in _LEGACY_FIELD_MOVES:
        if d.get(old) and d.get(new) in (None, "", "unknown"):
            d[new] = d[old]
    return d


def _needs_normalize(doc: dict) -> bool:
    if doc.get("type") == "pairing":
        return False  # pairings have no country_of_origin
    if any(old in doc for old, _ in _LEGACY_FIELD_MOVES):
        return True  # carries a legacy field
    return not doc.get("country_of_origin")  # missing the now-mandatory field


async def normalize_records(db: CouchDBClient) -> dict:
    """Deterministic schema-drift fix: fold legacy fields (origin_country ->
    country_of_origin), then re-validate each item record through the current
    schema — which drops the legacy field and any other stray key — and
    rewrite both the JSON record and the Obsidian file. Idempotent: records
    already conforming are skipped, so it's safe to re-run."""
    docs = await db.find({"type": {"$in": _NOTE_TYPES}}, limit=_BIG)
    changed = skipped = failed = 0
    errors: list[str] = []

    for doc in docs:
        if not _needs_normalize(doc):
            skipped += 1
            continue
        try:
            note = parse_any_note(_fold_legacy(doc))  # revalidate -> drops origin_country et al.
            markdown = render_markdown(note)           # clean frontmatter (no legacy field)
            await db.update_note(note, markdown, doc_id=doc["_id"], rev=doc["_rev"])
            changed += 1
        except Exception as e:  # noqa: BLE001 — one bad record must not stop the migration
            failed += 1
            errors.append(f"{doc.get('_id')}: {e}")
            logger.warning("normalize_records: failed for id=%s: %s", doc.get("_id"), e)

    state = await count_state(db)
    logger.info("normalize_records: changed=%d skipped=%d failed=%d -> %s", changed, skipped, failed, state)
    return {"changed": changed, "skipped": skipped, "failed": failed, "errors": errors[:20], **state}
