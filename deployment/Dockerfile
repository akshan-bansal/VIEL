# Venue Integrity Audit API (replay mode). No order-entry capability.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VIA_MODE=replay VIA_CONFIG_DIR=/app/config
WORKDIR /app
RUN useradd --system --uid 10001 via
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir ".[api]"
USER via
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"
CMD ["uvicorn", "venue_integrity.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
