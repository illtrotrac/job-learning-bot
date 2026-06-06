"""
Job scraper: queries SerpAPI Google Jobs based on the active resume profile.
Stores new results in the jobs table (dedup by title+company hash).
Free-tier safe: max 2 queries per run (~2 of 100 monthly credits).
"""

import hashlib
import json
import os
import sqlite3

import requests
from dotenv import load_dotenv

from database import get_connection, init_db
from parser import get_active_profile

load_dotenv()

SERPAPI_BASE = "https://serpapi.com/search"
SNIPPET_MAX_CHARS = 1200  # ~300 tokens — enough context for skill gap analysis


def build_queries(profile_data: dict) -> list[str]:
    """Derive 1–2 search queries from the resume profile."""
    title = profile_data.get("title", "").strip()
    skills = profile_data.get("skills") or []
    keywords = profile_data.get("keywords") or []

    queries: list[str] = []

    if title:
        queries.append(title)

    # Second query: title + top skill (adds specificity without wasting credits)
    if title and skills:
        combo = f"{title} {skills[0]}".strip()
        if combo != title:
            queries.append(combo)

    # Fallback when no title extracted
    if not queries and keywords:
        queries.append(keywords[0])

    return queries[:2]


def fetch_jobs(query: str, location: str = "") -> list[dict]:
    """Call SerpAPI Google Jobs and return the raw jobs_results list."""
    api_key = os.getenv("SERPAPI_KEY")
    if not api_key:
        raise ValueError("SERPAPI_KEY not set in .env")

    params: dict = {"engine": "google_jobs", "q": query, "api_key": api_key}
    if location:
        params["location"] = location

    resp = requests.get(SERPAPI_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"SerpAPI error: {data['error']}")

    return data.get("jobs_results", [])


def normalize_job(raw: dict, source: str = "google_jobs") -> dict | None:
    """Convert a raw SerpAPI result into our jobs schema. Returns None if unusable."""
    title = (raw.get("title") or "").strip()
    company = (raw.get("company_name") or "").strip()

    if not title or not company:
        return None

    # Dedup key: first 16 hex chars of title+company hash (stable across re-runs)
    job_id = hashlib.sha256(
        f"{title.lower()}|{company.lower()}".encode()
    ).hexdigest()[:16]

    # Best available application URL
    apply_options = raw.get("apply_options") or []
    url = apply_options[0].get("link", "") if apply_options else ""

    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": (raw.get("location") or "").strip(),
        "description_snippet": (raw.get("description") or "")[:SNIPPET_MAX_CHARS],
        "url": url,
        "source": source,
    }


def upsert_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> int:
    """Insert new jobs, skip duplicates. Returns the count of new rows."""
    new_count = 0
    for job in jobs:
        try:
            conn.execute(
                """
                INSERT INTO jobs
                    (job_id, title, company, location, description_snippet, url, source)
                VALUES
                    (:job_id, :title, :company, :location,
                     :description_snippet, :url, :source)
                """,
                job,
            )
            new_count += 1
        except sqlite3.IntegrityError:
            pass  # duplicate job_id — already stored
    conn.commit()
    return new_count


def list_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Return the most recent jobs for display."""
    rows = conn.execute(
        "SELECT id, title, company, location, source, scraped_at "
        "FROM jobs ORDER BY scraped_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def scrape_jobs(
    job_title: str | None = None,
    location: str = "",
    db_path: str | None = None,
) -> int:
    """
    Scrape Google Jobs via SerpAPI and store new listings in the DB.

    Args:
        job_title: Custom search query. Uses active resume profile if None.
        location:  Optional location filter, e.g. "Remote" or "London, UK".
        db_path:   Override DB path (uses .env default if None).

    Returns:
        Number of new jobs added to the database.
    """
    init_db(db_path)
    conn = get_connection(db_path)

    # Build queries ─────────────────────────────────────────────────────────
    if job_title:
        queries = [job_title]
    else:
        profile_row = get_active_profile(conn)
        if not profile_row:
            conn.close()
            raise ValueError(
                "No active resume profile found. "
                "Run parser.py first, or pass --title to scrape_jobs()."
            )
        profile_data = json.loads(profile_row["profile_json"])
        queries = build_queries(profile_data)
        print(f"Built queries from profile: {queries}")

    if location:
        print(f"Location filter: {location}")

    # Scrape and store ──────────────────────────────────────────────────────
    total_new = 0
    for query in queries:
        print(f"\nQuerying: '{query}'...")
        try:
            raw_jobs = fetch_jobs(query, location)
            normalized = [j for r in raw_jobs if (j := normalize_job(r)) is not None]
            new_count = upsert_jobs(conn, normalized)
            print(f"  {len(raw_jobs)} result(s) from SerpAPI → {new_count} new job(s) stored.")
            total_new += new_count
        except Exception as exc:
            print(f"  Error: {exc}")

    conn.close()
    print(f"\nTotal new jobs added: {total_new}")
    return total_new


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    title: str | None = None
    location = ""
    show_list = False

    i = 0
    while i < len(args):
        if args[i] == "--title" and i + 1 < len(args):
            title = args[i + 1]
            i += 2
        elif args[i] == "--location" and i + 1 < len(args):
            location = args[i + 1]
            i += 2
        elif args[i] == "--list":
            show_list = True
            i += 1
        else:
            print(f"Unknown argument: {args[i]}")
            i += 1

    if show_list:
        _conn = get_connection()
        jobs = list_jobs(_conn)
        _conn.close()
        if not jobs:
            print("No jobs in database yet. Run scraper.py first.")
        else:
            print(f"\n{'ID':<4} {'Title':<38} {'Company':<24} {'Location':<18} Scraped")
            print("─" * 110)
            for j in jobs:
                print(
                    f"{j['id']:<4} {j['title'][:37]:<38} "
                    f"{j['company'][:23]:<24} {j['location'][:17]:<18} "
                    f"{j['scraped_at']}"
                )
    else:
        print("Usage:")
        print("  python scraper.py                         # use active resume profile")
        print("  python scraper.py --title 'Data Scientist'")
        print("  python scraper.py --title 'ML Engineer' --location 'Remote'")
        print("  python scraper.py --list                  # show stored jobs")
        print()
        scrape_jobs(job_title=title, location=location)
