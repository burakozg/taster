"""The vision step: read a photo into text, then let a text model do the rest.

A vision model earns its place on exactly one job — seeing. Everything after
that (deciding what the product is, searching the web, writing strict JSON,
answering a question over the vault) is reasoning and tool work, and the vision
model was visibly the wrong instrument for it: asked a narrow follow-up it
answered with ~15 tokens, and the maintenance planner narrated a plan instead of
producing one. So a photo is first transcribed here — what it shows and the text
printed on it, verbatim, with no interpretation — and the model chosen for the
actual task works from that text.

Returns None when the photo yields too little text to stand in for the image
(a plain bottle with no legible label, say) or the read fails; the caller then
falls back to handing the image straight to the vision model, as before.
"""
from __future__ import annotations

import logging

from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.providers import provider_for

logger = logging.getLogger("worker.image")

MIN_TEXT_CHARS = 10

_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "one sentence on what the photo shows (a whisky bottle, a cigar band, a screenshot of a message…)"},
        "printed_text": {"type": "string", "description": "ALL legible text in the photo, exactly as printed, line by line — no interpretation, no correction"},
    },
    "required": ["description", "printed_text"],
    "additionalProperties": False,
}
_PROMPT = """\
You read photos for a tasting log. Say what the photo shows, and transcribe \
every piece of legible text exactly as printed, line by line. Do not identify \
the product beyond what is printed, do not add facts you know, do not correct \
spelling or fill in missing words. If nothing is legible, leave printed_text empty.
"""


async def read_image(
    settings: Settings,
    db: CouchDBClient,
    *,
    job_id: str,
    model: str,
    image_b64: str,
    image_media_type: str | None,
) -> str | None:
    provider = provider_for(model)
    if provider is None:
        return None
    try:
        out = await provider.extract_structured(
            settings, db, job_id=f"{job_id}#vision", model=model,
            system_prompt=_PROMPT, text="Read this photo.",
            image_b64=image_b64, image_media_type=image_media_type,
            use_web_search=False, output_schema=_SCHEMA, site="vision",
            max_output_tokens=4096,
        )
    except Exception as e:  # noqa: BLE001 — falling back to the old path beats failing the capture
        logger.warning("image read failed for %s (%s); the vision model will do the whole job", job_id, e)
        return None
    description = str(out.get("description") or "").strip()
    printed = str(out.get("printed_text") or "").strip()
    if len(printed) < MIN_TEXT_CHARS:
        logger.info("image read for %s found %d chars of text; keeping the image in the loop", job_id, len(printed))
        return None
    logger.info("image read for %s: %d chars of text", job_id, len(printed))
    return f"Photo shows: {description}\nText printed on it (transcribed verbatim by a vision model):\n{printed}"
