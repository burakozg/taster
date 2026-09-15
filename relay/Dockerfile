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
# pyproject.toml — it's run in place via `uvicorn app.main:app`), so a single
# sync against the lockfile is all this layer needs; it stays cached across
# builds where only app code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

# --- application layer ---------------------------------------------------
COPY app ./app
COPY static ./static
COPY entrypoint.sh /entrypoint.sh

RUN useradd --create-home appuser && mkdir -p /data && chown appuser:appuser /data \
    && chmod +x /entrypoint.sh

# No USER here — the entrypoint starts as root only to chown the /data
# volume mount (root-owned when Fly mounts it), then drops to appuser via
# setpriv before exec'ing uvicorn. See entrypoint.sh.
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
