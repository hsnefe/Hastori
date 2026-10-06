FROM python:3.12.15-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
COPY packages/common packages/common
# uv needs every workspace member's manifest to resolve the lock file, not its source
COPY services/ingestion/pyproject.toml services/ingestion/pyproject.toml
COPY services/simulator services/simulator
RUN uv sync --frozen --no-dev --package hastori-simulator
COPY seed seed
CMD ["python", "-m", "hastori_simulator"]
