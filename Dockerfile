# Inky Frame service. Runs on Raspberry Pi (arm64, Bookworm) but builds on any
# arch. uv handles deps; the "hardware" group (inky + gpiod) is installed here.
FROM python:3.12-slim-bookworm

# Build tooling, kept as a safety net in case a wheel is missing for arm64
# (e.g. gpiod / epaper-dithering source builds). If everything resolves to
# wheels you can trim these. i2c-tools is handy for debugging the EEPROM.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev \
        i2c-tools \
        fonts-dejavu-core \
        fonts-lato \
        fonts-comfortaa \
        fonts-league-spartan \
        fonts-jetbrains-mono \
        fonts-ebgaramond \
    && rm -rf /var/lib/apt/lists/*

# If epaper-dithering has no arm64 wheel for your Python, uncomment to build it
# from source (adds a Rust toolchain, ~hundreds of MB and a slow first build):
# RUN apt-get update && apt-get install -y --no-install-recommends curl \
#     && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
#     && rm -rf /var/lib/apt/lists/*
# ENV PATH="/root/.cargo/bin:${PATH}"

# uv binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Install deps first for layer caching. uv.lock is optional (the * keeps the
# COPY from failing if it isn't committed yet).
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --group hardware

COPY app ./app

EXPOSE 8080

# Single worker: the panel and its GPIO lines must be owned by one process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
