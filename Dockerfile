# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install ".[llm,postgres]" \
    && useradd --create-home --uid 10001 permitpilot \
    && mkdir -p /srv/data && chown permitpilot /srv/data

USER permitpilot
ENV PERMITPILOT_DATABASE_URL=sqlite:////srv/data/permitpilot.db
EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
