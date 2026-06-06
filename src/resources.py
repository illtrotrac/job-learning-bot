"""
Learning resource finder: searches Tavily for resources on missing skills,
summarizes each with Claude Haiku, and caches in learning_resources table.
30-day TTL prevents re-fetching the same skill repeatedly.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta

import anthropic
from dotenv import load_dotenv
from tavily import TavilyClient

from database import get_connection, init_db

load_dotenv()

MODEL = "claude-haiku-4-5"
CACHE_DAYS = 30
MAX_RESULTS_PER_SKILL = 3

SUMMARY_PROMPT = """You are a learning coach. Write a 2-sentence summary of this resource for someone
learning <<SKILL>> to advance their tech/data career.
Sentence 1: what the resource covers. Sentence 2: why it is useful for this skill specifically.
Be concrete. No filler phrases like "This article explores...".

Title: <<TITLE>>
Content snippet: <<CONTENT>>

Return ONLY the 2 sentences, nothing else."""


def get_missing_skills(conn: sqlite3.Connection, top_n: int = 5) -> list[str]:
    """Aggregate missing skills across all analyzed jobs, return top N by frequency."""
    rows = conn.execute(
        "SELECT missing_skills FROM skill_gaps WHERE missing_skills IS NOT NULL"
    ).fetchall()

    counts: dict[str, int] = {}
    for row in rows:
        for skill in json.loads(row["missing_skills"] or "[]"):
            key = skill.strip()
            if key:
                counts[key] = counts.get(key, 0) + 1

    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [skill for skill, _ in ranked[:top_n]]


def is_cached(conn: sqlite3.Connection, skill: str) -> bool:
    """True if fresh (non-expired) resources exist for this skill."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM learning_resources "
        "WHERE skill = ? AND (expires_at IS NULL OR expires_at > datetime('now'))",
        (skill,),
    ).fetchone()
    return row["cnt"] > 0


def search_tavily(skill: str, max_results: int = MAX_RESULTS_PER_SKILL) -> list[dict]:
    """Search Tavily for learning resources about a skill."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise ValueError("TAVILY_API_KEY not set in .env")

    client = TavilyClient(api_key=api_key)
    query = f"learn {skill} tutorial beginner guide"
    response = client.search(query=query, max_results=max_results, search_depth="basic")
    return response.get("results", [])


def summarize_resource(skill: str, title: str, content: str) -> str:
    """Call Claude Haiku for a 2-sentence learning-focused summary."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = (
        SUMMARY_PROMPT
        .replace("<<SKILL>>", skill)
        .replace("<<TITLE>>", title)
        .replace("<<CONTENT>>", content[:800])
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def infer_resource_type(url: str, title: str) -> str:
    """Guess resource type from URL and title."""
    url_l, title_l = url.lower(), title.lower()
    if any(x in url_l for x in ["youtube.com", "youtu.be", "vimeo.com"]):
        return "video"
    if any(x in title_l for x in ["tutorial", "how to", "guide", "course", "learn", "introduction"]):
        return "tutorial"
    return "article"


def store_resources(
    conn: sqlite3.Connection,
    skill: str,
    results: list[dict],
    summaries: list[str],
) -> int:
    """Save resources to DB. Returns count inserted."""
    expires = (datetime.utcnow() + timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    saved = 0
    for result, summary in zip(results, summaries):
        url = result.get("url", "")
        title = result.get("title", "")
        conn.execute(
            """
            INSERT INTO learning_resources (skill, title, url, summary, resource_type, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (skill, title, url, summary, infer_resource_type(url, title), expires),
        )
        saved += 1
    conn.commit()
    return saved


def fetch_resources(
    skills: list[str] | None = None,
    db_path: str | None = None,
    skip_cache: bool = False,
) -> int:
    """
    Find and store learning resources for missing skills.

    Args:
        skills:     Explicit skill list. Falls back to top missing skills from DB if None.
        db_path:    Override DB path.
        skip_cache: Re-fetch even if cached.

    Returns:
        Total resources saved.
    """
    init_db(db_path)
    conn = get_connection(db_path)

    if skills is None:
        skills = get_missing_skills(conn)

    if not skills:
        print("No missing skills found. Run analyzer.py first.")
        conn.close()
        return 0

    print(f"Finding resources for: {', '.join(skills)}\n")
    # Haiku cost: ~150 tokens out × $5/1M ≈ $0.00075 per resource, $0.0023 per skill (3 resources)
    print(f"Estimated cost: ~${len(skills) * 0.003:.3f}\n")

    total_saved = 0
    for skill in skills:
        if not skip_cache and is_cached(conn, skill):
            print(f"  '{skill}': cached (< {CACHE_DAYS} days old), skipping.")
            continue

        print(f"  '{skill}': searching...")
        try:
            results = search_tavily(skill)
            if not results:
                print("    No results returned by Tavily.")
                continue

            summaries = []
            for r in results:
                summary = summarize_resource(skill, r.get("title", ""), r.get("content", ""))
                summaries.append(summary)

            saved = store_resources(conn, skill, results, summaries)
            print(f"    {saved} resource(s) saved.")
            total_saved += saved
        except Exception as exc:
            print(f"    Error: {exc}")

    conn.close()
    print(f"\nDone. {total_saved} resource(s) added.")
    return total_saved


def list_resources(
    conn: sqlite3.Connection,
    skill: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """Return stored resources, optionally filtered by skill."""
    if skill:
        rows = conn.execute(
            "SELECT * FROM learning_resources WHERE skill = ? ORDER BY fetched_at DESC LIMIT ?",
            (skill, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM learning_resources ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    show_list = False
    explicit_skills: list[str] | None = None
    list_skill_filter: str | None = None
    skip_cache = False

    i = 0
    while i < len(args):
        if args[i] == "--list":
            show_list = True
            i += 1
        elif args[i] == "--skill" and i + 1 < len(args):
            explicit_skills = [args[i + 1]]
            i += 2
        elif args[i] == "--refresh":
            skip_cache = True
            i += 1
        else:
            i += 1

    if show_list:
        conn = get_connection()
        resources = list_resources(conn, skill=list_skill_filter)
        conn.close()
        if not resources:
            print("No resources yet. Run resources.py first.")
        else:
            print(f"\n{'Skill':<22} {'Type':<10} {'Title'}")
            print("─" * 95)
            for r in resources:
                print(
                    f"{r['skill'][:21]:<22} {r['resource_type'][:9]:<10} "
                    f"{r['title'][:58]}"
                )
            print()
            # Print summaries for first 5
            print("Summaries (first 5):")
            print("─" * 95)
            for r in resources[:5]:
                print(f"\n[{r['skill']}] {r['title']}")
                print(f"  {r['url']}")
                print(f"  {r['summary']}")
    else:
        print("Usage:")
        print("  python resources.py                    # fetch for top missing skills")
        print("  python resources.py --skill 'Tableau'  # fetch for one specific skill")
        print("  python resources.py --refresh          # ignore cache, re-fetch all")
        print("  python resources.py --list             # show stored resources")
        print()
        fetch_resources(skills=explicit_skills, skip_cache=skip_cache)
