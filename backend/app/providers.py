"""Model id → provider module, in one place.

The four call sites that run a model (capture, lookup, the maintenance plan,
regenerate-pairings) all need the same decision, and each used to spell it out
inline:

    provider = openrouter_provider if openrouter_provider.is_openrouter_model(model) else mistral_provider

which is already awkward at two alternates and gets worse with every one added
— four copies of a nested ternary, each an opportunity to order the checks
differently from its siblings. So the routing table lives here instead, and the
call sites ask one question: which provider module handles this id?

Only two providers remain: OpenRouter (every namespaced `vendor/model` id —
the open-weight models this app standardises on) and Mistral (its own
mistral-*/ministral-*/pixtral-* ids). Anthropic and OpenAI direct-API paths
were removed entirely — this app no longer holds either credential or calls
either API — so unlike the old table, `None` here just means "not a
recognised id" rather than "fall through to a third, unlisted provider".
Call sites treat a None return as a configuration error to surface clearly,
not a valid third branch.
"""
from __future__ import annotations

from types import ModuleType

from app import mistral_provider, openrouter_provider

# Order matters in principle (an id namespaced `vendor/model` can only ever be
# an OpenRouter id, so it's checked first), though today the two matchers
# can't actually collide — see is_openrouter_model's comment.
_MATCHERS: list[tuple[ModuleType, object]] = [
    (openrouter_provider, openrouter_provider.is_openrouter_model),
    (mistral_provider, mistral_provider.is_mistral_model),
]


def provider_for(model: str) -> ModuleType | None:
    """The provider module handling `model`, or None if no known provider
    matches (an admin panel selection retired from the catalog, most likely).

    Every returned module exposes `extract_structured(...)` and
    `answer_question(...)` with identical signatures, so callers never branch on
    which one came back.
    """
    for module, matches in _MATCHERS:
        if matches(model):
            return module
    return None
