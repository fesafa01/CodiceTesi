FROM ghcr.io/astral-sh/uv:0.11.19 AS uv
FROM python:3.13-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_NO_CACHE=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYDANTIC_AI_NO_BANNER=1
RUN useradd --uid 10001 --create-home app
COPY --from=builder /app/.venv /app/.venv
COPY main.py ./
USER app
ENTRYPOINT ["python", "/app/main.py"]
CMD ["--help"]
