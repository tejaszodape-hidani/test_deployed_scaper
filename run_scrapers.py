"""
run_scrapers.py  —  Production-grade 24-hour deployment runner
─────────────────────────────────────────────────────────────────────────────
Execution order every cycle:
    1. all_scrape.py  (Pass 1) – LinkedIn / SimplyHired / HiringCafe / Indeed — US jobs
    2. workday_scraper.py      – Workday career-portal scraper  (US + UK jobs)
    3. all_scrape.py  (Pass 2) – LinkedIn / SimplyHired / HiringCafe / Indeed — UK jobs
       (Pass 2 uses link_uk.json automatically — handled inside all_scrape.main())

Production features
───────────────────
• Graceful shutdown  : SIGTERM / SIGINT finish the current scraper cleanly
• Lock file          : prevents two instances running at the same time
• Structured logging : rotates at 10 MB, keeps 5 backups (scraper.log)
• Heartbeat file     : updated every cycle (monitor with uptime checks)
• Retry on crash     : individual scraper crash does NOT kill the loop
• Exact-interval     : drift-free scheduling (wall-clock based, not sleep-based)
• DB health gate     : skips the cycle if DB is unreachable

Usage
─────
    python run_scrapers.py                   # every 24 h
    python run_scrapers.py --interval 12     # every 12 h
    python run_scrapers.py --run-once        # one shot, then exit
    python run_scrapers.py --log-file /var/log/scraper/scraper.log

Deploy (keep alive)
───────────────────
    # PM2  (recommended)
    pm2 start run_scrapers.py --interpreter python3 --name job-scraper

    # systemd  (see: systemd/job-scraper.service)
    systemctl start job-scraper

    # simple background
    nohup python run_scrapers.py > scraper.log 2>&1 &
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta

# ── Load .env early so SCRAPER_INTERVAL_HOURS and other vars are available ────
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)
except ImportError:
    pass  # python-dotenv not installed; rely on shell environment

# ── Paths ─────────────────────────────────────────────────────────────────────
_DIR            = os.path.dirname(os.path.abspath(__file__))
_LOCK_FILE      = os.path.join(_DIR, ".scraper.lock")
_HEARTBEAT_FILE = os.path.join(_DIR, "scraper_heartbeat.json")
_DEFAULT_LOG    = os.path.join(_DIR, "scraper.log")

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup  (file + console)
# ─────────────────────────────────────────────────────────────────────────────

def _setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("scraper")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler — 10 MB per file, 5 backups
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


log: logging.Logger  # set in main()


# ─────────────────────────────────────────────────────────────────────────────
# Process lock  (prevents duplicate instances)
# ─────────────────────────────────────────────────────────────────────────────

_lock_fh = None  # held open for the lifetime of the process

def _acquire_lock() -> bool:
    """Return True if we successfully acquired the lock, False otherwise."""
    global _lock_fh
    try:
        _lock_fh = open(_LOCK_FILE, "w")
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
        return True
    except (IOError, OSError):
        return False


def _release_lock() -> None:
    global _lock_fh
    if _lock_fh:
        try:
            fcntl.flock(_lock_fh, fcntl.LOCK_UN)
            _lock_fh.close()
            os.unlink(_LOCK_FILE)
        except Exception:
            pass
        _lock_fh = None


# ─────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────────────────────

_shutdown_requested = False


def _handle_signal(signum, frame):  # noqa: ANN001
    global _shutdown_requested
    sig_name = signal.Signals(signum).name
    log.warning(f"Signal {sig_name} received — finishing current scraper then shutting down.")
    _shutdown_requested = True


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat
# ─────────────────────────────────────────────────────────────────────────────

def _write_heartbeat(cycle: int, status: str, next_run: datetime | None = None) -> None:
    payload = {
        "pid":      os.getpid(),
        "cycle":    cycle,
        "status":   status,
        "updated":  datetime.now().isoformat(),
        "next_run": next_run.isoformat() if next_run else None,
    }
    try:
        tmp = _HEARTBEAT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, _HEARTBEAT_FILE)
    except Exception as exc:
        log.warning(f"Could not write heartbeat: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Optional imports (scrapers + DB check)
# ─────────────────────────────────────────────────────────────────────────────

def _load_scrapers():
    """Lazily import scraper modules so startup errors are clearly reported."""
    scrapers = {}

    try:
        from push_to_db import check_db_connection as _check_db
        scrapers["check_db"] = _check_db
    except ImportError as exc:
        log.warning(f"push_to_db not available: {exc}")

    try:
        from all_scrape import main as _all_scrape
        scrapers["all_scrape"] = _all_scrape
    except ImportError as exc:
        log.warning(f"all_scrape.py not importable: {exc}")

    # try:
    #     from workday_scraper import scrape_all_workday as _workday
    #     # scrapers["workday"] = _workday  # Temporarily disabled per user request
    # except ImportError as exc:
    #     log.warning(f"workday_scraper.py not importable: {exc}")

    return scrapers


# ─────────────────────────────────────────────────────────────────────────────
# Single pipeline run
# ─────────────────────────────────────────────────────────────────────────────

def _run_step(name: str, fn, cycle: int) -> bool:
    """Run one scraper step, catch all exceptions, return True on success."""
    log.info(f"[Cycle {cycle}] ── {name} starting ──")
    _write_heartbeat(cycle, f"running:{name}")
    t0 = time.monotonic()
    try:
        fn()
        elapsed = time.monotonic() - t0
        log.info(f"[Cycle {cycle}] ── {name} finished in {elapsed:.0f}s ──")
        return True
    except SystemExit as exc:
        # Scrapers sometimes call sys.exit() on fatal errors; treat as failure
        elapsed = time.monotonic() - t0
        log.error(f"[Cycle {cycle}] {name} called sys.exit({exc.code}) after {elapsed:.0f}s")
        return False
    except Exception:
        elapsed = time.monotonic() - t0
        log.error(
            f"[Cycle {cycle}] {name} CRASHED after {elapsed:.0f}s:\n{traceback.format_exc()}"
        )
        return False


def run_pipeline(scrapers: dict, cycle: int) -> dict:
    """Run the full scraping pipeline and return a result summary.

    Cycle order:
      Step 1 — all_scrape.py  (US jobs, then UK jobs via link_uk.json)
      Step 2 — workday_scraper.py  (Workday portals: US + UK jobs)
    """
    results: dict[str, bool | str] = {"cycle": cycle, "started": datetime.now().isoformat()}
    log.info(f"{'='*65}")
    log.info(f"  PIPELINE START — Cycle #{cycle}  [{results['started']}]")
    log.info(f"{'='*65}")

    # ── DB health gate ────────────────────────────────────────────────────────
    if "check_db" in scrapers:
        ok = scrapers["check_db"]()
        results["db_ok"] = ok
        if not ok:
            log.warning("DB connection failed — jobs will be saved to JSON only.")
    else:
        results["db_ok"] = None

    # ── Step 1: all_scrape (US  →  UK) ───────────────────────────────────────
    # all_scrape.main() runs Pass-1 (US config) then Pass-2 (link_uk.json)
    # in a single call, so one step covers both regions.
    if "all_scrape" in scrapers:
        results["all_scrape"] = _run_step(
            "all_scrape.py (US + UK)", scrapers["all_scrape"], cycle
        )
    else:
        log.warning("all_scrape.py — skipped (not importable)")
        results["all_scrape"] = "skipped"

    if _shutdown_requested:
        results["aborted_early"] = True
        log.info("Shutdown requested — skipping remaining steps.")
        return results

    # ── Step 2: workday_scraper (US + UK) ─────────────────────────────────────
    if "workday" in scrapers:
        # Clear Workday progress file so companies are scraped again every cycle
        try:
            os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "workday_progress.json"))
        except FileNotFoundError:
            pass
        results["workday"] = _run_step(
            "workday_scraper.py (US + UK)", scrapers["workday"], cycle
        )
    else:
        log.warning("workday_scraper.py — skipped (not importable)")
        results["workday"] = "skipped"

    results["finished"] = datetime.now().isoformat()
    log.info(f"{'='*65}")
    log.info(f"  PIPELINE COMPLETE — Cycle #{cycle}  [{results['finished']}]")
    log.info(f"{'='*65}")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global log

    parser = argparse.ArgumentParser(description="Production 24-hour job scraper scheduler")
    _default_interval = float(os.getenv("SCRAPER_INTERVAL_HOURS", "0"))
    parser.add_argument("--interval",  type=float, default=_default_interval, metavar="HOURS",
                        help="Hours between pipeline runs (default: SCRAPER_INTERVAL_HOURS env or 24)")
    parser.add_argument("--run-once",  action="store_true",
                        help="Run the pipeline once and exit")
    parser.add_argument("--log-file",  default=_DEFAULT_LOG, metavar="PATH",
                        help=f"Log file path (default: {_DEFAULT_LOG})")
    args = parser.parse_args()

    log = _setup_logging(args.log_file)

    # ── Single-instance guard ─────────────────────────────────────────────────
    if not _acquire_lock():
        log.error(
            "Another instance of run_scrapers.py is already running "
            f"(lock file: {_LOCK_FILE}). Exiting."
        )
        sys.exit(1)

    # ── Signal handlers ───────────────────────────────────────────────────────
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    interval_seconds = args.interval * 3600
    mode = "single-run" if args.run_once else f"every {args.interval:.1f}h"

    log.info(f"Job Scraper Deployment Runner started (PID {os.getpid()}, mode={mode})")
    log.info(f"Log file: {args.log_file}")
    log.info(f"Lock file: {_LOCK_FILE}")

    scrapers = _load_scrapers()
    log.info(f"Loaded scrapers: {list(scrapers.keys())}")

    cycle = 1
    try:
        while not _shutdown_requested:
            # ── Drift-free next-run timestamp ─────────────────────────────────
            cycle_start = time.monotonic()
            next_run_dt = datetime.now() + timedelta(seconds=interval_seconds)

            _write_heartbeat(cycle, "running", next_run_dt)
            run_pipeline(scrapers, cycle)

            if args.run_once or _shutdown_requested:
                break

            # ── Sleep in 5-second chunks so SIGTERM is handled promptly ───────
            elapsed = time.monotonic() - cycle_start
            remaining = max(0.0, interval_seconds - elapsed)
            log.info(
                f"Next pipeline run at: {next_run_dt.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(sleeping {remaining / 3600:.2f}h)"
            )
            _write_heartbeat(cycle, "sleeping", next_run_dt)

            deadline = time.monotonic() + remaining
            while time.monotonic() < deadline and not _shutdown_requested:
                time.sleep(min(5.0, deadline - time.monotonic()))

            cycle += 1

    finally:
        log.info("Shutting down — releasing lock file.")
        _write_heartbeat(cycle, "stopped")
        _release_lock()
        log.info("Goodbye.")


if __name__ == "__main__":
    main()
