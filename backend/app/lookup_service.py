"""Lookup: query_notes tool loop over the vault, natural-language answer.
Covers both text lookups and shop-mode (photo) lookups — see §4.5.

The tool loop itself lives in each provider module (openrouter_provider.py /
mistral_provider.py); this file just resolves the model to a provider and
hands off — see providers.py.
"""
from __future__ import annotations

import logging

from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.providers import provider_for

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You answer questions about a personal whisky/cigar/coffee/pipe-tobacco/beer/chocolate/rakı tasting vault \
using the query_notes tool. Never guess at vault contents — always query \
before answering a question that depends on prior tastings.

If the user provides a photo (shop mode: they're standing in front of a \
bottle deciding whether to buy it), identify the item from the image first, \
then query_notes by name/producer to check whether it's been tasted before \
and to surface similar highly-rated items in the same region/category.

query_notes is the source of truth for what the user owns and has tasted — \
never answer from web_search where query_notes can answer. Use web_search \
only for facts the vault cannot hold: identifying an unfamiliar label, or \
the character of something not tasted yet (typical in shop mode). Keep it to \
1-2 searches, and say which part of the answer came from the web rather than \
from their own notes.

For pairing questions ("what should I have with this?"), prefer tried \
pairings (query_notes with type: "pairing", filtering on the relevant item's \
id if known) over generic `pairings_suggested` entries on item notes — and \
say clearly which kind of answer you're giving ("you've actually tried..." \
vs "no tried pairing on record, but this note suggested...").

Keep answers short and conversational — this is a chat reply, not a report.
"""


class LookupResult:
    def __init__(self, answer: str) -> None:
        self.answer = answer


async def run_lookup(
    lookup_id: str,
    settings: Settings,
    db: CouchDBClient,
    *,
    question: str,
    image_b64: str | None = None,
    image_media_type: str | None = None,
    model_override: str | None = None,
) -> LookupResult:
    claude_cfg = settings.models.claude
    # Admin-panel choice (delivered per job by the relay) wins over the baked-in
    # config.yaml default. providers.py maps the resolved id to its
    # OpenRouter/Mistral provider module.
    model = model_override or claude_cfg.text_model
    provider = provider_for(model)
    if provider is None:
        # Only reachable via a stale admin-panel override or a hand-edited
        # config.yaml — see capture_service.run_capture's identical guard.
        return LookupResult(
            answer=f"{model!r} is not a recognised OpenRouter or Mistral model id "
                   f"— pick a model in Admin → Models, or fix image_model/text_model "
                   f"in config.yaml."
        )
    answer = await provider.answer_question(
        settings, db,
        job_id=lookup_id,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        question=question,
        image_b64=image_b64,
        image_media_type=image_media_type,
    )
    return LookupResult(answer=answer)
