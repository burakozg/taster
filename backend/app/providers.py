"""Model id → provider module, in one place.

The four call sites that run a model (capture, lookup, the maintenance plan,
regenerate-pairings) all need the same decision, and each used to spell it out
inline:

    if openai_provider.is_openai_model(model) or mistral_provider.is_mistral_model(model):
        provider = openai_provider if openai_provider.is_openai_model(model) else mistral_provider

which is already awkward at two alternates and gets worse with every one added
— four copies of a nested ternary, each an opportunity to order the checks
differently from its siblings. So the routing table lives here instead, and the
call sites ask one question: is there an alternate provider for this id?

`None` means the Claude path. That asymmetry is deliberate rather than an
oversight: Anthropic is not a module with this interface — capture/lookup/manage
each drive the Anthropic SDK with their own tool loop inline — so it can't be
returned as a peer here. The alternates exist precisely because they DO share
one interface (`extract_structured` / `answer_question`).
"""
from __future__ import annotations

from types import ModuleType

from app import mistral_provider, openai_provider, openrouter_provider

# Order matters: OpenRouter's namespaced `vendor/model` ids are checked first so
# that `openai/gpt-5.1` routes to the router and not to OpenAI's own API (where
# that id doesn't exist). See is_openrouter_model's comment.
_MATCHERS: list[tuple[ModuleType, object]] = [
    (openrouter_provider, openrouter_provider.is_openrouter_model),
    (openai_provider, openai_provider.is_openai_model),
    (mistral_provider, mistral_provider.is_mistral_model),
]


def provider_for(model: str) -> ModuleType | None:
    """The alternate-provider module handling `model`, or None for Claude.

    Every returned module exposes `extract_structured(...)` and
    `answer_question(...)` with identical signatures, so callers never branch on
    which one came back.
    """
    for module, matches in _MATCHERS:
        if matches(model):
            return module
    return None
