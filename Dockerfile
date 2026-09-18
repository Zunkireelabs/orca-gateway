FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "orca_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
