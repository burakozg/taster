"""Uniform handling of unusable model output, shared by all three provider paths.

The three paths detect trouble differently — that part is irreducible, because
each API signals it in its own way:

    truncation   Anthropic  stop_reason == "max_tokens"
                 OpenAI     incomplete_details.reason / status == "incomplete"
                 Mistral    choices[0].finish_reason == "length"
    refusal      Anthropic  stop_reason == "refusal"
                 OpenAI     (no signal consumed here)
                 Mistral    (no signal consumed here)

What used to differ needlessly was everything downstream of detection: three
wordings for the same failure, fence-tolerant JSON parsing on the Claude path
only, and — most visibly — provider errors reaching the PWA as "unexpected
error: ..." while Claude's arrived as a clean sentence. That asymmetry made the
same underlying problem look like three different bugs.

So detection stays local and reporting comes from here. Raise ModelOutputError
(or a subclass) for "the model ran but its output is unusable"; callers treat it
as a clean job failure rather than a crash.
"""
from __future__ import annotations

import json


class ModelOutputError(RuntimeError):
    """The model responded, but the response can't be turned into a note.

    Distinct from an API/transport error (those keep propagating as-is): this
    one carries a message meant for the user's screen.
    """


def truncated(provider: str, budget_key: str) -> ModelOutputError:
    """Hit the output token ceiling mid-answer.

    Worth naming explicitly on every path: the raw symptom is a JSON string
    that stops mid-token, which surfaces as "Unterminated string" and sends you
    hunting for a parser bug instead of a budget that's too low. Reasoning and
    thinking tokens are drawn from the same ceiling on the providers that have
    them, so the budget can be exhausted before any visible text is produced.
    """
    return ModelOutputError(
        f"model output was truncated (hit the output token limit) — raise "
        f"models.claude.{budget_key} in config.yaml, or lower effort [{provider}]"
    )


def no_text_output(provider: str, detail: str | None = None) -> ModelOutputError:
    return ModelOutputError(
        f"model produced no text output{f' ({detail})' if detail else ''} [{provider}]"
    )


def refused(provider: str, detail: str | None = None) -> ModelOutputError:
    return ModelOutputError(
        f"the model declined the request{f': {detail}' if detail else ''} [{provider}]"
    )


def loads_model_json(text: str, provider: str) -> dict:
    """Parse the model's JSON reply, tolerating a ```json fence.

    No provider path enforces the output schema any more — the Claude path
    describes it in the prompt, and the other two pass `strict: False` — so a
    fenced or prose-wrapped reply is possible everywhere, not just on Claude.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ModelOutputError(f"model output was not valid JSON: {e} [{provider}]") from e
