"""CouchDB client — durable store + Mango query engine for the backend.

Each note is written twice (see write_note):

1. A plain JSON document (frontmatter fields at the top level plus a
   `markdown` field) under the `{type}:{slug}:{date}` id. This is what every
   query in the app runs against — /items snapshots, /lookup's query_notes,
   repeat-detection, pairing resolution. LiveSync ignores it ("Skipped
   unexpected non-note document").

2. A LiveSync-format pair — a `h:…` leaf chunk holding the markdown text and
   an entry document keyed by the lowercased vault path — so the note
   materializes as a real file under Tastings/<Folder>/ in Obsidian. The
   format was reverse-engineered from documents a live Self-hosted LiveSync
   v0.25 client (E2EE off) wrote to this same database: entry docs carry
   {path, children: [chunk ids], ctime/mtime (epoch ms), size, type:
   "plain", eden: {}}, chunks carry {data, type: "leaf"} and are
   content-addressed (identical content shares a chunk). Chunk ids use our
   own `h:t…` hash namespace — LiveSync fetches children strictly by id, so
   not matching its internal xxhash scheme costs at most a duplicate chunk.

The capture write path is append-only (a new JSON doc per capture, never an
update), which keeps `_rev` conflicts out of that path. Reverse-sync
(reconcile.py) is the one sanctioned exception: it watches _changes for a
human's Obsidian edit to a vault file and folds the change back into the
matching JSON doc (by `uid`), updating it in place. It never touches the
LiveSync chunk/entry, so the vault file stays the human's to own and the two
directions don't fight over the same document.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.categories import VAULT_FOLDERS
from app.schema import AnyNote

logger = logging.getLogger(__name__)

# Folder layout in the vault (ARCHITECTURE.md "Obsidian (client-side)").
# Note-type -> Obsidian folder, from the category registry (single source of
# truth) so every category — including pipe/beer and any future one — is covered
# without editing this file.
_VAULT_FOLDERS = VAULT_FOLDERS

# Characters Obsidian refuses in filenames, plus path separators.
_FILENAME_BAD = str.maketrans({c: "-" for c in '\\/:*?"<>|#^[]'})


def vault_path(note: AnyNote) -> str:
    """Public accessor for a note's vault file path — used by record edits to
    detect a rename (which moves the file) and clean up the stale entry."""
    return _vault_path(note)


def _vault_path(note: AnyNote) -> str:
    folder = _VAULT_FOLDERS[note.item_type()]
    name = getattr(note, "name", None)
    if not name:  # pairing notes have no name — reuse the doc id's a+b slug
        name = note.doc_id().split(":")[1]
    producer = getattr(note, "producer", None)
    title = f"{producer} - {name}" if producer else str(name)
    # Coffee is rated per (bean, brew_method), so the method is part of the
    # filename too — otherwise two brews of one bean on one date collide.
    method = getattr(note, "brew_method", None)
    if method:
        title += f" - {method}"
    safe = " ".join(title.translate(_FILENAME_BAD).split())
    return f"{folder}/{safe} ({note.date.isoformat()}).md"


def _q(doc_id: str) -> str:
    # LiveSync entry ids are vault paths and contain "/" — leaving that
    # unencoded makes CouchDB parse it as db/doc/attachment segments.
    return quote(doc_id, safe="")


def _chunk_id(content: str) -> str:
    # Our own namespace inside LiveSync's `h:` chunk-id space; content-
    # addressed so identical markdown dedups to one chunk, like LiveSync's.
    return "h:t" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:24]


class CouchDBError(RuntimeError):
    pass


class CouchDBClient:
    def __init__(self, base_url: str, db: str, user: str, password: str) -> None:
        self._db = db
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(user, password),
            timeout=10.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ensure_db_and_indexes(self) -> None:
        """Create the database and Mango indexes if they don't already exist.

        Safe to call on every startup, including under a least-privilege
        member account (INSTALL.md step 6): existence is checked first,
        because a member issuing PUT /{db} gets 401 (only server admins may
        create databases) even when the db already exists. The PUT only runs
        when the db is genuinely missing — i.e. on the very first boot,
        which INSTALL.md runs under the admin bootstrap account. Index
        creation likewise needs db-admin rights; failures there are logged
        and swallowed since the first (admin) boot already created them.
        """
        resp = await self._client.head(f"/{self._db}")
        if resp.status_code == 404:
            resp = await self._client.put(f"/{self._db}")
            if resp.status_code not in (201, 202, 412):
                # WRK-6: operation named, credentials never in the message.
                logger.error("couchdb create db failed op=put_db db=%s status=%s", self._db, resp.status_code)
                raise CouchDBError(f"failed to create db: {resp.status_code} {resp.text}")
            logger.info("couchdb db created db=%s", self._db)
        elif resp.status_code != 200:
            logger.error("couchdb db check failed op=head_db db=%s status=%s", self._db, resp.status_code)
            raise CouchDBError(f"failed to check db existence: {resp.status_code} {resp.text}")
        else:
            logger.info("couchdb db exists db=%s", self._db)

        indexed_fields = [
            ["type"],
            ["type", "status"],
            ["type", "region"],
            ["type", "rating"],
            ["name"],
            ["producer"],
            ["tags"],
            ["uid"],  # reverse-sync matches an edited vault file to its record
        ]
        for fields in indexed_fields:
            resp = await self._client.post(
                f"/{self._db}/_index",
                json={"index": {"fields": fields}, "name": f"idx_{'_'.join(fields)}"},
            )
            if resp.status_code not in (200, 201):
                logger.warning("index creation for %s returned %s: %s", fields, resp.status_code, resp.text)

    async def write_note(self, note: AnyNote, markdown: str) -> str:
        """Write a note as a new document. Append-only per §5 — always a
        fresh _id (date-scoped), never an update to an existing note.

        Also writes the LiveSync-format projection (chunk + entry) so the
        note appears as a vault file; a failure there is logged but doesn't
        fail the capture — the JSON doc is the source of truth."""
        doc_id = note.doc_id()
        doc: dict[str, Any] = note.model_dump(mode="json", exclude_none=True)
        doc["_id"] = doc_id
        doc["markdown"] = markdown

        resp = await self._client.put(f"/{self._db}/{_q(doc_id)}", json=doc)
        if resp.status_code not in (201, 202):
            raise CouchDBError(f"failed to write note {doc_id}: {resp.status_code} {resp.text}")

        try:
            await self._write_livesync_file(note, markdown)
        except Exception:
            logger.exception("LiveSync projection failed for %s (JSON doc written fine)", doc_id)
        return doc_id

    async def project_vault_file(self, note: AnyNote, markdown: str) -> None:
        """Public entry point for (re)writing a note's LiveSync file — used by
        the DB→Obsidian rebuild (sync_service) to resurrect files a human
        deleted in Obsidian. Same projection write_note() does on capture."""
        await self._write_livesync_file(note, markdown)

    async def _write_livesync_file(self, note: AnyNote, markdown: str) -> None:
        """Write the chunk + entry pair a LiveSync client would have written
        for this file, so pulling clients materialize it in the vault. Both
        writes resurrect a deleted doc if one is in the way — that's what makes
        the DB→Obsidian rebuild able to restore files removed in Obsidian."""
        chunk_id = _chunk_id(markdown)
        # dedup=True: a live chunk with this exact content is fine to leave as
        # is (content-addressed); only resurrect it if it was deleted.
        await self._put_livesync(chunk_id, {"_id": chunk_id, "data": markdown, "type": "leaf"}, dedup=True)

        path = _vault_path(note)
        entry_id = path.lower()  # LiveSync keys entries by lowercased path
        entry: dict[str, Any] = {
            "_id": entry_id,
            "path": path,
            "children": [chunk_id],
            "ctime": int(note.created.timestamp() * 1000),
            "mtime": int(note.updated.timestamp() * 1000),
            "size": len(markdown.encode("utf-8")),
            "type": "plain",
            "eden": {},
        }
        await self._put_livesync(entry_id, entry, dedup=False)

    async def _put_livesync(self, doc_id: str, body: dict[str, Any], *, dedup: bool) -> None:
        """PUT a LiveSync doc, creating / overwriting / resurrecting as needed.

        On a 409 conflict the existing doc may be live, soft-deleted (LiveSync
        keeps the doc with a `deleted: true` field), or hard-deleted (a CouchDB
        `_deleted` tombstone). For a live doc with `dedup` we leave it alone;
        otherwise we take over its revision and rewrite — and because our body
        carries no `deleted`/`_deleted` flag, that rewrite resurrects it."""
        resp = await self._client.put(f"/{self._db}/{_q(doc_id)}", json=body)
        if resp.status_code in (201, 202):
            return
        if resp.status_code != 409:
            raise CouchDBError(f"failed to write {doc_id}: {resp.status_code} {resp.text}")

        existing = await self.get_document(doc_id)  # None only if hard-deleted
        if dedup and existing is not None and not existing.get("deleted"):
            return  # identical live content already present — nothing to do
        rev = existing.get("_rev") if existing else await self._deleted_leaf_rev(doc_id)
        if rev is None:
            raise CouchDBError(f"conflict writing {doc_id} but no revision found to take over")
        body = {**body, "_rev": rev}
        # Preserve original ctime on an entry we're resurrecting.
        if existing and existing.get("ctime") and "ctime" in body:
            body["ctime"] = existing["ctime"]
        resp = await self._client.put(f"/{self._db}/{_q(doc_id)}", json=body)
        if resp.status_code not in (201, 202):
            raise CouchDBError(f"failed to resurrect {doc_id}: {resp.status_code} {resp.text}")

    async def _deleted_leaf_rev(self, doc_id: str) -> str | None:
        """The revision of a hard-deleted doc's tombstone leaf, so we can write
        over it. `get_document` returns None for these (a plain GET 404s), so
        we ask for the deleted leaf explicitly via open_revs."""
        resp = await self._client.get(
            f"/{self._db}/{_q(doc_id)}",
            params={"open_revs": "all"},
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None
        for row in resp.json():
            ok = row.get("ok")
            if ok and ok.get("_rev"):
                return ok["_rev"]
        return None

    async def get_document(self, doc_id: str) -> dict | None:
        resp = await self._client.get(f"/{self._db}/{_q(doc_id)}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CouchDBError(f"failed to get {doc_id}: {resp.status_code} {resp.text}")
        return resp.json()

    async def find(
        self,
        selector: dict,
        limit: int = 20,
        sort: list[dict] | None = None,
        fields: list[str] | None = None,
    ) -> list[dict]:
        body: dict[str, Any] = {"selector": selector, "limit": limit}
        if sort:
            body["sort"] = sort
        if fields is not None:
            body["fields"] = fields
        resp = await self._client.post(f"/{self._db}/_find", json=body)
        if resp.status_code != 200:
            raise CouchDBError(f"_find failed: {resp.status_code} {resp.text}")
        return resp.json().get("docs", [])

    # --- reverse-sync (reconcile.py) --------------------------------------
    #
    # Everything below exists so a human's Obsidian edit can flow back into
    # the queryable JSON doc. The read path here is the mirror image of
    # write_note()'s LiveSync projection: instead of writing a chunk+entry, we
    # watch _changes for edited entries and reassemble the markdown from their
    # chunks. Nothing here writes to the LiveSync docs — the vault file stays
    # the human's to own; only the JSON doc is updated (in place, by _rev).

    async def update_document(self, doc: dict) -> None:
        """Overwrite an existing document. Requires `_id` and `_rev` — unlike
        write_note()'s append-only path, reverse-sync genuinely updates."""
        doc_id = doc["_id"]
        resp = await self._client.put(f"/{self._db}/{_q(doc_id)}", json=doc)
        if resp.status_code not in (201, 202):
            logger.error("couchdb update failed op=put_doc id=%s status=%s", doc_id, resp.status_code)
            raise CouchDBError(f"failed to update {doc_id}: {resp.status_code} {resp.text}")

    async def update_note(self, note: AnyNote, markdown: str, *, doc_id: str, rev: str) -> str:
        """Update an existing note in place (AI maintenance / manage-apply):
        overwrite the JSON doc at its existing `_id` (kept stable even if the
        edit changed name/date) and re-project the LiveSync file so Obsidian
        reflects the change. Unlike reverse-sync, this DOES rewrite the vault
        file — the edit originated on the app side, not in Obsidian."""
        doc: dict[str, Any] = note.model_dump(mode="json", exclude_none=True)
        doc["_id"] = doc_id
        doc["_rev"] = rev
        doc["markdown"] = markdown

        resp = await self._client.put(f"/{self._db}/{_q(doc_id)}", json=doc)
        if resp.status_code not in (201, 202):
            logger.error("couchdb update_note failed op=put_doc id=%s status=%s", doc_id, resp.status_code)
            raise CouchDBError(f"failed to update note {doc_id}: {resp.status_code} {resp.text}")

        try:
            await self._write_livesync_file(note, markdown)
        except Exception:
            logger.exception("LiveSync re-projection failed for %s (JSON doc updated fine)", doc_id)
        return doc_id

    async def delete_document(self, doc_id: str, rev: str) -> None:
        """Hard-delete a JSON record (record delete). The vault file is removed
        separately via soft_delete_vault_file so LiveSync propagates it."""
        resp = await self._client.delete(f"/{self._db}/{_q(doc_id)}", params={"rev": rev})
        if resp.status_code not in (200, 202):
            logger.error("couchdb delete failed op=delete_doc id=%s status=%s", doc_id, resp.status_code)
            raise CouchDBError(f"failed to delete {doc_id}: {resp.status_code} {resp.text}")

    async def soft_delete_vault_file(self, note: AnyNote) -> None:
        """Mark a note's LiveSync file entry deleted (LiveSync's own soft-delete
        convention), so Obsidian removes the file AND reverse-sync won't
        resurrect the record from a now-stale file. No-op if already gone."""
        entry_id = _vault_path(note).lower()
        existing = await self.get_document(entry_id)
        if not existing or existing.get("deleted"):
            return
        existing["deleted"] = True
        existing["mtime"] = int(time.time() * 1000)
        resp = await self._client.put(f"/{self._db}/{_q(entry_id)}", json=existing)
        if resp.status_code not in (201, 202):
            raise CouchDBError(f"failed to soft-delete entry {entry_id}: {resp.status_code} {resp.text}")

    async def current_update_seq(self) -> str:
        resp = await self._client.get(f"/{self._db}")
        if resp.status_code != 200:
            raise CouchDBError(f"failed to read db info: {resp.status_code} {resp.text}")
        return str(resp.json().get("update_seq"))

    async def changes_since(self, since: str, *, limit: int = 200) -> dict:
        """One page of the _changes feed with full docs inlined, so the
        reconciler can filter to vault entries without a GET per change."""
        resp = await self._client.get(
            f"/{self._db}/_changes",
            params={"since": since, "include_docs": "true", "limit": limit},
        )
        if resp.status_code != 200:
            raise CouchDBError(f"_changes failed: {resp.status_code} {resp.text}")
        return resp.json()

    # The reverse-sync cursor lives in a `_local/` doc: it never replicates to
    # LiveSync clients and never shows up in _changes, so it can't feed back
    # into the very feed it bookmarks.
    _RECONCILE_LOCAL = "_local/taster_reconcile"

    async def load_reconcile_state(self) -> dict:
        resp = await self._client.get(f"/{self._db}/{self._RECONCILE_LOCAL}")
        if resp.status_code == 404:
            return {}
        if resp.status_code != 200:
            raise CouchDBError(f"failed to read reconcile state: {resp.status_code} {resp.text}")
        return resp.json()

    async def save_reconcile_state(self, seq: str, rev: str | None = None) -> str:
        body: dict[str, Any] = {"seq": seq}
        if rev:
            body["_rev"] = rev
        resp = await self._client.put(f"/{self._db}/{self._RECONCILE_LOCAL}", json=body)
        if resp.status_code not in (201, 202):
            raise CouchDBError(f"failed to save reconcile state: {resp.status_code} {resp.text}")
        return resp.json().get("rev", "")
