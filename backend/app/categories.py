"""Single source of truth for the tasting-log categories.

Everything that needs "the list of note types" derives from CATEGORIES here:
the CouchDB `$in` selectors (items_query, sync, tools), the capture/manage
JSON-schema enums, and — via categories_metadata() served at /categories — the
PWA's group headings and edit forms.

Adding a category is: define its Pydantic model in schema.py (and add it to the
`_ITEM_MODELS` tuple there), then add one line to CATEGORIES below. The per-type
*fields* stay explicit Pydantic models (the enforcement layer); this registry
carries the *list* plus the presentation metadata the front end reads.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.schema import (
    PAIR_GROUP_BY_TYPE,
    BeerNote,
    ChocolateNote,
    CigarNote,
    CoffeeNote,
    PairingNote,
    PipeTobaccoNote,
    RakiNote,
    WhiskyNote,
)

# Re-exported from schema (its home, to avoid a circular import) so the registry
# stays the one place to read category facts. A category's pairing side —
# "companion" (cigar/pipe/chocolate) or "drink" (whisky/coffee/beer) — is set in
# schema.PAIR_GROUP_BY_TYPE; pairings are always cross-side. See PairingNote's
# validator.
pair_group = PAIR_GROUP_BY_TYPE.get

# Render-kind for the PWA edit form, keyed by field name (shared across
# categories; anything not listed renders as a plain text input).
FIELD_KINDS = {
    "rating": "number", "price_sek": "number", "age_years": "number",
    "dose_g": "number", "brew_time_s": "number", "cacao_percent": "number",
    "stock": "number", "abv": "number", "ibu": "number", "peated": "bool",
    "distillations": "number",
    "status": "status", "tags": "tags", "components": "tags",
    "notes": "textarea", "common_notes": "textarea",
}

# Fields common to every item category, in edit-form order. Per-category tuples
# below insert their bespoke fields between the shared head and tail.
_HEAD = ("name", "producer", "rating", "status", "stock", "country_of_origin")
_TAIL = ("price_sek", "recommended_by", "tags", "notes", "common_notes")


@dataclass(frozen=True)
class Category:
    type: str            # the `type` discriminator value
    label: str           # PWA group heading / display name
    model: type          # the Pydantic model class (schema.py)
    edit_fields: tuple[str, ...]  # PWA edit-form field order
    is_item: bool = True  # items carry country_of_origin/rating/stock; pairing does not
    # Obsidian sub-folder under Tastings/ for this type's files. Defaults to the
    # label; only set when the on-disk folder differs (whisky/coffee, for
    # historical reasons). A new category's folder = its label automatically.
    folder: str | None = None

    def vault_folder(self) -> str:
        return self.folder or self.label


CATEGORIES: tuple[Category, ...] = (
    Category("cigar", "Cigars", CigarNote,
             _HEAD + ("wrapper", "vitola", "strength") + _TAIL),
    Category("whisky", "Whiskies", WhiskyNote,
             _HEAD + ("region", "category", "peated", "cask", "age_years") + _TAIL,
             folder="Whisky"),
    Category("coffee", "Coffee Beans", CoffeeNote,
             _HEAD + ("brew_method", "origin", "roaster", "process", "roast_level",
                      "grind_size", "dose_g", "brew_time_s",
                      "grinder", "machine") + _TAIL,
             folder="Coffee"),
    Category("beer", "Beers", BeerNote,
             _HEAD + ("style", "abv", "ibu", "serving") + _TAIL),
    Category("pipe", "Pipe Tobacco", PipeTobaccoNote,
             _HEAD + ("blend_type", "cut", "components", "strength",
                      "room_note", "tin_date") + _TAIL),
    Category("chocolate", "Chocolate", ChocolateNote,
             _HEAD + ("chocolate_type", "cacao_percent", "cacao_origin", "form") + _TAIL),
    Category("raki", "Rakı", RakiNote,
             _HEAD + ("raki_base", "anise", "abv", "distillations", "serving") + _TAIL,
             folder="Raki"),  # ASCII folder name — the vault path stays keyboard-safe
    Category("pairing", "Pairings", PairingNote,
             ("rating", "tags", "notes"), is_item=False),
)

NOTE_TYPES: tuple[str, ...] = tuple(c.type for c in CATEGORIES)
ITEM_TYPES: tuple[str, ...] = tuple(c.type for c in CATEGORIES if c.is_item)
# type -> full vault folder path ("Tastings/Whisky", …). Single source for the
# note-to-file projection (couchdb_client) so a new category's folder is covered
# automatically.
VAULT_FOLDERS: dict[str, str] = {c.type: f"Tastings/{c.vault_folder()}" for c in CATEGORIES}
_BY_TYPE = {c.type: c for c in CATEGORIES}


def label_for(note_type: str) -> str:
    c = _BY_TYPE.get(note_type)
    return c.label if c else note_type


def item_types_phrase() -> str:
    """Human phrase for prompts, e.g. 'whisky, cigar, coffee, pipe'."""
    return ", ".join(c.type for c in CATEGORIES if c.is_item)


def categories_metadata() -> list[dict]:
    """PWA-facing registry: group order/labels + per-type edit-field specs (with
    render kinds). Pushed to the relay with each items snapshot and served at
    /categories, so a new category needs no front-end change."""
    return [
        {
            "type": c.type,
            "label": c.label,
            "is_item": c.is_item,
            "pair_group": pair_group(c.type),  # "companion" | "drink" | None
            "edit_fields": [{"key": k, "kind": FIELD_KINDS.get(k, "text")} for k in c.edit_fields],
        }
        for c in CATEGORIES
    ]
