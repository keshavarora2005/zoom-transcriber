FROM python:3.11-slim

# System deps for Playwright Chromium
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

ENV PLAYWRIGHT_BROWSERS_PATH=/app/pw-browsers

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --with-deps ensures all OS-level Chromium deps are present
RUN playwright install chromium --with-deps

COPY . .

RUN mkdir -p recordings static templates

ENV PORT=10000
ENV RECORDINGS_DIR=/app/recordings

EXPOSE 10000

# Use uvicorn directly — NOT python main.py
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]