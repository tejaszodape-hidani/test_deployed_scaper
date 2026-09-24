# Automatic Job Extractor V2

An enterprise-grade, multi-threaded job scraping pipeline designed to extract, parse, and push job postings from various platforms (LinkedIn, Glassdoor, HiringCafe, Workday, etc.) into a centralized MySQL database. 

It is built for continuous 24-hour background operation, complete with Cloudflare bypass mechanisms, process locking, and robust error recovery.

## 🌟 Features
- **Multi-Source Scraping**: Extracts from LinkedIn, Glassdoor, JobRight, HiringCafe, and Workday.
- **Advanced Bot Bypass**: Uses `curl_cffi` to impersonate `chrome124` fingerprints to bypass Cloudflare 403 blocks (specifically on HiringCafe).
- **Intelligent Deduping**: Prevents duplicate DB insertions by cross-checking existing URLs per cycle.
- **24/7 Scheduler**: Built-in Python orchestrator (`run_scrapers.py`) that handles retries, heartbeats, and interval-based execution.
- **Production Ready**: Fully containerized via Docker for deployment to platforms like Render.
- **Centralized Database (SQLAlchemy)**: Safe connection pooling, pre-ping checks, and automated generated columns for skill indexing.

---

## 🛠️ Prerequisites
- Python 3.11+
- Chromium / Google Chrome installed locally (or via Docker)
- MySQL Database (e.g., Aiven, AWS RDS, or local)

---

## 🚀 Local Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/job-scraper-v2.git
   cd job-scraper-v2
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On macOS/Linux
   # venv\Scripts\activate   # On Windows
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up Environment Variables:**
   Rename `.env.example` to `.env` and fill in your database credentials:
   ```env
   # .env
   DATABASE_URL=mysql+pymysql://user:password@host:port/dbname

   SAVE_TO_DB=true
   SAVE_TO_DATABASE=true
   SAVE_TO_JSON=true
   ```

---

## 💻 Running the Scraper

The recommended way to run the entire pipeline is via the orchestrator:

```bash
# Run continuously (defaults to 24-hour intervals)
python run_scrapers.py

# Run only once and exit
python run_scrapers.py --run-once

# Change the interval (e.g., every 12 hours)
python run_scrapers.py --interval 12
```

> **Note:** Do NOT run `all_scrape.py` or `workday_scraper.py` directly in production. The orchestrator handles file locks, logging, and crash recovery.

---

## ☁️ Deployment (Render)

This project is configured out-of-the-box for [Render](https://render.com) using Infrastructure as Code (`render.yaml`).

1. Push your code to GitHub.
2. In the Render Dashboard, create a **New Background Worker**.
3. Connect your repository. Render will automatically detect the `render.yaml` and `Dockerfile`.
4. In the Render environment settings, add your secret `DATABASE_URL`.
5. Deploy! The worker will pull the Chromium dependencies, install Python packages, and run continuously.

*(Note: On Render, `SAVE_TO_JSON` should be `false` since the filesystem is ephemeral. Data will be saved directly to the database.)*

---

## 🗂️ Code Structure
- **`run_scrapers.py`** — The master 24-hr orchestrator. Handles locking and logging.
- **`all_scrape.py`** — The primary deep-scraper for LinkedIn, Glassdoor, and HiringCafe.
- **`workday_scraper.py`** — Specialized multi-worker scraper for Workday portals.
- **`push_to_db.py`** — Centralized SQLAlchemy DB configuration and models.
- **`latest_scraping_links.json`** — Contains the specific URLs/configs the scraper targets.
- **`Dockerfile`** — Linux environment setup including Chromium binaries for headless scraping.
