"""
Daily digest builder: assembles job matches, skill gaps, tech trends, and learning
resources into a single Telegram message and records each send in digest_history.
"""

import json
import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv

from bot import send_message
from database import get_connection, init_db

load_dotenv()


# ── Data collectors ───────────────────────────────────────────────────────────

def _top_job_matches(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        """
        SELECT j.title, j.company, j.location, j.url,
               g.match_score, g.missing_skills
        FROM skill_gaps g
        JOIN jobs j ON j.id = g.job_id
        ORDER BY g.match_score DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _tech_trends(conn: sqlite3.Connection, top_n: int = 8) -> list[tuple[str, int, int]]:
    rows = conn.execute(
        "SELECT tech_stack FROM skill_gaps WHERE tech_stack IS NOT NULL"
    ).fetchall()
    total = len(rows)
    counts: dict[str, int] = {}
    for row in rows:
        for tool in json.loads(row["tech_stack"] or "[]"):
            key = tool.strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [(tool, count, total) for tool, count in ranked[:top_n]]


def _top_missing_skills(conn: sqlite3.Connection, top_n: int = 6) -> list[tuple[str, int]]:
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
    return ranked[:top_n]


def _fresh_resources(conn: sqlite3.Connection, limit: int = 4) -> list[dict]:
    """Prefer least-sent resources so the digest stays fresh each day."""
    rows = conn.execute(
        """
        SELECT * FROM learning_resources
        WHERE expires_at IS NULL OR expires_at > datetime('now')
        ORDER BY sent_count ASC, fetched_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _already_sent_today(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM digest_history "
        "WHERE date(sent_at) = date('now')"
    ).fetchone()
    return row["cnt"] > 0


def _record_digest(
    conn: sqlite3.Connection,
    digest_json: dict,
    job_ids: list[int],
    resource_ids: list[int],
    telegram_message_id: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO digest_history (digest_json, jobs_included, resources_included, telegram_message_id)
        VALUES (?, ?, ?, ?)
        """,
        (
            json.dumps(digest_json),
            json.dumps(job_ids),
            json.dumps(resource_ids),
            telegram_message_id,
        ),
    )
    # Increment sent_count for resources included in this digest
    if resource_ids:
        placeholders = ",".join("?" * len(resource_ids))
        conn.execute(
            f"UPDATE learning_resources SET sent_count = sent_count + 1 WHERE id IN ({placeholders})",
            resource_ids,
        )
    conn.commit()


# ── Formatter ─────────────────────────────────────────────────────────────────

def _bar(count: int, total: int, width: int = 8) -> str:
    filled = round(count / total * width) if total else 0
    return "█" * filled + "░" * (width - filled)


def format_digest(data: dict) -> str:
    """Format collected digest data as Telegram HTML."""
    today = datetime.now().strftime("%B %-d, %Y") if os.name != "nt" else datetime.now().strftime("%B %#d, %Y")
    lines: list[str] = []

    lines.append(f"<b>Job Learning Digest — {today}</b>")

    # ── Job matches ──────────────────────────────────────────────────────────
    jobs = data.get("jobs", [])
    if jobs:
        lines.append("")
        lines.append("<b>Top Job Matches</b>")
        for i, job in enumerate(jobs, 1):
            score = job["match_score"]
            title = job["title"]
            company = job["company"]
            url = job.get("url", "")
            location = job.get("location", "")
            loc_str = f" · {location}" if location else ""
            link = f'<a href="{url}">{title}</a>' if url else title
            lines.append(f"{i}. {link} @ {company}{loc_str} — <b>{score:.0%}</b>")
    else:
        lines.append("")
        lines.append("<i>No job matches yet. Run scraper.py and analyzer.py first.</i>")

    # ── Tech stack trends ────────────────────────────────────────────────────
    trends = data.get("trends", [])
    if trends:
        total_jobs = trends[0][2] if trends else 1
        lines.append("")
        lines.append(f"<b>Trending Tech Stack</b> <i>(across {total_jobs} jobs)</i>")
        for tool, count, total in trends:
            pct = count / total * 100 if total else 0
            lines.append(f"  {_bar(count, total)} {tool} — {pct:.0f}%")

    # ── Skill gaps ───────────────────────────────────────────────────────────
    gaps = data.get("missing_skills", [])
    if gaps:
        lines.append("")
        lines.append("<b>Your Top Skill Gaps</b>")
        gap_strs = [f"{skill} ({count}x)" for skill, count in gaps]
        lines.append("  " + " · ".join(gap_strs))

    # ── Learning resources ───────────────────────────────────────────────────
    resources = data.get("resources", [])
    if resources:
        lines.append("")
        lines.append("<b>Learn Today</b>")
        for r in resources:
            lines.append("")
            lines.append(f"<b>[{r['skill']}]</b> {r['title']}")
            lines.append(f'<a href="{r["url"]}">{r["url"][:60]}...</a>' if len(r["url"]) > 60 else f'<a href="{r["url"]}">{r["url"]}</a>')
            if r.get("summary"):
                lines.append(f"<i>{r['summary']}</i>")
    else:
        lines.append("")
        lines.append("<i>No learning resources yet. Run resources.py first.</i>")

    lines.append("")
    lines.append("<i>Job Learning Bot · run daily</i>")

    return "\n".join(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

def send_digest(db_path: str | None = None, force: bool = False) -> bool:
    """
    Build and send today's digest.

    Args:
        db_path: Override DB path.
        force:   Send even if a digest was already sent today.

    Returns:
        True if sent, False if skipped.
    """
    init_db(db_path)
    conn = get_connection(db_path)

    if not force and _already_sent_today(conn):
        print("Digest already sent today. Use --force to send again.")
        conn.close()
        return False

    print("Building digest...")

    jobs = _top_job_matches(conn)
    trends = _tech_trends(conn)
    missing = _top_missing_skills(conn)
    resources = _fresh_resources(conn)

    if not jobs and not resources:
        print(
            "\nNothing to send yet. Make sure you have run:\n"
            "  1. python parser.py --job --title 'Your Role'\n"
            "  2. python scraper.py\n"
            "  3. python analyzer.py\n"
            "  4. python resources.py"
        )
        conn.close()
        return False

    data = {
        "jobs": jobs,
        "trends": trends,
        "missing_skills": missing,
        "resources": resources,
    }

    message = format_digest(data)

    print("\n" + "─" * 60)
    print(message)
    print("─" * 60 + "\n")

    print("Sending to Telegram...")
    try:
        send_message(message)
    except Exception as e:
        print(f"Telegram send failed: {e}")
        print("Tip: run 'python bot.py --test' to check your bot setup.")
        conn.close()
        return False

    resource_ids = [r["id"] for r in resources]
    job_ids = [j.get("id") for j in jobs if j.get("id")]
    _record_digest(conn, data, job_ids, resource_ids)

    conn.close()
    print("Digest sent and recorded.")
    return True


def preview_digest(db_path: str | None = None) -> None:
    """Print the digest without sending it."""
    init_db(db_path)
    conn = get_connection(db_path)

    data = {
        "jobs": _top_job_matches(conn),
        "trends": _tech_trends(conn),
        "missing_skills": _top_missing_skills(conn),
        "resources": _fresh_resources(conn),
    }
    conn.close()

    print(format_digest(data))


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    args = sys.argv[1:]

    if not args:
        print("Usage:")
        print("  python digest.py --preview   # print digest without sending")
        print("  python digest.py --send      # send to Telegram")
        print("  python digest.py --force     # send even if already sent today")
        print()
        print("Make sure you have completed all prior steps:")
        print("  python parser.py --job --title 'Your Role'")
        print("  python scraper.py")
        print("  python analyzer.py")
        print("  python resources.py")
        sys.exit(0)

    force = "--force" in args

    if "--preview" in args:
        preview_digest()
    elif "--send" in args or "--force" in args:
        sent = send_digest(force=force)
        sys.exit(0 if sent else 1)
    else:
        print(f"Unknown argument: {args[0]}")
        print("Run 'python digest.py' with no arguments to see usage.")
        sys.exit(1)
