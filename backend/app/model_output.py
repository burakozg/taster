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
    # config.yaml's model-config block is still named `claude:` even though
    # every provider (OpenAI/Mistral/OpenRouter included) reads its budgets
    # from it — see config.ClaudeConfig's docstring. Naming it plainly here,
    # with the `[provider]` tag already on the message, avoids reading like
    # this OpenRouter/Mistral/OpenAI failure was somehow routed through Claude.
    return ModelOutputError(
        f"model output was truncated (hit the output token limit) — raise "
        f"{budget_key} under config.yaml's `claude:` block (shared setting "
        f"for every provider, not Claude-specific), or lower effort [{provider}]"
    )


def no_text_output(provider: str, detail: str | None = None) -> ModelOutputError:
    return ModelOutputError(
        f"model produced no text output{f' ({detail})' if detail else ''} [{provider}]"
    )


def refused(provider: str, detail: str | None = None) -> ModelOutputError:
    return ModelOutputError(
        f"the model declined the request{f': {detail}' if detail else ''} [{provider}]"
    )


def _extract_json_object(text: str) -> str | None:
    """The first balanced {...} in `text`, or None.

    Last resort for a reply with prose around the JSON. Brace counting is
    string-aware: a `{` or `}` inside a string value (a tasting note about a
    "{weird} label", an escaped quote) must not move the depth, or the slice
    ends in the wrong place and the salvage produces garbage instead of failing
    honestly.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = escaped = False
    for i in range(start, len(text)):
        c = text[i]
        if escaped:
            escaped = False
            continue
        if c == "\\" and in_string:
            escaped = True
        elif c == '"':
            in_string = not in_string
        elif not in_string:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def loads_model_json(text: str, provider: str) -> dict:
    """Parse the model's JSON reply, tolerating the wrappers models put round it.

    No provider path enforces the output schema any more — the Claude path
    describes it in the prompt, and the other three pass `strict: False` — so a
    fenced or prose-wrapped reply is possible everywhere.

    Reasoning models made this sharper: they narrate before answering, and on
    OpenRouter that narration can arrive in `content` ahead of the JSON (or as a
    `<think>` block around it). The bare json.loads that used to be here failed
    those with "Expecting value: line 1 column 1 (char 0)" — an error that
    describes column 1 of something it never showed you, and reads like an empty
    response when the reply was actually 4KB of correct work with a sentence in
    front of it.
    """
    stripped = text.strip()

    # ```json … ``` fence.
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    # <think> … </think> preamble, closed or (when truncated) unclosed.
    if stripped.startswith("<think>"):
        _, _, after = stripped.partition("</think>")
        stripped = (after or "").strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    if (candidate := _extract_json_object(stripped)) is not None:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Still unparseable. The full text is content (GEN-4) so it goes to DEBUG,
    # which is off by default — hence a short shape summary in the message
    # itself, because "char 0" alone sent us hunting for an empty response.
    raise ModelOutputError(
        f"model output was not valid JSON — {_shape(text)} [{provider}]"
    )


def _shape(text: str) -> str:
    """A one-line description of unparseable output: how much, and how it opens."""
    if not text.strip():
        return f"the reply was empty ({len(text)} chars, all whitespace)"
    prefix = " ".join(text.strip()[:60].split())
    return f"{len(text)} chars starting {prefix!r}"
