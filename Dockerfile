# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    APP_AUTO_CREATE_SCHEMA=false \
    APP_INLINE_WORKER=true \
    APP_SKILL_ROOT=/skills

WORKDIR /app

COPY backend/pyproject.toml backend/README.md backend/alembic.ini /app/
COPY backend/src /app/src
COPY backend/migrations /app/migrations
COPY .agents/skills /skills

RUN --mount=type=cache,target=/root/.cache/pip pip install .

EXPOSE 8000

CMD ["/bin/sh", "-c", "alembic upgrade head && uvicorn memory_agent.main:app --host 0.0.0.0 --port 8000"]
