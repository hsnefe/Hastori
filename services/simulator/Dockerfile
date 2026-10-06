FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
COPY packages/common packages/common
COPY services/ingestion services/ingestion
COPY services/simulator services/simulator
RUN uv sync --frozen --no-dev --package hastori-simulator
COPY seed seed
CMD ["python", "-m", "hastori_simulator"]
