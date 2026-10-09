FROM python:3.12.15-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
COPY packages/common packages/common
# uv needs every workspace member's manifest to resolve the lock file, not its source
COPY services/ingestion/pyproject.toml services/ingestion/pyproject.toml
COPY services/alarm/pyproject.toml services/alarm/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/simulator services/simulator
RUN uv sync --frozen --no-dev --package hastori-simulator
COPY seed seed
# Unprivileged: nothing in here needs root at run time.
RUN useradd --system --uid 10001 --no-create-home app
USER 10001
CMD ["python", "-m", "hastori_simulator"]
