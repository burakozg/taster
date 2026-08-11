FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fail the build on an unusable provider SDK rather than at capture time.
# Provider SDKs are imported lazily inside the request path, so a resolved-but-
# incompatible version (e.g. the mistralai 1.x -> 2.x package restructure)
# otherwise only surfaces when that model is picked in Admin -> Models.
RUN python -c "from mistralai.client import Mistral; from openai import AsyncOpenAI; import anthropic; print('anthropic', anthropic.__version__)"

COPY app ./app
COPY config.yaml .

# Non-root — see tasting-log-design.md §7.
RUN useradd --create-home appuser
USER appuser

# No EXPOSE — this container accepts no inbound connections at all. It's a
# pure polling worker: outbound to the Fly relay and to CouchDB only.
CMD ["python", "-m", "app.worker"]
