"""
Skill gap analyzer: compares active resume profile against scraped job listings.
Calls Claude Haiku once per unanalyzed job. Results stored in skill_gaps table.
Cost: ~$0.003 per job at Haiku pricing.
"""

import json
import os
import sqlite3

import anthropic
from dotenv import load_dotenv

from database import get_connection, init_db
from parser import get_active_profile

load_dotenv()

MODEL = "claude-haiku-4-5"

GAP_PROMPT = """You are a career coach. Compare this candidate profile against the job listing.
Return ONLY valid JSON, no explanation.

CANDIDATE PROFILE:
{profile}

JOB TITLE: {job_title}
JOB DESCRIPTION SNIPPET:
{snippet}

Return JSON with exactly these keys:
{
  "match_score": 0.0,
  "matched_skills": [],
  "missing_skills": [],
  "tech_stack": [],
  "summary": "one sentence"
}

Rules:
- match_score: 0.0 (no match) to 1.0 (perfect match)
- matched_skills: skills the candidate already has that the job needs
- missing_skills: skills the job needs that the candidate lacks
- tech_stack: ALL specific tools this job explicitly requires — programming languages, libraries,
  frameworks, databases, BI tools, cloud platforms, data warehouses, orchestration tools.
  Use exact product names (e.g. "Python", "SQL", "Tableau", "BigQuery", "dbt", "Airflow", "AWS").
  Do NOT include soft skills or vague terms like "analytics" or "communication".
- Keep matched_skills and missing_skills to max 10 items each; tech_stack can be up to 20
- summary: one plain sentence, no fluff"""


def get_unanalyzed_jobs(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """Fetch jobs that haven't been analyzed yet."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE is_analyzed = 0 ORDER BY scraped_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def call_haiku_analyze(profile: dict, job: dict) -> dict:
    """Call Claude Haiku to compare profile vs job. Returns structured gap dict."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Keep profile compact — only fields relevant to skill matching
    compact_profile = {
        "title": profile.get("title", ""),
        "skills": profile.get("skills", []),
        "keywords": profile.get("keywords", []),
        "experience_years": profile.get("experience_years", 0),
    }

    prompt = (
        GAP_PROMPT
        .replace("{profile}", json.dumps(compact_profile))
        .replace("{job_title}", job["title"])
        .replace("{snippet}", job["description_snippet"] or "(no description available)")
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    return json.loads(raw)


def store_gap(
    conn: sqlite3.Connection,
    job_db_id: int,
    profile_db_id: int,
    result: dict,
) -> None:
    """Save analysis result to skill_gaps and mark job as analyzed."""
    conn.execute(
        """
        INSERT INTO skill_gaps
            (job_id, resume_profile_id, match_score, matched_skills,
             missing_skills, tech_stack, analysis_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_db_id,
            profile_db_id,
            result.get("match_score", 0.0),
            json.dumps(result.get("matched_skills", [])),
            json.dumps(result.get("missing_skills", [])),
            json.dumps(result.get("tech_stack", [])),
            json.dumps(result),
        ),
    )
    conn.execute("UPDATE jobs SET is_analyzed = 1 WHERE id = ?", (job_db_id,))
    conn.commit()


def get_tech_trends(conn: sqlite3.Connection, top_n: int = 20) -> list[tuple[str, int, int]]:
    """
    Aggregate tech stack mentions across all analyzed jobs.

    Returns list of (tool_name, count, total_jobs) sorted by frequency descending.
    """
    rows = conn.execute(
        "SELECT tech_stack FROM skill_gaps WHERE tech_stack IS NOT NULL"
    ).fetchall()

    total_jobs = len(rows)
    counts: dict[str, int] = {}
    for row in rows:
        tools = json.loads(row["tech_stack"] or "[]")
        for tool in tools:
            # Normalize to title case so "python" and "Python" merge
            key = tool.strip()
            if key:
                counts[key] = counts.get(key, 0) + 1

    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [(tool, count, total_jobs) for tool, count in ranked[:top_n]]


def list_gaps(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Return recent skill gap results joined with job info."""
    rows = conn.execute(
        """
        SELECT j.title, j.company, j.location,
               g.match_score, g.missing_skills, g.matched_skills
        FROM skill_gaps g
        JOIN jobs j ON j.id = g.job_id
        ORDER BY g.analyzed_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def analyze_jobs(limit: int = 5, db_path: str | None = None) -> int:
    """
    Analyze all unanalyzed jobs against the active profile.

    Args:
        limit:   Max jobs to analyze per run (protects API budget).
        db_path: Override DB path (uses .env default if None).

    Returns:
        Number of jobs analyzed.
    """
    init_db(db_path)
    conn = get_connection(db_path)

    profile_row = get_active_profile(conn)
    if not profile_row:
        conn.close()
        raise ValueError("No active profile. Run parser.py first.")

    profile = json.loads(profile_row["profile_json"])
    profile_db_id = profile_row["id"]

    jobs = get_unanalyzed_jobs(conn, limit)
    if not jobs:
        print("No unanalyzed jobs found. Run scraper.py first.")
        conn.close()
        return 0

    print(f"Analyzing {len(jobs)} job(s) against profile: '{profile.get('title', '?')}'")
    print(f"Estimated cost: ~${len(jobs) * 0.003:.3f}\n")

    analyzed = 0
    for job in jobs:
        print(f"  [{analyzed + 1}/{len(jobs)}] {job['title']} @ {job['company']}...")
        try:
            result = call_haiku_analyze(profile, job)
            store_gap(conn, job["id"], profile_db_id, result)
            score = result.get("match_score", 0)
            missing = result.get("missing_skills", [])
            print(f"    Score: {score:.0%}  |  Missing: {', '.join(missing[:5]) or 'none'}")
            analyzed += 1
        except Exception as exc:
            print(f"    Error: {exc}")

    conn.close()
    print(f"\nDone. {analyzed} job(s) analyzed.")
    return analyzed


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    limit = 5
    show_list = False
    show_trends = False

    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--list":
            show_list = True
            i += 1
        elif args[i] == "--trends":
            show_trends = True
            i += 1
        else:
            i += 1

    if show_trends:
        conn = get_connection()
        trends = get_tech_trends(conn)
        conn.close()
        if not trends:
            print("No tech stack data yet. Run analyzer.py first.")
        else:
            total = trends[0][2] if trends else 0
            print(f"\nTech stack trends across {total} analyzed job(s):\n")
            print(f"  {'Tool':<25} {'Jobs':<6} {'Frequency'}")
            print("  " + "─" * 50)
            for tool, count, total_jobs in trends:
                bar = "█" * count
                pct = count / total_jobs * 100 if total_jobs else 0
                print(f"  {tool:<25} {count}/{total_jobs:<4}  {pct:5.0f}%  {bar}")

    elif show_list:
        conn = get_connection()
        gaps = list_gaps(conn)
        conn.close()
        if not gaps:
            print("No analysis results yet. Run analyzer.py first.")
        else:
            print(f"\n{'Score':<7} {'Title':<35} {'Company':<22} {'Missing Skills'}")
            print("─" * 100)
            for g in gaps:
                missing = json.loads(g["missing_skills"])
                print(
                    f"{g['match_score']:.0%:<7} {g['title'][:34]:<35} "
                    f"{g['company'][:21]:<22} {', '.join(missing[:4])}"
                )
    else:
        analyze_jobs(limit=limit)
