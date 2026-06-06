"""
SQLite schema setup. Run once to initialize the database.
All tables use TEXT for JSON blobs to keep queries simple.
"""

import sqlite3
import os
from pathlib import Path


def get_db_path() -> str:
    from dotenv import load_dotenv
    load_dotenv()
    return os.getenv("DB_PATH", "data/job_learning.db")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create all tables if they don't exist."""
    conn = get_connection(db_path)
    c = conn.cursor()

    # Stores the extracted resume profile as compact JSON (~500 tokens max)
    # extract_once: never reprocess the same resume
    c.execute("""
        CREATE TABLE IF NOT EXISTS resume_profile (
            id INTEGER PRIMARY KEY,
            source_type TEXT NOT NULL CHECK(source_type IN ('pdf', 'text')),
            source_hash TEXT NOT NULL UNIQUE,  -- SHA256 of raw input
            raw_text TEXT NOT NULL,
            profile_json TEXT NOT NULL,        -- extracted structured JSON
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1        -- only one active profile at a time
        )
    """)

    # Job listings scraped from SerpAPI (Indeed/Google Jobs)
    c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL UNIQUE,       -- dedup key (title+company hash)
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            description_snippet TEXT,          -- truncated to ~300 tokens
            url TEXT,
            source TEXT,                       -- 'indeed' | 'google_jobs'
            scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_analyzed INTEGER DEFAULT 0
        )
    """)

    # Skill gap analysis results (resume vs job listing)
    c.execute("""
        CREATE TABLE IF NOT EXISTS skill_gaps (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            resume_profile_id INTEGER NOT NULL REFERENCES resume_profile(id),
            match_score REAL,                  -- 0.0 to 1.0
            matched_skills TEXT,               -- JSON array
            missing_skills TEXT,               -- JSON array
            analysis_json TEXT,                -- full Claude response
            analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Learning resources fetched via Tavily + summarized by Claude
    c.execute("""
        CREATE TABLE IF NOT EXISTS learning_resources (
            id INTEGER PRIMARY KEY,
            skill TEXT NOT NULL,               -- the gap skill this covers
            title TEXT,
            url TEXT,
            summary TEXT,                      -- 3-sentence Claude summary
            resource_type TEXT,                -- 'article' | 'tutorial' | 'video'
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME,               -- 30-day TTL cache
            sent_count INTEGER DEFAULT 0       -- times included in digest
        )
    """)

    # History of daily digests sent via Telegram
    c.execute("""
        CREATE TABLE IF NOT EXISTS digest_history (
            id INTEGER PRIMARY KEY,
            digest_json TEXT NOT NULL,         -- full digest content
            jobs_included TEXT,                -- JSON array of job IDs
            resources_included TEXT,           -- JSON array of resource IDs
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            telegram_message_id TEXT
        )
    """)

    # Migrations: add columns introduced after initial schema
    migrations = [
        "ALTER TABLE skill_gaps ADD COLUMN tech_stack TEXT",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()
    conn.close()
    print(f"Database initialized at: {db_path or get_db_path()}")


if __name__ == "__main__":
    init_db()
