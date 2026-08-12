# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md alembic.ini /app/
COPY src /app/src
COPY migrations /app/migrations
RUN --mount=type=cache,target=/root/.cache/pip pip install .

CMD ["uvicorn", "memory_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
