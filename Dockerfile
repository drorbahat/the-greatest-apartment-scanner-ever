FROM python:3.11-slim-bookworm

# Install Chromium + dependencies for CDP
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    chromium-common \
    chromium-sandbox \
    cron \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy everything
COPY . .

# Make chromium launcher executable
RUN chmod +x scripts/yogev-chromium 2>/dev/null || true

# Volume for persistent data
VOLUME /app/data

# Entrypoint starts cron + Telegram bot
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
