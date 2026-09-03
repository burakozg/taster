"""Pydantic models for tasting notes and pairings — see tasting-log-design.md §5.

These are the enforcement layer: Claude's structured-output JSON is a nudge,
this validation is what actually guarantees a clean document ever reaches
CouchDB. Discriminated on `type`.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = 1

Status = Literal["tasted", "to-try"]
Source = Literal["photo", "chat"]

# Pairing sides: a pairing is ALWAYS cross-category — one COMPANION (a thing you
# savor: cigar, pipe, chocolate) with one DRINK (whisky, coffee, beer), never
# same-side (see PairingNote's validator and the capture prompt). This is the
# single source of truth for "what pairs with what"; it lives here (not in
# app.categories) so the PairingNote model can use it without a circular import,
# and app.categories re-exports it. To set a new category's pairing side, add it
# here alongside its model.
PAIR_GROUP_BY_TYPE = {
    "cigar": "companion",
    "pipe": "companion",
    "chocolate": "companion",
    "whisky": "drink",
    "coffee": "drink",
    "beer": "drink",
    "raki": "drink",
}

# Classical cocktails attach to an actual smoke, not to every companion — so the
# cocktail suggestion field is restricted to these types (chocolate, a companion
# for pairing purposes, does not carry cocktails).
_COCKTAIL_TYPES = ("cigar", "pipe")


# Turkish (and other) letters that don't survive NFKD on their own. Dotless ı
# has no combining-mark decomposition, so without this it is simply deleted:
# "Göbek" slugged to "g-bek" and "Rakı" to "rak" before this existed.
_TRANSLITERATE = str.maketrans({"ı": "i", "İ": "i", "ø": "o", "Ø": "o",
                                "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
                                "ß": "ss", "đ": "d", "Đ": "d", "ł": "l", "Ł": "l"})


def _slugify(text: str) -> str:
    """ASCII slug that keeps non-English letters as their closest ASCII form.

    Naively dropping everything outside [a-z0-9] mangles exactly the products
    this vault is full of — Turkish rakı, Scandinavian and Central European
    labels — and mangles them *collidingly*: every "Göbek" became "g-bek", so
    two different rakıs landed on one document id.
    """
    folded = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATE))
    ascii_text = folded.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug or "untitled"


def slug_tokens(text: str) -> set[str]:
    """Normalized word tokens, folded exactly as _slugify folds a slug.

    Sharing the folding matters for comparing two pieces of user text: Turkish
    casing is not a plain .lower() ("İ".lower() grows a combining dot), so
    naive tokenizing would make the same word fail to match itself.
    """
    return {t for t in _slugify(text).split("-") if t and t != "untitled"}


class PairingMatch(BaseModel):
    """One concrete vault item (from the OPPOSITE pairing group) that fits a
    suggestion's profile."""

    item: str | None = None  # vault note _id, when query_notes found a match
    name: str | None = None  # display name / fallback when no id resolved


class PairingSuggestion(BaseModel):
    """One cross-category pairing idea for an item note (§5): an ideal archetype
    (`profile`) plus 0-2 concrete inventory `matches` that fit it. `profile` is
    optional only for backward-compatibility with the old {item,name,reason}
    shape — new captures always set it."""

    profile: str = ""  # e.g. "a sherry-cask matured 10+ yo single malt"
    matches: list[PairingMatch] = Field(default_factory=list)
    reason: str = ""

    @field_validator("matches", mode="before")
    @classmethod
    def _clean_matches(cls, v):
        # Tolerate a model that emits a bare string match ("Glenfarclas 15")
        # instead of {item, name}, and drop anything that isn't a mapping —
        # a malformed match should never fail the whole capture.
        if not isinstance(v, list):
            return []
        out = []
        for m in v:
            if isinstance(m, dict):
                out.append(m)
            elif isinstance(m, str) and m.strip():
                out.append({"name": m.strip()})
        return out


class CocktailPairing(BaseModel):
    """A classical cocktail that pairs well with a smoke (cigar, pipe) — a
    general recommendation by NAME (Old Fashioned, Manhattan, Negroni, …) with a
    short why. Unlike PairingSuggestion.matches, a cocktail isn't vault
    inventory, so there's no `item` id — just the name and the reason it works.
    Restricted to cigar/pipe (see _COCKTAIL_TYPES); a cocktail accompanies a
    smoke, not a bar of chocolate."""

    name: str = ""
    reason: str = ""


class BaseNote(BaseModel):
    schema_version: int = SCHEMA_VERSION
    # Stable logical identity for the record, independent of the CouchDB `_id`
    # (which is derived from name/date and is therefore immutable-but-stale
    # after an Obsidian rename). The uid rides in the frontmatter, so it
    # round-trips through the vault file and lets reverse-sync (reconcile.py)
    # find the right record even when the name/date changed. Assigned at
    # capture when absent; older notes may have none (matched by `_id` then).
    uid: str | None = None
    status: Status
    name: str
    producer: str | None = None
    # Country of origin — mandatory on every item note (all non-pairing types);
    # "unknown" when it genuinely can't be determined. Captures always set it
    # (see the capture prompt); the AI maintenance flow can backfill it on
    # older records. Distinct from the finer-grained per-type location fields
    # (whisky.region, coffee.origin), which stay alongside it.
    country_of_origin: str = "unknown"
    # 1.0-5.0, one decimal of precision (slider in the PWA; chat captures
    # like "4 and a half stars" extract to 4.5).
    rating: float | None = Field(default=None, ge=1, le=5)
    date: date
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    price_sek: float | None = None
    # How many I currently have at home. 0 = none/unknown; drives the Search
    # "In stock" filter (stock > 0) and is editable in the detail modal or via
    # AI Maintain. Item-only concept — pairings aren't physical inventory, so
    # it lives on BaseNote, not PairingNote (same split as country_of_origin).
    stock: int = Field(default=0, ge=0)
    source: Source
    recommended_by: str | None = None  # only meaningful for status: to-try
    tags: list[str] = Field(default_factory=list)
    pairings_suggested: list[PairingSuggestion] = Field(default_factory=list)
    # Classical cocktail pairings — cigar/pipe only (see _COCKTAIL_TYPES). A
    # cocktail accompanies a smoke, so chocolate and the drinks never carry these
    # (a stray one is dropped by _cocktails_smoke_only below). Name + why only.
    cocktail_pairings: list[CocktailPairing] = Field(default_factory=list)
    notes: str = ""  # MY notes — the user's own words/impressions only
    # The other dimension: tasting profile from the vendor, reviews, or
    # common knowledge ("sherry-cask: dried fruit, walnut") — never the
    # user's own opinion.
    common_notes: str = ""

    @model_validator(mode="after")
    def _name_without_producer(self) -> "BaseNote":
        # `name` is the product/expression only ("Blue Series", "Double
        # Cask") — if the model repeated the producer in it, strip it.
        if self.producer and self.name.lower().startswith(self.producer.lower()):
            stripped = self.name[len(self.producer):].lstrip(" -–—:").strip()
            if stripped:
                self.name = stripped
        return self

    @field_validator("tags")
    @classmethod
    def _kebab_tags(cls, v: list[str]) -> list[str]:
        # Obsidian tags can't contain spaces — normalize to kebab-case and
        # drop duplicates/empties.
        out: list[str] = []
        for t in v:
            slug = _slugify(t)
            if slug != "untitled" and slug not in out:
                out.append(slug)
        return out

    @field_validator("rating", mode="before")
    @classmethod
    def _zero_rating_is_none(cls, v):
        # A recommendation (status: to-try) carries no rating, but models often
        # emit 0 ("unrated") instead of omitting the field — and 0 trips the
        # `ge=1` bound before the to-try rule below even runs, failing the whole
        # capture. Treat a zero/blank rating as "no rating" for EVERY category
        # (this is what broke adding a recommended pipe tobacco).
        if v in (None, 0, 0.0, "", "0"):
            return None
        return v

    @field_validator("pairings_suggested", mode="before")
    @classmethod
    def _clean_pairings_suggested(cls, v):
        # Keep only well-formed suggestion objects (a `profile` or `matches`);
        # drop stray strings/junk. A model occasionally floods this list with
        # unrelated field names — that must degrade to fewer suggestions, never
        # fail the whole tasting capture.
        if not isinstance(v, list):
            return []
        return [
            s for s in v
            if isinstance(s, dict) and (str(s.get("profile") or "").strip() or s.get("matches"))
        ]

    @field_validator("cocktail_pairings", mode="before")
    @classmethod
    def _clean_cocktail_pairings(cls, v):
        # Same defence for cocktails: keep only {name, ...} objects with a real
        # name and drop everything else. This is exactly the failure that broke
        # a pipe-tobacco capture — the model emitted the other schema field
        # names (blend_type, cut, components, …) as bare strings in here.
        if not isinstance(v, list):
            return []
        return [
            c for c in v
            if isinstance(c, dict) and str(c.get("name") or "").strip()
        ]

    @field_validator("rating")
    @classmethod
    def _round_rating(cls, v: float | None) -> float | None:
        # Keep stored precision to one decimal, whatever the source
        # (slider float artifacts, or a model emitting 4.25).
        return None if v is None else round(v, 1)

    @model_validator(mode="after")
    def _rating_only_when_tasted(self) -> "BaseNote":
        # §5: a rating only makes sense on an actual tasting; a to-try entry
        # is a recommendation and must not carry one.
        if self.status == "to-try" and self.rating is not None:
            raise ValueError("rating is only allowed when status is 'tasted'")
        return self

    @model_validator(mode="after")
    def _cocktails_smoke_only(self) -> "BaseNote":
        # A cocktail accompanies a smoke, so only cigar/pipe carry cocktail
        # pairings (chocolate is a companion for pairing, but not for cocktails).
        # Silently drop any that land elsewhere rather than failing the whole
        # note (keeps a slightly over-eager model or old edit from blocking it).
        if self.cocktail_pairings and self.item_type() not in _COCKTAIL_TYPES:
            self.cocktail_pairings = []
        return self

    def _slug(self) -> str:
        """Producer-qualified slug — the identifying half of doc_id().

        `name` is deliberately the expression ONLY, with the producer stripped
        out (see _name_without_producer), which makes bare names collide hard:
        "Göbek" is sold by every rakı producer, and "Blue Series" or "15 Year
        Old" are no better. Without the producer, two different bottles logged
        on one day are one document, and the second capture dies on a CouchDB
        409. The vault filename has always been "Producer - Name" for the same
        reason; this brings the id in line with it.
        """
        producer = (getattr(self, "producer", None) or "").strip()
        return _slugify(f"{producer} {self.name}" if producer else self.name)

    def doc_id(self) -> str:
        raise NotImplementedError

    def item_type(self) -> str:
        raise NotImplementedError


class WhiskyNote(BaseNote):
    type: Literal["whisky"] = "whisky"
    category: str | None = None  # single malt | blend | bourbon | ...
    region: str | None = None
    peated: bool | None = None
    cask: str | None = None
    age_years: int | None = None
    # Strength is printed on every whisky label and is the field that separates
    # a 40% supermarket blend from a cask-strength bottling — it belongs next to
    # the age, not only on beer/rakı (where `abv` started out, which is why
    # whisky captures used to lose it: the model emitted it and Pydantic, with
    # no such field here, silently dropped it).
    abv: float | None = None

    def doc_id(self) -> str:
        return f"whisky:{self._slug()}:{self.date.isoformat()}"

    def item_type(self) -> str:
        return "whisky"


class CigarNote(BaseNote):
    type: Literal["cigar"] = "cigar"
    wrapper: str | None = None
    vitola: str | None = None
    strength: str | None = None
    # Cigar country of origin now lives in the shared `country_of_origin`
    # (BaseNote) so it's uniform and mandatory across all item types.

    def doc_id(self) -> str:
        return f"cigar:{self._slug()}:{self.date.isoformat()}"

    def item_type(self) -> str:
        return "cigar"


class CoffeeNote(BaseNote):
    type: Literal["coffee"] = "coffee"
    roaster: str | None = None
    origin: str | None = None
    process: str | None = None
    roast_level: str | None = None
    # The rating is for the (bean, brew_method) couple: the same bean brewed a
    # different way is a SEPARATE entry with its own stars (a bean can shine as
    # espresso yet fall flat over a V60). brew_method is part of the id and the
    # vault filename so those entries coexist. espresso | v60 | drip |
    # french-press | aeropress | moka | cold-brew | ...
    brew_method: str | None = None

    # Espresso dial-in fields (§5) — the settled recipe for this bean, not
    # every test shot. Refined later via a chat re-capture or a manual Obsidian
    # edit. grinder/machine are captured from the entry when stated and default
    # to empty (None) otherwise — they're not assumed, so a pour-over or a bean
    # brewed on someone else's gear doesn't get a phantom espresso rig.
    # Free-text grind description — "medium-fine", "coarse", a number, or even
    # "metal filter" when the bean was ground elsewhere. Text, not a weight.
    # (Replaced the older grinder-specific `grind_setting`; Normalize folds any
    # legacy grind_setting value into this — see sync_service._LEGACY_FIELD_MOVES.)
    grind_size: str | None = None
    dose_g: float | None = None
    brew_time_s: float | None = None
    grinder: str | None = None
    machine: str | None = None

    def doc_id(self) -> str:
        # Fold the brew method in so (bean, method, date) is a distinct record;
        # a bean logged with no method keeps the original coffee:slug:date id.
        base = f"coffee:{self._slug()}"
        if self.brew_method:
            base += f":{_slugify(self.brew_method)}"
        return f"{base}:{self.date.isoformat()}"

    def item_type(self) -> str:
        return "coffee"


class PipeTobaccoNote(BaseNote):
    type: Literal["pipe"] = "pipe"
    blend_type: str | None = None   # english | virginia | va-per | aromatic | burley | ...
    cut: str | None = None          # ribbon | flake | plug | cake | shag | ...
    # The component leaf, e.g. ["virginia", "latakia", "perique"].
    components: list[str] = Field(default_factory=list)
    strength: str | None = None     # nicotine strength: mild..strong
    room_note: str | None = None    # the aroma others in the room perceive
    tin_date: str | None = None     # cellar/vintage marking, free-form (year or date)

    def doc_id(self) -> str:
        return f"pipe:{self._slug()}:{self.date.isoformat()}"

    def item_type(self) -> str:
        return "pipe"


class BeerNote(BaseNote):
    type: Literal["beer"] = "beer"
    style: str | None = None    # IPA | stout | pilsner | saison | lager | ...
    abv: float | None = None    # alcohol by volume, percent
    ibu: int | None = None      # bitterness (International Bitterness Units)
    serving: str | None = None  # bottle | can | draft | cask

    def doc_id(self) -> str:
        return f"beer:{self._slug()}:{self.date.isoformat()}"

    def item_type(self) -> str:
        return "beer"


class RakiNote(BaseNote):
    """Turkish rakı — an anise-flavoured grape spirit. A drink for pairing
    purposes (it goes with a companion), and like whisky it carries no
    cocktail suggestions: rakı is drunk neat or cut with water, not mixed."""

    type: Literal["raki"] = "raki"
    # What it's distilled from: fresh grape (yaş üzüm), dried grape/raisin,
    # fig (incir), mulberry (dut). The fresh-grape/raisin split is the main
    # quality axis, so it's the first thing to capture.
    raki_base: str | None = None
    # Aniseed character and, where stated, its provenance — Çeşme aniseed is
    # the prized one and producers put it on the label.
    anise: str | None = None
    distillations: int | None = None  # 1 | 2 | 3 ("üç kere damıtılmış")
    # abv/serving reuse the shared field names beer already established, so the
    # flat capture schema doesn't grow a near-duplicate key per category.
    abv: float | None = None          # typically 40-50%
    serving: str | None = None        # neat | with water | on ice

    def doc_id(self) -> str:
        return f"raki:{self._slug()}:{self.date.isoformat()}"

    def item_type(self) -> str:
        return "raki"


class ChocolateNote(BaseNote):
    type: Literal["chocolate"] = "chocolate"
    chocolate_type: str | None = None   # dark | milk | white | ruby
    cacao_percent: float | None = None  # % cacao, e.g. 70
    # Bean origin (single-origin region/country), distinct from country_of_origin
    # (where the bar is *made*) — same split as coffee's origin vs country.
    cacao_origin: str | None = None
    form: str | None = None             # bar | truffle | bonbon | drinking | ...

    def doc_id(self) -> str:
        return f"chocolate:{self._slug()}:{self.date.isoformat()}"

    def item_type(self) -> str:
        return "chocolate"


# Item models (carry country_of_origin/rating/stock). Adding an item category =
# define its model above and add it to this one tuple — the ItemNote union, the
# AnyNote union, and parse_any_note below all derive from it (PairingNote is
# folded in with _ALL_MODELS once it's defined), and app.categories reads the
# same models for the registry.
_ITEM_MODELS = (WhiskyNote, CigarNote, CoffeeNote, PipeTobaccoNote, BeerNote, ChocolateNote, RakiNote)

ItemNote = Annotated[Union[_ITEM_MODELS], Field(discriminator="type")]


class PairingNote(BaseModel):
    """A tried pairing — its own document, not an attribute of either item.

    See tasting-log-design.md §5: a pairing has its own date/rating and
    references two items at once, which doesn't fit cleanly as a field on
    either item's (append-only) note.
    """

    type: Literal["pairing"] = "pairing"
    schema_version: int = SCHEMA_VERSION
    uid: str | None = None  # stable logical id, see BaseNote.uid
    items: list[str] = Field(min_length=2, max_length=2)  # two note _ids
    rating: float | None = Field(default=None, ge=1, le=5)

    @field_validator("rating", mode="before")
    @classmethod
    def _zero_rating_is_none(cls, v):
        # Same as BaseNote: a 0/blank rating means "unrated", not an ge=1 error.
        if v in (None, 0, 0.0, "", "0"):
            return None
        return v

    @field_validator("rating")
    @classmethod
    def _round_rating(cls, v: float | None) -> float | None:
        return None if v is None else round(v, 1)
    date: date
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: Source
    tags: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _cross_category(self) -> "PairingNote":
        # A pairing is one companion + one drink, never same-side (e.g. two
        # cigars, or a cigar with chocolate). Item ids are `type:slug:date`, so
        # the type is the prefix. Only enforce when BOTH types resolve to a known
        # pairing group — unresolved/legacy ids pass rather than block a record.
        groups = [PAIR_GROUP_BY_TYPE.get(i.split(":")[0]) for i in self.items]
        known = [g for g in groups if g]
        if len(known) == 2 and known[0] == known[1]:
            raise ValueError(
                f"a pairing must be one companion and one drink, not two {known[0]} items"
            )
        return self

    def doc_id(self) -> str:
        a, b = sorted(_slugify(i.split(":")[1]) if ":" in i else _slugify(i) for i in self.items)
        return f"pairing:{a}+{b}:{self.date.isoformat()}"

    def item_type(self) -> str:
        return "pairing"


# Full set including pairings — defined here, after PairingNote exists.
_ALL_MODELS = (*_ITEM_MODELS, PairingNote)

AnyNote = Annotated[Union[_ALL_MODELS], Field(discriminator="type")]

# type discriminator value -> model, derived from the model tuple so it can
# never drift out of sync with the unions.
_MODEL_BY_TYPE = {m.model_fields["type"].default: m for m in _ALL_MODELS}


def parse_any_note(data: dict) -> AnyNote:
    """Validate a raw dict (from Claude's structured output) into a note model."""
    kind = data.get("type")
    model = _MODEL_BY_TYPE.get(kind)
    if model is None:
        raise ValueError(f"unknown note type: {kind!r}")
    return model.model_validate(data)
