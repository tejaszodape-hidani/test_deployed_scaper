"""
push_to_db.py
─────────────────────────────────────────────────────────────────────────────
Centralised database configuration and helper functions for the Job Scraper.

Both all_scrape.py and workday_scraper.py import from here so that DB
credentials are maintained in a SINGLE place (.env file).

Usage
-----
    from push_to_db import engine, Job, Base, push_data, push_jobs_list, check_db_connection

Environment Variables (.env)
-----------------------------
    DATABASE_URL=mysql://user:password@host:3306/dbname
       or
    DATABASE_URL=postgresql://user:password@host:5432/dbname
       or leave unset → falls back to a local SQLite file (jobs.db)
"""

import os
import sys
import json
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, Index, JSON
)
from sqlalchemy.orm import declarative_base, Session
from sqlalchemy.sql import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text

# ── Load .env from the same directory as this file ───────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, ".env"))

# ── Build DATABASE_URL ────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(_DIR, 'jobs.db')}")

# SQLAlchemy requires the mysql+pymysql:// dialect prefix
if DATABASE_URL.startswith("mysql://"):
    DATABASE_URL = DATABASE_URL.replace("mysql://", "mysql+pymysql://")

# ── Engine & Base ─────────────────────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,          # auto-reconnect on stale connections
    pool_recycle=3600,           # recycle connections every hour
    connect_args={"connect_timeout": 10} if "mysql" in DATABASE_URL else {}
)

Base = declarative_base()

# ── Job Model ─────────────────────────────────────────────────────────────────
class Job(Base):
    """ORM model — mirrors the Job table used by both scrapers and the backend."""
    __tablename__ = "Job"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    jobId            = Column(String(255), unique=True, index=True)
    jobTitle         = Column(String(500), index=True)
    companyName      = Column(String(255), index=True)
    companyLogo      = Column(String(1000), nullable=True)
    companyLocation  = Column(String(255), nullable=True)
    jobLocation      = Column(String(255), index=True)
    jobType          = Column(String(50), index=True)
    yearOfExperience = Column(String(50), nullable=True)
    skills           = Column(JSON, nullable=True)
    jobPostTime      = Column(DateTime)
    jobDescription   = Column(Text)
    salary           = Column(String(255), nullable=True)
    jobSource        = Column(String(100), index=True, nullable=True)
    category         = Column(String(100), index=True, nullable=True, default="General")
    jobUrl           = Column(String(1000), nullable=True)
    country          = Column(String(50), nullable=True)

    createdAt  = Column(DateTime, default=func.now())
    updatedAt  = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_job_title",     "jobTitle"),
        Index("idx_job_company",   "companyName"),
        Index("idx_job_type",      "jobType"),
        Index("idx_job_location",  "jobLocation"),
        Index("idx_job_post_time", "jobPostTime"),
        Index("idx_job_skills",    "skills"),
        Index("idx_job_source",    "jobSource"),
        Index("idx_job_category",  "category"),
    )

# Create tables if they don't exist yet
Base.metadata.create_all(bind=engine)


# ── Public helpers ────────────────────────────────────────────────────────────

def check_db_connection() -> bool:
    """Verify the database is reachable.

    Prints connection info on success, or an error on failure.
    Returns True on success, False on failure (does NOT call sys.exit so
    callers can decide how to handle a missing DB).
    """
    print("[DB] Checking database connection...", end=" ", flush=True)
    try:
        with engine.connect() as conn:
            if "sqlite" in DATABASE_URL:
                conn.execute(text("SELECT 1"))
                job_count = conn.execute(text("SELECT COUNT(*) FROM `Job`")).scalar()
                print(" Connected (SQLite)")
            else:
                version   = conn.execute(text("SELECT VERSION()")).scalar()
                job_count = conn.execute(text("SELECT COUNT(*) FROM `Job`")).scalar()
                print(" Connected!")
                print(f"[DB] Version  : {version}")

            db_name = DATABASE_URL.split("/")[-1].split("?")[0]
            print(f"[DB] Database : {db_name}")
            print(f"[DB] Job rows : {job_count:,} existing records")
        return True
    except Exception as exc:
        print(" FAILED")
        print(f"[DB] Error    : {exc}")
        return False


def push_data(job_dict: dict) -> bool:
    """Insert or skip a single job record (upsert by jobId).

    Parameters
    ----------
    job_dict : dict
        A job dictionary matching the Job model schema.

    Returns
    -------
    bool
        True if the record was inserted, False if it already existed or failed.
    """
    if not job_dict:
        return False

    # Normalise jobPostTime / createdAt / updatedAt to datetime objects
    for field in ("jobPostTime", "createdAt", "updatedAt"):
        val = job_dict.get(field)
        if isinstance(val, str):
            try:
                job_dict[field] = datetime.fromisoformat(val)
            except ValueError:
                job_dict[field] = datetime.now()
        elif val is None:
            job_dict[field] = datetime.now()

    # Ensure skills is JSON-serialisable (list)
    if "skills" in job_dict and not isinstance(job_dict["skills"], list):
        try:
            job_dict["skills"] = list(job_dict["skills"])
        except Exception:
            job_dict["skills"] = []

    # Strip any keys that are not columns on the Job model
    valid_columns = {c.key for c in Job.__table__.columns}
    clean = {k: v for k, v in job_dict.items() if k in valid_columns}

    try:
        with Session(engine) as session:
            job_obj = Job(**clean)
            session.add(job_obj)
            session.commit()
        return True
    except IntegrityError:
        # Duplicate jobId — skip silently
        return False
    except Exception as exc:
        print(f"[DB] push_data error for jobId={job_dict.get('jobId')}: {exc}")
        return False


def push_jobs_list(jobs: list) -> int:
    """Bulk-insert a list of job dicts. Skips duplicates.

    Parameters
    ----------
    jobs : list[dict]
        List of job dictionaries matching the Job model schema.

    Returns
    -------
    int
        Number of records actually inserted.
    """
    if not jobs:
        return 0

    inserted = 0
    valid_columns = {c.key for c in Job.__table__.columns}

    # Normalise all records upfront
    normalised = []
    for job_dict in jobs:
        if not isinstance(job_dict, dict):
            continue

        for field in ("jobPostTime", "createdAt", "updatedAt"):
            val = job_dict.get(field)
            if isinstance(val, str):
                try:
                    job_dict[field] = datetime.fromisoformat(val)
                except ValueError:
                    job_dict[field] = datetime.now()
            elif val is None:
                job_dict[field] = datetime.now()

        if "skills" in job_dict and not isinstance(job_dict["skills"], list):
            try:
                job_dict["skills"] = list(job_dict["skills"])
            except Exception:
                job_dict["skills"] = []

        normalised.append({k: v for k, v in job_dict.items() if k in valid_columns})

    with Session(engine) as session:
        for clean in normalised:
            try:
                session.add(Job(**clean))
                session.commit()
                inserted += 1
            except IntegrityError:
                session.rollback()  # duplicate — skip
            except Exception as exc:
                session.rollback()
                print(f"[DB] push_jobs_list error for jobId={clean.get('jobId')}: {exc}")

    return inserted


def get_job_count() -> int:
    """Return the current number of rows in the Job table."""
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT COUNT(*) FROM `Job`")).scalar() or 0
    except Exception:
        return -1
