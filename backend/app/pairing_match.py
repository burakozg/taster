"""Ground `pairings_suggested[*].matches` in Jev instead of a model's guess.

Two places propose pairing matches, and both share the same shape of problem:
capture_service.py's SYSTEM_PROMPT asks the capture model to call
`query_notes` and pick 0-2 `matches`, and manage_service.py's
run_repair_pairings_plan asks the same of a maintenance model regenerating
pairings for the whole vault. In both cases the pick is made inside the same
free-text completion that also writes the creative `profile`/`reason`, with
no confidence signal and nothing stopping a plausible-sounding but wrong
choice from being asserted as fact.

Jev's Choice primitive is built for exactly the part that's a real decision
rather than a creative one: given a fixed set of real candidates, which one
(if any) actually fits. `choose_matches` re-runs that one decision,
deterministically, against whatever candidates `query_notes` finds right now
— independent of whether or which candidates a model's own tool call saw.
The two thin wrappers below adapt it to each call site's own data shape.

Grounding REPLACES `matches` outright rather than merging with a model's own
guess: the `profile`/`reason` stand — that's still generative work — but the
concrete match is now entirely a typed decision over real inventory, with a
probability per candidate instead of an assertion.

Optional end to end, matching this app's "failure is never fatal" rule for
enrichment (see capture_service's module docstring on web_search): with no
TYPESAFE_API_KEY configured, no real candidates, or any Jev API failure,
every entry point here leaves `matches` exactly as its caller already had it.
"""
from __future__ import annotations

import logging
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient, Choice, TypeSafeError

from app.categories import opposite_pair_group
from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.schema import AnyNote, PairingMatch
from app.tools import query_notes_impl

logger = logging.getLogger(__name__)

#: Below this probability a candidate isn't a real match, just the
#: least-bad option in a distribution with nothing good in it — matches
#: PairingSuggestion's own "leaving matches empty is only valid when nothing
#: fits" rule (capture_service.SYSTEM_PROMPT).
MIN_MATCH_PROBABILITY = 0.2
MAX_MATCHES = 2
#: query_notes is real-vault-scale ("hundreds of docs" — tools.py), well
#: under Choice's 255-option ceiling, but capped anyway so one item's
#: pairing check can't become a 200-option question.
CANDIDATE_LIMIT = 40

#: BaseNote fields that are bookkeeping (ids, timestamps, stock/price) or
#: would be circular (the suggestions being graded) rather than describing
#: the item's own tasting/identity — excluded from the capture-path `state`.
_EXCLUDE_FROM_STATE = frozenset({
    "pairings_suggested", "cocktail_pairings", "uid", "created", "updated",
    "source", "stock", "price_sek",
})


async def choose_matches(
    db: CouchDBClient,
    settings: Settings,
    item_type: str,
    state: dict[str, Any],
    profiles: list[str],
) -> list[list[dict[str, str | None]]] | None:
    """One Jev call grounding every profile in `profiles` (in order) against
    real opposite-side inventory. Each result is a list of 0-2
    `{"item": id, "name": name}` dicts — schema.PairingMatch's own shape, as
    plain dicts so both call sites below can use them without extra
    conversion.

    Returns `None` when Jev did not run at all — not configured, nothing on
    the opposite side to choose from, or the API call itself failed. Callers
    must treat `None` as "keep what I already had", not as "grounded to
    nothing"; the distinction from an empty list matters because an empty
    list IS a real, deliberate ground truth (no candidate fit).
    """
    if not settings.typesafe_api_key or not profiles:
        return None
    group = opposite_pair_group(item_type)
    if group is None:
        return None

    candidates = await query_notes_impl(db, {"pair_group": group, "limit": CANDIDATE_LIMIT})
    by_id = {c["_id"]: c for c in candidates if c.get("_id")}
    if not by_id:
        return None  # nothing real to choose between — leave the caller's guess

    criteria = {item_id: _describe(doc) for item_id, doc in by_id.items()}
    questions = {
        f"q{i}": Choice(
            instructions=(
                f"Given the item described in state, which of these best fits "
                f"this ideal pairing profile: {profile!r}? Only choose one if "
                f"it genuinely fits — a mediocre fit among bad options is "
                f"still a bad match."
            ),
            criteria=criteria,
        )
        for i, profile in enumerate(profiles)
    }

    logger.info(
        "pairing_match: grounding %d profile(s) against %d candidate(s)",
        len(profiles), len(by_id),
    )
    try:
        client = AsyncTypeSafeClient(api_key=settings.typesafe_api_key)
        response = await client.system_one(state=state, questions=questions)
    except TypeSafeError as e:
        logger.warning("pairing_match: Jev call failed, keeping the existing matches: %s", e)
        return None

    out: list[list[dict[str, str | None]]] = []
    for i in range(len(profiles)):
        answer = response.choices.get(f"q{i}")
        if answer is None:
            out.append([])
            continue
        ranked = sorted(answer.probabilities.items(), key=lambda kv: -kv[1])
        out.append([
            {"item": item_id, "name": by_id[item_id].get("name")}
            for item_id, probability in ranked[:MAX_MATCHES]
            if probability >= MIN_MATCH_PROBABILITY
        ])
    logger.info(
        "pairing_match: grounded, matched=%d of %d profile(s)",
        sum(1 for m in out if m), len(out),
    )
    return out


async def ground_pairing_matches(
    db: CouchDBClient, settings: Settings, note: AnyNote,
) -> None:
    """Capture path: mutate `note.pairings_suggested[*].matches` in place."""
    suggestions = getattr(note, "pairings_suggested", None)
    if not suggestions:
        return
    indexed = [(i, s.profile) for i, s in enumerate(suggestions) if s.profile]
    if not indexed:
        return

    state = note.model_dump(mode="json", exclude=_EXCLUDE_FROM_STATE, exclude_none=True)
    results = await choose_matches(
        db, settings, note.item_type(), state, [profile for _, profile in indexed]
    )
    if results is None:
        return
    for (i, _), matches in zip(indexed, results, strict=True):
        suggestions[i].matches = [PairingMatch(**m) for m in matches]


async def ground_repair_changes(
    db: CouchDBClient,
    settings: Settings,
    changes: list[dict[str, Any]],
    items_by_id: dict[str, dict[str, Any]],
) -> None:
    """Regenerate-pairings path: mutate each change's `pairings[*].matches`
    in place — one Jev call per item (its own `state`, its own 1-2
    suggestions), same as the capture path but over a proposed plan instead
    of a freshly-captured note.

    `items_by_id` is the full-inventory compact record set
    (manage_service._repair_compact, keyed by `_id`) the repair prompt itself
    was built from — `changes` only echoes `doc_id`/`name`/`type`/`pairings`,
    not the item's own descriptive fields, so `state` has to come from there.
    A change with no matching record, or no `type`, is left untouched.
    """
    if not settings.typesafe_api_key:
        return
    # One Jev call per item, sequential and silent inside choose_matches
    # otherwise — a batch of several items grounding one at a time with
    # nothing logged in between is indistinguishable from stuck. This is the
    # only caller that loops over multiple items per invocation, so it is the
    # one that needs its own per-item line.
    grounded = skipped = 0
    for n, change in enumerate(changes, 1):
        pairings = change.get("pairings") or []
        indexed = [(i, p["profile"]) for i, p in enumerate(pairings) if p.get("profile")]
        if not indexed:
            continue
        record = items_by_id.get(change.get("doc_id") or "")
        # `type` is not in REPAIR_PAIRINGS_SCHEMA's `required` list, so a
        # model under token pressure can drop it — fall back to the record we
        # already have (reliable; it's the vault's own data, not the model's
        # echo of it) rather than skipping an item just because the model
        # left an optional field out.
        item_type = change.get("type") or (record.get("type") if record else None)
        if not item_type or record is None:
            skipped += 1
            logger.warning(
                "pairing_match: skipping doc_id=%s — %s",
                change.get("doc_id"),
                "no matching record" if record is None else "no type on record or change",
            )
            continue

        state = {k: v for k, v in record.items() if k != "_id"}
        try:
            results = await choose_matches(
                db, settings, item_type, state, [profile for _, profile in indexed]
            )
        except Exception as e:  # noqa: BLE001 — one item's grounding must not sink the batch
            logger.warning(
                "pairing_match: grounding failed for doc_id=%s: %s", change.get("doc_id"), e
            )
            continue
        if results is None:
            continue
        for (i, _), matches in zip(indexed, results, strict=True):
            pairings[i]["matches"] = matches
        grounded += 1
        logger.info(
            "pairing_match: repair batch %d/%d grounded doc_id=%s",
            n, len(changes), change.get("doc_id"),
        )
    # Unconditional, not `if grounded:` — a batch that grounds nothing is
    # exactly the failure mode this logging exists to catch, so it needs to
    # be visible too, not just the success case.
    logger.info(
        "pairing_match: repair batch done, grounded %d/%d item(s), skipped %d",
        grounded, len(changes), skipped,
    )


def _describe(doc: dict[str, Any]) -> str:
    """A short, structured description for one Choice `criteria` entry —
    a phrase Jev reasons over, not a full rendered note."""
    label = " ".join(p for p in (doc.get("producer"), doc.get("name")) if p) or "(unnamed)"
    bits: list[str] = []
    if region := doc.get("region"):
        bits.append(str(region))
    elif (origin := doc.get("country_of_origin")) and origin != "unknown":
        bits.append(str(origin))
    if (rating := doc.get("rating")) is not None:
        bits.append(f"rated {rating}/5")
    if common := doc.get("common_notes"):
        bits.append(str(common))
    return f"{label} — {'; '.join(bits)}" if bits else label
