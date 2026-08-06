FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install runtime deps first so source-only rebuilds are fast.
COPY pyproject.toml ./
RUN pip install \
    "google-cloud-bigquery>=3.20" \
    "google-cloud-pubsub>=2.21" \
    "google-cloud-secret-manager>=2.20" \
    "requests>=2.32"

# Source + the codified Airtable schema (the orchestrator reads schema.json
# at runtime to derive BQ row layout — keeping it bundled means the container
# is self-contained and the build pipeline can't mismatch the two).
# Single-base architecture per ADR 0020 (the prior crm_schema.json was retired).
COPY src/ ./src/
COPY airtable/schema.json ./airtable/schema.json

ENV PYTHONPATH=/app/src

# Cloud Run Jobs require a non-root user; use the unprivileged ``nobody``
# account that ships with the slim image.
USER nobody

CMD ["python", "-m", "agency_brain.sync.airtable_to_bq"]
