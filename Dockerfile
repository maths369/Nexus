FROM python:3.10-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY nexus /app/nexus
COPY config /app/config
COPY ARCHITECTURE_BLUEPRINT.md /app/ARCHITECTURE_BLUEPRINT.md
COPY MIGRATION_ARCHITECTURE_AND_PLAN.md /app/MIGRATION_ARCHITECTURE_AND_PLAN.md

ARG NEXUS_EXTRAS=""
RUN if [ -n "$NEXUS_EXTRAS" ]; then \
      pip install -e ".[${NEXUS_EXTRAS}]"; \
    else \
      pip install -e .; \
    fi

EXPOSE 8000

CMD ["python", "-m", "nexus", "serve", "--host", "0.0.0.0", "--port", "8000"]
