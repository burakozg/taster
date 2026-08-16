"""The capture output schema, shared by all four provider paths.

Kept as one flat, permissive object (not a strict anyOf-per-type union) —
structured-output JSON schema support excludes numeric/string length
constraints and works best kept simple (see claude-api skill notes on
structured-output limitations). Pydantic validation in schema.py is the real
enforcement layer; this is just a strong nudge so the raw output is close to
valid on the first try.

How each path consumes it:

- **Claude** — as *prompt text* (capture_service.CLAUDE_SYSTEM_PROMPT), NOT as
  output_config.format. Flatness costs us the grammar path there: 47 optional
  properties against a limit of 24, and Anthropic rejects the request outright
  ("Schemas contains too many optional parameters", 400). Restoring the grammar
  would mean a per-note-type schema and a classify-then-extract two-call shape.
- **OpenAI** — as text.format json_schema with `strict: False`, which skips
  grammar compilation and so has no optional-parameter ceiling.
- **Mistral** — as response_format json_schema, also `strict: False`.
- **OpenRouter** (Qwen, Gemini, Grok, Moonshot, …) — same response_format
  json_schema shape as Mistral, also `strict: False`. Every model behind the
  router shares this one nudge, so a weak model on this path is exactly as
  prone to dropping non-required fields as Mistral was — see the `required`
  list below.

So the optional-property count is load-bearing on the Claude path only: keep
new fields optional freely, but don't assume a grammar is enforcing any of it.
"""

from app.categories import NOTE_TYPES

CAPTURE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(NOTE_TYPES)},
        "status": {"type": "string", "enum": ["tasted", "to-try"]},
        "name": {
            "type": "string",
            "description": "product/expression only, e.g. 'Blue Series', 'Double Cask' — must NOT repeat the producer",
        },
        "producer": {"type": "string", "description": "brand/distillery/roaster, e.g. 'My Father', 'Glen Scotia'"},
        "country_of_origin": {
            "type": "string",
            "description": "country the item is made in, e.g. 'Scotland', 'Nicaragua', 'Ethiopia', 'Belgium'. MANDATORY for every item type (whisky/cigar/coffee/pipe/beer) — infer from the producer/label or web_search; use 'unknown' only if genuinely undeterminable. Not applicable to pairings.",
        },
        "rating": {"type": "number", "description": "1-5, one decimal allowed (e.g. 4.3). ONLY for status=tasted; for a recommendation (status=to-try) OMIT this field entirely — never send 0."},
        "date": {"type": "string", "format": "date"},
        "price_sek": {"type": "number"},
        "source": {"type": "string", "enum": ["photo", "chat"]},
        "recommended_by": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "lowercase-kebab-case, no spaces; never the product or producer name"},
        "notes": {"type": "string", "description": "The USER'S OWN impressions, transcribed in their words — never web/vendor info (that is common_notes). MUST be empty if they did not describe the taste: a name, ABV, price or star rating is not a tasting note. Never invent impressions on their behalf."},
        "common_notes": {
            "type": "string",
            "description": "Tasting profile from the vendor, reviews, or common knowledge (e.g. sherry-cask character) — never the user's own opinion.",
        },
        "pairings_suggested": {
            "type": "array",
            "description": "cross-category pairing ideas: a companion (cigar/pipe/chocolate) pairs with a drink (whisky/coffee/beer) and a drink pairs with a companion — NEVER same-side (no cigar-with-chocolate, no whisky-with-beer).",
            "items": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "the ideal pairing archetype in a SPECIFIC, tightly-specified style, e.g. 'a sherry-cask matured 10+ yo single malt', 'a natural-process Ethiopian with berry acidity' — never a generic category like 'espresso' or 'a bourbon'",
                    },
                    "matches": {
                        "type": "array",
                        "description": "0-2 concrete items the user OWNS (from query_notes on the opposite pairing group) that fit the profile; empty if nothing in the vault fits",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item": {"type": "string", "description": "vault note _id from query_notes"},
                                "name": {"type": "string", "description": "the matched item's display name"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "reason": {"type": "string", "description": "why it complements this item's character"},
                },
                # profile + reason are the two the Pydantic model relies on for a
                # useful suggestion — keep the nudge and enforcement in agreement.
                "required": ["profile", "reason"],
                "additionalProperties": False,
            },
        },
        "cocktail_pairings": {
            "type": "array",
            "description": "CIGAR and PIPE only: 1-2 CLASSICAL cocktails that pair well, by name (Old Fashioned, Manhattan, Negroni, Sazerac, Whiskey Sour, Daiquiri, …). Leave empty/omit for chocolate and drink items.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "the classical cocktail's common name, e.g. 'Old Fashioned'"},
                    "reason": {"type": "string", "description": "why it complements this tobacco's character"},
                },
                "required": ["name", "reason"],
                "additionalProperties": False,
            },
        },
        # whisky
        "category": {"type": "string"},
        "region": {"type": "string"},
        "peated": {"type": "boolean"},
        "cask": {"type": "string"},
        "age_years": {"type": "integer"},
        # cigar
        "wrapper": {"type": "string"},
        "vitola": {"type": "string"},
        "strength": {"type": "string"},
        # cigar country of origin is the shared `country_of_origin` above
        # coffee
        "roaster": {"type": "string"},
        "origin": {"type": "string"},
        "process": {"type": "string"},
        "roast_level": {"type": "string"},
        "brew_method": {"type": "string", "description": "how it was brewed for THIS rating, e.g. espresso, V60, drip, French press, AeroPress, moka, cold brew — the rating is for the (bean, method) couple"},
        "grind_size": {"type": "string", "description": "free-text grind description, e.g. 'medium-fine', 'coarse', a number, or 'metal filter' if ground elsewhere — text, never a weight/number-only value"},
        "dose_g": {"type": "number"},
        "brew_time_s": {"type": "number"},
        "grinder": {"type": "string", "description": "the grinder used, ONLY if the user states it (e.g. 'Baratza ESP', 'Comandante') — never assume a default"},
        "machine": {"type": "string", "description": "the espresso machine / brewer, ONLY if the user states it (e.g. 'La Pavoni', 'Gaggia Classic') — never assume a default"},
        # pipe tobacco
        "blend_type": {"type": "string", "description": "e.g. english, virginia, va-per, aromatic, burley"},
        "cut": {"type": "string", "description": "e.g. ribbon, flake, plug, cake, shag"},
        "components": {"type": "array", "items": {"type": "string"}, "description": "component leaf, e.g. ['virginia','latakia','perique']"},
        "room_note": {"type": "string", "description": "the aroma others perceive"},
        "tin_date": {"type": "string", "description": "cellar/vintage marking if shown"},
        # 'strength' (nicotine) is shared with cigars, defined above
        # beer
        "style": {"type": "string", "description": "e.g. IPA, stout, pilsner, saison, lager"},
        "abv": {"type": "number", "description": "alcohol by volume, percent"},
        "ibu": {"type": "integer", "description": "bitterness, International Bitterness Units"},
        "serving": {"type": "string", "description": "bottle | can | draft | cask"},
        # raki (abv + serving are shared with beer, above/below)
        "raki_base": {"type": "string", "description": "what the rakı is distilled from: 'fresh grape' (yaş üzüm), 'dried grape'/raisin, 'fig' (incir), 'mulberry' (dut)"},
        "anise": {"type": "string", "description": "aniseed character and provenance when stated, e.g. 'Çeşme aniseed'"},
        "distillations": {"type": "integer", "description": "how many times distilled (1-3); 'üç kere damıtılmış' means 3"},
        # chocolate
        "chocolate_type": {"type": "string", "description": "dark | milk | white | ruby"},
        "cacao_percent": {"type": "number", "description": "percent cacao, e.g. 70"},
        "cacao_origin": {"type": "string", "description": "bean origin / single-origin region, e.g. 'Madagascar', 'Ecuador' — distinct from country_of_origin (where the bar is made)"},
        "form": {"type": "string", "description": "bar | truffle | bonbon | drinking"},
        # pairing
        "items": {
            "type": "array",
            "items": {"type": "string"},
            "description": "exactly two vault note _ids, resolved via query_notes — only for type=pairing",
        },
    },
    # country_of_origin required so item captures always determine it (the
    # Pydantic default "unknown" is only a floor; pairings ignore the field).
    # name/status/date are true Pydantic-required fields on every item note
    # (schema.BaseNote) — without them here, a permissive `strict: False`
    # model (observed on Mistral) can drop them from its output entirely and
    # fail Pydantic validation on a clean, well-described capture. Listing
    # them here doesn't cost anything on the Claude path (this schema is only
    # ever pasted into the prompt as text there, never wired as an actual
    # output_config.format grammar — see the module docstring), and pairing
    # notes (which have neither field) just get name/status keys Pydantic
    # silently ignores as extras.
    "required": ["type", "source", "country_of_origin", "name", "status", "date"],
    "additionalProperties": False,
}
