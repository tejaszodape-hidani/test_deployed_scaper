# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile for Job Scraper — runs on Render as a Background Worker
#
# Base: Python 3.11 on Debian (slim) — Python 3.14 has no pre-built
# packages for many scientific libs; 3.11 is the production sweet spot.
# Chromium is installed from the Debian repo and pinned to the system version.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Chromium browser + matching driver
        chromium \
        chromium-driver \
        # Font support (prevents rendering crashes)
        fonts-liberation \
        fonts-noto-color-emoji \
        # SSL / CA certs
        ca-certificates \
        # Used by undetected-chromedriver at install time
        curl \
        wget \
        gnupg \
        # Required by some Python packages
        gcc \
        g++ \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Chrome binary paths ───────────────────────────────────────────────────────
# Tell the scrapers exactly where Chromium lives (avoids auto-detection failures)
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

# ── Disable Selenium's auto-download manager (we use the system driver) ───────
ENV SE_MANAGER_PATH=/usr/bin/chromedriver

# ── Python environment ────────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ── App directory ─────────────────────────────────────────────────────────────
WORKDIR /app

# ── Install Python dependencies ───────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ── Copy source code ──────────────────────────────────────────────────────────
COPY . .

# ── Entrypoint ────────────────────────────────────────────────────────────────
CMD ["python", "run_scrapers.py"]
