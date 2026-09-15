FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# uv itself, pinned to a known release rather than whatever pip resolves.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

WORKDIR /app

# --- dependency layer ---------------------------------------------------
# This service isn't installed as a package (tool.uv.package = false in
# pyproject.toml — it's run in place via `python -m app.worker`), so a single
# sync against the lockfile is all this layer needs; it stays cached across
# builds where only app code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

# Fail the build on an unusable provider SDK rather than at capture time.
# Provider SDKs are imported lazily inside the request path, so a resolved-but-
# incompatible version (e.g. the mistralai 1.x -> 2.x package restructure)
# otherwise only surfaces when that model is picked in Admin -> Models.
RUN python -c "from mistralai.client import Mistral; from openai import AsyncOpenAI; print('mistralai/openai SDKs import OK')"

# --- application layer ---------------------------------------------------
COPY app ./app
COPY config.yaml .

# Non-root — see tasting-log-design.md §7.
RUN useradd --create-home appuser
USER appuser

# No EXPOSE — this container accepts no inbound connections at all. It's a
# pure polling worker: outbound to the Fly relay and to CouchDB only.
CMD ["python", "-m", "app.worker"]
