# ── Stage: runtime ────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Install system deps needed by Playwright's Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 \
    libpangocairo-1.0-0 libgtk-3-0 libdrm2 \
    ffmpeg \
    && (apt-get install -y libasound2t64 || apt-get install -y libasound2) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── CRITICAL: store Playwright browsers INSIDE the image, not in ~/.cache ─────
ENV PLAYWRIGHT_BROWSERS_PATH=/app/pw-browsers

# Install Python deps (cached layer — changes only when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium into /app/pw-browsers (persists inside the image)
RUN playwright install chromium

# Copy application code
COPY . .

# Create required runtime directories
RUN mkdir -p recordings static templates

# Runtime environment — port 10000 matches Render's detection
ENV PORT=10000
ENV RECORDINGS_DIR=/app/recordings

EXPOSE 10000

CMD ["python", "main.py"]