"""The curated model list the admin panel offers — single source of truth.

Only models that support the capture feature set (vision + tool calling +
structured JSON output) are listed. Claude Haiku is deliberately absent: it has
a different API surface (no adaptive thinking/effort, older web-search tool
variant) that isn't worth the conditionals when the budget slots are covered.

`cost` is a shopping hint (€ = cheapest … €€€€ = priciest), not a bill. It is
derived, not eyeballed — assign it from this blend so the column stays
comparable across providers:

    blended $/MTok = (4 x input + 1 x output) / 5

Input-weighted 4:1 because that is this app's shape: a large system prompt
(the capture schema rides in it), a label photo, and web-search results go in;
one small JSON note comes out. Bands: € < 1, €€ 1-4, €€€ 4-7, €€€€ > 7.
List prices checked 2026-07-28 ($/MTok in/out -> blended):

    Claude Opus 4.8       5    / 25    -> 9.00   €€€€
    Claude Sonnet 5       3    / 15    -> 5.40   €€€
    GPT-5.1               1.25 / 10    -> 3.00   €€
    Grok 4.5              2    /  6    -> 2.80   €€
    Mistral Medium 3.5    1.5  /  7.5  -> 2.70   €€
    Gemini 3.6 Flash      1.5  /  7.5  -> 2.70   €€
    Kimi K2.6             0.589/  2.48 -> 0.97   €
    DeepSeek V4 Pro       0.397/  0.794-> 0.48   €
    Mistral Large 3       0.5  /  1.5  -> 0.70   €
    GPT-5 mini            0.25 /  2    -> 0.60   €
    Qwen3 VL 235B         0.21 /  1.9  -> 0.55   €
    Mistral Small 4       0.15 /  0.6  -> 0.24   €
    Ministral 3 14B       0.2  /  0.2  -> 0.20   €

The four OpenRouter rows are its pass-through token prices, checked live
2026-08-04 (see the curl below). They exclude two OpenRouter-specific costs the
€ column can't express: the ~5.5% fee on credit top-ups, and web search, which
is billed per result rather than per token (the `web` plugin's Exa engine, or
the underlying provider's own search rate where OpenRouter routes to it —
$0.014/search for Gemini, $0.005 for Grok). With web_search_max_uses at 3 that
is cents per capture, not a band change, but it is why a cheap OpenRouter model
is not as cheap as its row suggests.

Two traps this table exists to prevent. Mistral Large 3 is *cheaper* than
Mistral Medium 3.5 (0.70 vs 2.70) — the size word in a model name says nothing
about its price, and this entry was previously mis-tiered at €€€ on that
assumption. And Sonnet 5 is banded on its standard $3/$15; its $2/$10
introductory rate (through 2026-08-31) would blend to 3.60 and flip it to €€,
which is not worth churning the column over.

The relay never calls any of these models itself — this list only feeds the
admin dropdowns and validates PUT /admin/settings. The worker decides the
provider from the id (claude-* → Anthropic, gpt-* → OpenAI, mistral-*/pixtral-*
→ Mistral, and anything namespaced `vendor/model` → OpenRouter; see the worker's
providers.py). Each alternate needs its own key in the worker's .env:
OPENAI_API_KEY, MISTRAL_API_KEY, OPENROUTER_API_KEY. The Mistral path gets web
search via Mistral's Agents/Conversations API, with an automatic fall back to
its chat API (no web search) if that call fails — see mistral_provider.py; the
OpenRouter path gets it from OpenRouter's server-side `web` plugin.

OpenRouter is the one entry point here that is a router rather than a lab, and
it earns its slots by reaching families the other three can't: Google, xAI,
Moonshot, Qwen. Models already covered by a direct path are deliberately NOT
listed through it — `openai/gpt-5.1` and `anthropic/claude-opus-4-8` exist on
OpenRouter, but routing them there would only add a hop and a fee over the
direct entries above.

VERIFY IDS AND CAPABILITIES AGAINST THE LIVE API, NOT THE DOCS. Every wrong id
this file has shipped came from reading a documentation page: `pixtral-large-
latest` outlived its retirement here, and a later pass invented
`mistral-medium-3-5-26-04` / `mistral-large-3-25-12` by mistaking a model
card's URL slug for its `model` string. The docs also claimed Mistral Small 4
and the Ministral 3 family were text-only; the API says otherwise. One request
settles all of it (`capabilities.vision` / `.function_calling` per model):

    curl -s https://api.mistral.ai/v1/models \\
      -H "Authorization: Bearer $MISTRAL_API_KEY" \\
      | jq -r '.data[] | "\\(.id) vision=\\(.capabilities.vision)"' | sort

Mistral ids are `*-latest` aliases here — matching the convention already used
in the sibling security-digest app, so the two stay consistent. Dated
equivalents as of 2026-07-29, if a pin is ever needed:
mistral-small-2603, mistral-medium-2604, mistral-large-2512,
ministral-14b-2512.

Every Mistral entry below reports vision + function calling live, which is the
bar: photo captures need image input, and the query_notes tool needs function
calling. Ministral 3 8B/3B are also multimodal and cheaper still, but 14B is
the smallest of the family worth pointing at a structured-extraction task.

OpenRouter's catalog answers the same question in one unauthenticated request —
no key needed, so there is no excuse for guessing an id here either. It carries
pricing too, which is where the four rows in the table above came from:

    curl -s https://openrouter.ai/api/v1/models | jq -r '
      .data[]
      | select(.architecture.input_modalities | index("image"))
      | select(.supported_parameters | index("tools") and index("structured_outputs"))
      | "\\(.id) in=\\(.pricing.prompt) out=\\(.pricing.completion)"' | sort

The OpenRouter entries below are the survivors of that filter, chosen one per
family and skipping two kinds of id that look tempting in the output: anything
`-preview` (Gemini 3.1 Pro is only available that way today, and a preview id
disappears without notice) and the `~vendor/model-latest` aliases (they float to
a different model, and this file's whole point is that the € column means
something).
"""

# Ordered priciest -> cheapest by the blend above, so the € column reads as a
# sorted ladder in the Admin dropdown instead of grouping by provider.
MODEL_CATALOG: list[dict] = [
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "anthropic", "cost": "€€€€"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "provider": "anthropic", "cost": "€€€"},
    {"id": "gpt-5.1", "label": "GPT-5.1", "provider": "openai", "cost": "€€"},
    {"id": "x-ai/grok-4.5", "label": "Grok 4.5", "provider": "openrouter", "cost": "€€"},
    {"id": "mistral-medium-latest", "label": "Mistral Medium 3.5", "provider": "mistral", "cost": "€€"},
    {"id": "google/gemini-3.6-flash", "label": "Gemini 3.6 Flash", "provider": "openrouter", "cost": "€€"},
    {"id": "moonshotai/kimi-k2.6", "label": "Kimi K2.6", "provider": "openrouter", "cost": "€"},
    {"id": "deepseek/deepseek-v4-pro", "label": "DeepSeek V4 Pro", "provider": "openrouter", "cost": "€"},
    {"id": "mistral-large-latest", "label": "Mistral Large 3", "provider": "mistral", "cost": "€"},
    {"id": "gpt-5-mini", "label": "GPT-5 mini", "provider": "openai", "cost": "€"},
    {"id": "qwen/qwen3-vl-235b-a22b-instruct", "label": "Qwen3 VL 235B", "provider": "openrouter", "cost": "€"},
    {"id": "mistral-small-latest", "label": "Mistral Small 4", "provider": "mistral", "cost": "€"},
    {"id": "ministral-14b-latest", "label": "Ministral 3 14B", "provider": "mistral", "cost": "€"},
]

MODEL_IDS = {m["id"] for m in MODEL_CATALOG}

# Settings whose value is a model id. `capture_model`/`lookup_model` are the
# retired pair — both meant "the model for a job with a photo", so they were
# folded into `image_model`. They stay listed so a value stored under the old
# name is still pruned when the model behind it is retired, and so migrate()
# below has one place to read them from.
_MODEL_SETTINGS = ("image_model", "text_model", "capture_model", "lookup_model")


def migrate_model_settings(settings: dict) -> dict:
    """Fold a stored capture_model/lookup_model pair into image_model.

    Settings outlive a rename: the choice lives in the relay's database, not in
    the code, so a deploy that renames the key would silently drop the user's
    saved model back to the worker's config.yaml default. capture_model wins
    over lookup_model when both are set and differ — captures are the job that
    actually depends on image quality.
    """
    out = {k: v for k, v in settings.items() if k not in ("capture_model", "lookup_model")}
    if not out.get("image_model"):
        legacy = settings.get("capture_model") or settings.get("lookup_model")
        if legacy:
            out["image_model"] = legacy
    return out


def prune_unknown_models(settings: dict) -> dict:
    """Drop model overrides that are no longer in the catalog.

    PUT /admin/settings validates on write, but a saved choice outlives that
    check: when a provider retires a model (Mistral dropped `pixtral-large-*`
    in favour of Mistral Medium 3.5), the stored id keeps riding on every job
    and every capture fails with the provider's `invalid_model` 400 until
    someone re-picks in the Admin tab. Pruning on read makes a retired
    override fall back to the worker's config.yaml default instead.
    """
    return {
        k: v for k, v in settings.items()
        if k not in _MODEL_SETTINGS or v in MODEL_IDS
    }
