"""Facts the user stated outright in their own words — parsed here, not inferred
by a model.

A chat capture arrives as one string with everything mixed together:

    "Glenlivet 18, little punchy, mainly floral. 3,6 stars"

Product identity ("Glenlivet 18") genuinely needs world knowledge, so the model
earns its keep there. A rating and an ABV do not: they are written down, in a
handful of shapes, and a regex reads them correctly every time while a model has
to be *asked* to and can decline.

The case that forced this: `3,6` is a decimal comma. To survive the round trip a
model must re-render it as `3.6` in JSON — and the ways that go wrong are all
silent. `3,6` is invalid JSON; `36` and `3` both parse fine and are both wrong;
and the models this app actually runs (Qwen3 VL, DeepSeek) are exactly the tier
that mangles it. One of them had already been observed zero-filling a rating it
was handed explicitly.

So the split is: whatever the user spelled out is read here and pinned onto the
note afterwards (capture_service.run_capture), and the model is left to do the
part that needs judgement. Nothing found means nothing pinned — the model's
answer stands, so this can only ever add certainty, never remove it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A number with an optional decimal part, written with either separator. The
# separator must sit BETWEEN digits: "3,6" is three-point-six, but "chocolate,
# 6 year old" is a list and must not read as 6.
_NUM = r"(?P<num>\d{1,3}(?:[.,]\d{1,2})?)"

# Ratings need an explicit cue. Without one, "Glenlivet 18" reads as an
# eighteen-star rating — which is why a bare number is never accepted here.
_RATING_PATTERNS = (
    # "3,6/5", "4 / 5", "4 out of 5"
    re.compile(_NUM + r"\s*(?:/|\s+out\s+of\s+)\s*5\b", re.I),
    # "3,6 stars", "4 star", "5★", "4 yıldız"
    re.compile(_NUM + r"\s*(?:\*+|★+|stars?\b|yildiz\b|yıldız\b)", re.I),
    # "rated 4.5", "rating: 3,6"
    re.compile(r"\b(?:rated|rating|score[ds]?)\s*[:=]?\s*" + _NUM, re.I),
)

# ABV is always a percentage, and the unit words only ever strengthen the match.
_ABV_PATTERNS = (
    # "abv 46", "ABV: 40%"
    re.compile(r"\babv\b\s*[:=]?\s*" + _NUM + r"\s*%?", re.I),
    # "40%", "4,4 %", "46 % vol" — but never "50% off", which is a price tag
    # and lands squarely inside the plausible ABV range.
    # The lookahead sits immediately after "%" and swallows the space itself:
    # written as `%\s*(?!off)` the engine simply backtracks `\s*` to zero and
    # matches anyway.
    re.compile(_NUM + r"\s*%(?!\s*off\b)\s*(?:vol\.?|abv)?", re.I),
)

# Ratings are 1-5 by the schema. ABV spans everything from a 0.5% near-beer to
# a 96% neutral spirit; outside that a percentage is something else entirely
# (a discount, a cacao content) and is better left alone than guessed at.
_RATING_RANGE = (1.0, 5.0)
_ABV_RANGE = (0.5, 96.0)


@dataclass(frozen=True)
class TextFacts:
    """What the user spelled out. `None` means "not stated", never "zero"."""

    rating: float | None = None
    abv: float | None = None


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "."))
    except ValueError:  # pragma: no cover — the pattern only matches digits
        return None


def _first_match(text: str, patterns, low: float, high: float) -> float | None:
    for pattern in patterns:
        for match in pattern.finditer(text):
            value = _to_float(match.group("num"))
            if value is not None and low <= value <= high:
                return round(value, 1)
    return None


def extract_facts(text: str | None) -> TextFacts:
    """Read the rating and ABV the user stated, if any.

    Patterns are tried in order and the first in-range hit wins, so an explicit
    "4/5" outranks a stray percentage. A value outside its plausible range is
    skipped rather than clamped: "50% off" is not a 50% spirit, and a silently
    clamped number is worse than no number, because it looks like data.
    """
    if not text or not text.strip():
        return TextFacts()
    return TextFacts(
        rating=_first_match(text, _RATING_PATTERNS, *_RATING_RANGE),
        abv=_first_match(text, _ABV_PATTERNS, *_ABV_RANGE),
    )
