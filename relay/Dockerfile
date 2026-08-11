FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
