"""Fetch missing details for one item: one worker per field, then an orchestrator.

Asking one model call for "everything about this product" is how a hollow or
invented field gets through — it fills the shape, not the facts. So every
field is its own small job:

  worker       one narrow web-search call per field. It must hand back a
               source URL and a VERBATIM quote that states the fact for this
               exact product, or say it found nothing. It never answers from
               memory.
  evidence     deterministic, no model. The orchestrator fetches the source
               page itself and checks the quote is really on it (and, for hard
               facts, that the value is in the quote), and that the value
               coerces to the field's type and survives the note schema.
  verifier     one model call over the whole set: same product and expression
               as the record? consistent with each other and with what is
               already stored? It can only flag a proposal, never change a
               value or lift a rejection.

Nothing is written here. The result is a list of PROPOSALS, each with its
source, quote and verdicts, for a person to accept — a wrong ABV or cask would
sit in the vault looking exactly as authoritative as a right one.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import re
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.model_output import ModelOutputError
from app.model_roles import effort_for_role, model_for_role
from app.providers import provider_for
from app.schema import parse_any_note
from app import history
from app.record_service import update_record
from app.sync_service import _fold_legacy

logger = logging.getLogger("worker.details")

# field -> how the value is typed and checked.
#   text/int/float/list : a hard fact; the value must appear in the quote.
#   summary             : prose or a judgement (common_notes, peated); the quote
#                         must be on the page, the verifier judges the rest.
_KINDS: dict[str, str] = {
    "common_notes": "summary", "peated": "summary",
    "age_years": "int", "distillations": "int", "ibu": "int",
    "abv": "float", "cacao_percent": "float",
    "components": "list",
}
# What each field means, so a worker searches for the right thing.
_HINTS: dict[str, str] = {
    "common_notes": "the established tasting profile of this product from the producer, a retailer or reviewers — flavours and character, in 1-3 plain sentences. Never a personal opinion.",
    "country_of_origin": "the country where it is made (for whisky: Scotland, Japan, Ireland, USA…)",
    "region": "the distilling region or appellation (e.g. Speyside, Islay)",
    "bottler": "the independent bottler, ONLY if this is not an official distillery bottling (e.g. Gordon & MacPhail, Signatory)",
    "category": "single malt, blend, bourbon, rye…",
    "peated": "whether the whisky is peated (true/false)",
    "cask": "the cask type or finish stated for this expression (e.g. Oloroso sherry)",
    "age_years": "the age statement in years as a whole number",
    "abv": "the bottling strength in percent ABV (number)",
    "wrapper": "the wrapper leaf",
    "vitola": "the vitola / size name",
    "strength": "the strength as stated (cigars: mild … full)",
    "roaster": "the roaster", "origin": "where the beans come from",
    "process": "the processing method (washed, natural, honey…)",
    "roast_level": "the roast level",
    "style": "the beer style", "ibu": "the bitterness in IBU (whole number)",
    "blend_type": "the blend type (english, virginia, aromatic…)",
    "cut": "the tobacco cut (ribbon, flake, plug…)",
    "components": "the tobacco components, as a comma-separated list",
    "chocolate_type": "dark, milk, white or ruby",
    "cacao_percent": "the cacao percentage (number)",
    "cacao_origin": "where the cacao comes from",
    "form": "bar, truffle, bonbon…",
    "raki_base": "the base spirit/ingredient", "anise": "the anise character or type",
    "distillations": "the number of distillations (whole number)",
}
# Per category: the facts that can be looked up. Personal fields (rating,
# notes, stock, price, brew settings…) are deliberately absent.
FETCHABLE: dict[str, tuple[str, ...]] = {
    "whisky": ("common_notes", "country_of_origin", "region", "bottler", "category", "peated", "cask", "age_years", "abv"),
    "cigar": ("common_notes", "country_of_origin", "wrapper", "vitola", "strength"),
    "coffee": ("common_notes", "country_of_origin", "roaster", "origin", "process", "roast_level"),
    "beer": ("common_notes", "country_of_origin", "style", "abv", "ibu"),
    "pipe": ("common_notes", "country_of_origin", "blend_type", "cut", "components", "strength"),
    "chocolate": ("common_notes", "country_of_origin", "chocolate_type", "cacao_percent", "cacao_origin", "form"),
    "raki": ("common_notes", "country_of_origin", "raki_base", "anise", "abv", "distillations"),
}
# Fields that are a reading of the record rather than a plain fact — `bottler`
# is absent on an official bottling, which is not "missing".
_OPTIONAL_BY_NATURE = frozenset({"bottler"})

_WORKER_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "value": {"type": "string", "description": "the value, as plain text (numbers as digits, true/false for yes/no)"},
        "source_url": {"type": "string", "description": "the exact page the quote was copied from"},
        "quote": {"type": "string", "description": "a VERBATIM fragment copied from that page that states the fact, max 300 characters"},
    },
    "required": ["found"],
    "additionalProperties": False,
}
_WORKER_PROMPT = """\
You look up ONE fact about ONE product for a personal tasting log, using web \
search. Find a page that states the fact for THIS exact product — the same \
producer, name, age statement and edition — not a sibling expression, another \
age, or the brand in general. Prefer the producer's own page, then established \
retailers and reviewers.

Return found=true only with all three of: the `value`, the `source_url` of the \
page you actually read, and a `quote` copied word for word from that page that \
states it. Otherwise return found=false and nothing else. Never answer from \
memory, never paraphrase the quote, never guess. A near miss (a different age \
or edition) is found=false.
"""

_VERIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "identity_ok": {"type": "boolean", "description": "do the quotes describe the SAME product and edition as the record?"},
        "identity_note": {"type": "string"},
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["ok", "doubtful", "conflict"]},
                    "reason": {"type": "string"},
                },
                "required": ["field", "verdict"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["identity_ok", "verdicts"],
    "additionalProperties": False,
}
_VERIFIER_PROMPT = """\
You review facts proposed for one entry in a personal tasting log. For each \
proposal you get the field, the proposed value, the record's current value, a \
quote from a web page and the page's domain. Judge ONLY:

- Does the quote describe the SAME product and edition as the record (producer, \
name, age statement, edition)? Set identity_ok=false if the quotes are about a \
different expression, age or the brand in general.
- Is each value consistent with its quote, with the other proposals and with \
the existing record? A field is `ok`, `doubtful` (weak, generic or ambiguous \
evidence) or `conflict` (contradicts the record or another proposal).

You cannot change values or add new ones. Give a short reason for anything \
that is not `ok`.
"""

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_MAX_PAGE_BYTES = 1_500_000
_WORKER_CONCURRENCY = 3


def is_empty(field: str, value: Any) -> bool:
    if value is None or value == "" or value == []:
        return True
    return field == "country_of_origin" and value == "unknown"


def fetchable_fields(item_type: str) -> tuple[str, ...]:
    return FETCHABLE.get(item_type, ())


def _norm(text: str) -> str:
    """Case-folded, punctuation-free, whitespace-collapsed — the comparison
    form for quotes, tolerant of curly quotes, dashes and layout."""
    return " ".join("".join(c if c.isalnum() else " " for c in text.casefold()).split())


def _coerce(field: str, raw: Any) -> Any:
    """The worker's text value as the field's real type; ValueError if it can't be."""
    kind = _KINDS.get(field, "text")
    s = str(raw).strip()
    if not s:
        raise ValueError("empty value")
    if field == "peated":
        low = s.casefold()
        if low in ("true", "yes", "peated"):
            return True
        if low in ("false", "no", "unpeated", "non-peated"):
            return False
        raise ValueError(f"not a yes/no: {s!r}")
    if kind in ("int", "float"):
        m = re.search(r"\d+(?:[.,]\d+)?", s)
        if not m:
            raise ValueError(f"no number in {s!r}")
        num = float(m.group().replace(",", "."))
        if kind == "int":
            if num != int(num):
                raise ValueError(f"{s!r} is not a whole number")
            return int(num)
        return round(num, 1)
    if kind == "list":
        parts = [p.strip() for p in re.split(r"[,;/]", s) if p.strip()]
        if not parts:
            raise ValueError("empty list")
        return parts
    return s


def _value_in_quote(field: str, value: Any, quote: str) -> bool:
    """Hard facts must literally appear in the quote that is supposed to prove them."""
    kind = _KINDS.get(field, "text")
    if kind == "summary":
        return True
    q = _norm(quote)
    if kind in ("int", "float"):
        nums = {m.replace(",", ".") for m in re.findall(r"\d+(?:[.,]\d+)?", quote)}
        want = f"{value:g}" if isinstance(value, float) else str(value)
        return want in nums or any(abs(float(n) - float(value)) < 1e-9 for n in nums)
    parts = value if isinstance(value, list) else [value]
    return all(_norm(str(p)) in q for p in parts)


# ---- fetching a source page, safely ----

def _public_host(host: str) -> bool:
    """Refuse hosts that resolve to private, loopback, link-local or reserved
    addresses — the URL comes from a model, so it must not be a way to poke at
    the NAS or the other containers beside this one."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return bool(infos)


async def _page_text(client: httpx.AsyncClient, url: str) -> str | None:
    """The visible text of a page, or None if it can't be fetched."""
    for _ in range(4):  # follow a few redirects by hand, re-checking each hop
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if not await asyncio.to_thread(_public_host, parsed.hostname):
            return None
        try:
            async with client.stream("GET", url, headers={"User-Agent": _UA, "Accept": "text/html,*/*;q=0.5"}) as r:
                if r.is_redirect and (loc := r.headers.get("location")):
                    url = urljoin(url, loc)
                    continue
                if r.status_code != 200:
                    return None
                ctype = r.headers.get("content-type", "")
                if "html" not in ctype and "text" not in ctype and "json" not in ctype:
                    return None
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > _MAX_PAGE_BYTES:
                        break
        except httpx.HTTPError:
            return None
        raw = body.decode(r.encoding or "utf-8", errors="replace")
        raw = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
        return html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return None


def _quote_on_page(quote: str, page: str) -> bool:
    """Every substantial fragment of the quote (split at ellipses) must be on the page."""
    page_n = _norm(page)
    fragments = [f for f in re.split(r"…|\.\.\.", quote) if len(_norm(f)) >= 12]
    return bool(fragments) and all(_norm(f) in page_n for f in fragments)


# ---- the workers ----

def _identity(item: dict[str, Any]) -> dict[str, Any]:
    keep = ("type", "producer", "name", "country_of_origin", "region", "bottler", "category",
            "cask", "age_years", "abv", "style", "origin", "roaster", "vitola", "wrapper")
    return {k: item[k] for k in keep if not is_empty(k, item.get(k))}


async def _work_one(settings: Settings, db: CouchDBClient, model: str, job_id: str,
                    item: dict[str, Any], field: str) -> dict[str, Any]:
    provider = provider_for(model)
    if provider is None:
        raise ModelOutputError(f"{model!r} is not a recognised model id")
    ask = (
        f"Product: {json.dumps(_identity(item), ensure_ascii=False)}\n\n"
        f"Field to find — `{field}`: {_HINTS.get(field, field)}"
    )
    out = await provider.extract_structured(
        settings, db, job_id=f"{job_id}#{field}", model=model,
        system_prompt=_WORKER_PROMPT, text=ask,
        image_b64=None, image_media_type=None,
        use_web_search=True, output_schema=_WORKER_SCHEMA, site="details",
        web_search_max_uses=3, effort=effort_for_role(settings, "research"),
    )
    return out


def _check_schema(item: dict[str, Any], field: str, value: Any) -> str | None:
    """None if the note still validates with this value, else why not."""
    data = _fold_legacy({k: v for k, v in item.items() if k not in ("_rev", "markdown")})
    data[field] = value
    try:
        parse_any_note(data)
    except Exception as e:  # noqa: BLE001
        return str(e).splitlines()[0][:160] if str(e) else type(e).__name__
    return None


async def fetch_details(
    job_id: str,
    settings: Settings,
    db: CouchDBClient,
    *,
    doc_id: str,
    fields: list[str] | None = None,
    overwrite: bool = False,
    model_override: str | None = None,
    reviewer_model: str | None = None,
) -> dict[str, Any]:
    """Run the workers for one item and return verified PROPOSALS (nothing is written).

    By default only fields that are empty on the record are looked up;
    `overwrite=True` also re-checks filled ones, and a proposal that differs from
    the stored value is returned unticked for a person to decide."""
    # Workers are many small search-and-quote lookups: the RESEARCH role. The
    # reviewer is one judgement over their output: the REASONING role — a separate
    # model by default only if configured, but separable, because a model grading
    # its own searches shares their blind spots.
    model = model_override or model_for_role(settings, "research")
    review_model = reviewer_model or model_for_role(settings, "reasoning")
    item = await db.get_document(doc_id)
    if item is None:
        raise ValueError(f"record not found: {doc_id}")
    allowed = fetchable_fields(item.get("type", ""))
    if not allowed:
        raise ValueError(f"nothing to look up for type {item.get('type')!r}")
    # Asked for by name, a field is looked up if it is empty (or overwrite is on).
    # With no list, every fetchable field that is empty — except ones whose
    # absence is meaningful, like `bottler` on an official bottling.
    candidates = [f for f in (fields or allowed) if f in allowed]
    wanted = candidates if fields else [f for f in candidates if f not in _OPTIONAL_BY_NATURE]
    todo = [f for f in wanted if overwrite or is_empty(f, item.get(f))]
    name = " — ".join(x for x in (item.get("producer"), item.get("name")) if x) or doc_id
    result: dict[str, Any] = {"doc_id": doc_id, "name": name, "proposals": [], "skipped": [],
                              "identity_ok": None, "identity_note": "", "verifier": "skipped"}
    result["skipped"] = [f for f in wanted if f not in todo]
    if not todo:
        return result

    sem = asyncio.Semaphore(_WORKER_CONCURRENCY)

    async def run(field: str) -> tuple[str, dict[str, Any] | None, str | None]:
        async with sem:
            try:
                return field, await _work_one(settings, db, model, job_id, item, field), None
            except Exception as e:  # noqa: BLE001 — one field failing must not sink the item
                logger.warning("details %s: worker %s failed: %s", job_id, field, e)
                return field, None, str(e) or type(e).__name__

    outs = await asyncio.gather(*(run(f) for f in todo))

    proposals: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), follow_redirects=False) as http:
        for field, out, err in outs:
            p: dict[str, Any] = {"field": field, "current": item.get(field), "value": None,
                                 "source_url": "", "quote": "", "evidence": "", "reason": "", "verdict": ""}
            if err:
                p.update(evidence="error", reason=f"lookup failed: {err}")
            elif not out or not out.get("found"):
                p.update(evidence="notfound", reason="no source found for this exact product")
            else:
                p.update(source_url=str(out.get("source_url") or ""), quote=str(out.get("quote") or "").strip())
                try:
                    p["value"] = _coerce(field, out.get("value"))
                except ValueError as e:
                    p.update(evidence="rejected", reason=str(e))
                else:
                    if not p["source_url"] or not p["quote"]:
                        p.update(evidence="rejected", reason="no source URL or quote given")
                    elif not _value_in_quote(field, p["value"], p["quote"]):
                        p.update(evidence="rejected", reason="the value is not in its own quote")
                    elif (why := _check_schema(item, field, p["value"])):
                        p.update(evidence="rejected", reason=f"fails the note schema: {why}")
                    else:
                        page = await _page_text(http, p["source_url"])
                        if page is None:
                            p.update(evidence="unverified", reason="the source page could not be fetched to check the quote")
                        elif _quote_on_page(p["quote"], page):
                            p["evidence"] = "verified"
                        else:
                            p.update(evidence="rejected", reason="the quote is not on the source page")
                if p["evidence"] in ("verified", "unverified") and not is_empty(field, p["current"]) and p["value"] == p["current"]:
                    p.update(evidence="unchanged", reason="already what the source says")
            proposals.append(p)

    # The orchestrator's judgement over what survived the evidence check.
    live = [p for p in proposals if p["evidence"] in ("verified", "unverified")]
    if live:
        try:
            provider = provider_for(review_model)
            review = await provider.extract_structured(
                settings, db, job_id=f"{job_id}#verify", model=review_model,
                system_prompt=_VERIFIER_PROMPT,
                text=json.dumps({
                    "record": _identity(item),
                    "proposals": [{
                        "field": p["field"], "current": p["current"], "value": p["value"],
                        "quote": p["quote"], "domain": urlparse(p["source_url"]).hostname,
                    } for p in live],
                }, ensure_ascii=False),
                image_b64=None, image_media_type=None,
                use_web_search=False, output_schema=_VERIFIER_SCHEMA, site="details",
                effort=effort_for_role(settings, "reasoning"),
            )
            result["identity_ok"] = review.get("identity_ok")
            result["identity_note"] = str(review.get("identity_note") or "")
            by_field = {v.get("field"): v for v in review.get("verdicts") or []}
            for p in live:
                v = by_field.get(p["field"])
                p["verdict"] = (v or {}).get("verdict") or "unchecked"
                if v and v.get("reason"):
                    p["reason"] = str(v["reason"])
                if review.get("identity_ok") is False and p["verdict"] == "ok":
                    p["verdict"] = "doubtful"
            result["verifier"] = "done"
        except Exception as e:  # noqa: BLE001 — a missing review downgrades trust, it doesn't void the evidence
            logger.warning("details %s: verifier failed: %s", job_id, e)
            for p in live:
                p["verdict"] = "unchecked"
            result["verifier"] = f"failed: {str(e) or type(e).__name__}"

    # What a person should accept by default: evidence checked AND the
    # orchestrator happy AND it fills a gap rather than replacing a value.
    for p in proposals:
        p["recommended"] = (p["evidence"] == "verified" and p["verdict"] == "ok"
                            and is_empty(p["field"], p["current"]))
    result["proposals"] = proposals
    logger.info("details %s: %s — %d proposal(s), %d recommended",
                job_id, doc_id, len(proposals), sum(p["recommended"] for p in proposals))
    return result


async def apply_details(db: CouchDBClient, doc_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Write the proposals a person accepted, and keep what they replaced.

    The ONE way fetched details reach a record, whichever front end — the phone
    app (through a worker job) or the admin page — accepted them. Only lookup-able
    fields are allowed: this is not a back door to rating, notes or stock.
    Raises ValueError for a missing record or a disallowed field; a value the
    note schema rejects raises whatever `update_record` raises."""
    doc = await db.get_document(doc_id)
    if doc is None:
        raise ValueError(f"record not found: {doc_id}")
    allowed = set(fetchable_fields(doc.get("type", "")))
    bad = [k for k in fields if k not in allowed]
    if bad or not fields:
        raise ValueError(f"not a fetchable field: {', '.join(bad) or '(none given)'}")
    previous = {k: doc.get(k) for k in fields}
    await update_record(db, doc_id, fields)
    name = " — ".join(x for x in (doc.get("producer"), doc.get("name")) if x) or doc_id
    history.append([{
        "kind": "fields", "at": datetime.now(timezone.utc).isoformat(), "doc_id": doc_id,
        "name": name, "mode": "details", "previous": previous, "applied": fields,
    }])
    return {"ok": True, "doc_id": doc_id, "applied": list(fields), "previous": previous}
