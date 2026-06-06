"""
Daily digest builder: assembles job matches, skill gaps, tech trends, and learning
resources into a single Telegram message and records each send in digest_history.

Supports multiple users — each user gets a digest based on their own profile.
Digests are sent on-demand via the /digest bot command.
"""

import json
import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv

from database import get_connection, init_db

load_dotenv()


# ── Data collectors (all filtered by the user's profile IDs) ─────────────────

def _get_profile_ids(conn: sqlite3.Connection, telegram_user_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM resume_profile WHERE telegram_user_id = ? AND is_active = 1",
        (telegram_user_id,),
    ).fetchall()
    return [r["id"] for r in rows]


def _top_job_matches(
    conn: sqlite3.Connection,
    profile_ids: list[int],
    limit: int = 5,
) -> list[dict]:
    if not profile_ids:
        return []
    placeholders = ",".join("?" * len(profile_ids))
    rows = conn.execute(
        f"""
        SELECT j.id, j.title, j.company, j.location, j.url,
               g.match_score, g.missing_skills
        FROM skill_gaps g
        JOIN jobs j ON j.id = g.job_id
        WHERE g.resume_profile_id IN ({placeholders})
        ORDER BY g.match_score DESC
        LIMIT ?
        """,
        (*profile_ids, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _tech_trends(
    conn: sqlite3.Connection,
    profile_ids: list[int],
    top_n: int = 8,
) -> list[tuple[str, int, int]]:
    if not profile_ids:
        return []
    placeholders = ",".join("?" * len(profile_ids))
    rows = conn.execute(
        f"SELECT tech_stack FROM skill_gaps "
        f"WHERE tech_stack IS NOT NULL AND resume_profile_id IN ({placeholders})",
        profile_ids,
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


def _top_missing_skills(
    conn: sqlite3.Connection,
    profile_ids: list[int],
    top_n: int = 6,
) -> list[tuple[str, int]]:
    if not profile_ids:
        return []
    placeholders = ",".join("?" * len(profile_ids))
    rows = conn.execute(
        f"SELECT missing_skills FROM skill_gaps "
        f"WHERE missing_skills IS NOT NULL AND resume_profile_id IN ({placeholders})",
        profile_ids,
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        for skill in json.loads(row["missing_skills"] or "[]"):
            key = skill.strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return ranked[:top_n]


def _fresh_resources(
    conn: sqlite3.Connection,
    profile_ids: list[int],
    limit: int = 4,
) -> list[dict]:
    """
    Return resources for skills the user actually has gaps in.
    Falls back to any fresh resource if no profile-specific ones exist.
    """
    # Collect the skills this user's active profile is missing
    needed: set[str] = set()
    if profile_ids:
        placeholders = ",".join("?" * len(profile_ids))
        rows = conn.execute(
            f"SELECT missing_skills FROM skill_gaps "
            f"WHERE missing_skills IS NOT NULL AND resume_profile_id IN ({placeholders})",
            profile_ids,
        ).fetchall()
        for row in rows:
            for skill in json.loads(row["missing_skills"] or "[]"):
                needed.add(skill.strip())

    if needed:
        skill_ph = ",".join("?" * len(needed))
        result = conn.execute(
            f"""
            SELECT * FROM learning_resources
            WHERE (expires_at IS NULL OR expires_at > datetime('now'))
              AND skill IN ({skill_ph})
            ORDER BY sent_count ASC, fetched_at DESC
            LIMIT ?
            """,
            (*needed, limit),
        ).fetchall()
    else:
        result = conn.execute(
            """
            SELECT * FROM learning_resources
            WHERE expires_at IS NULL OR expires_at > datetime('now')
            ORDER BY sent_count ASC, fetched_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [dict(r) for r in result]


def _already_sent_today(conn: sqlite3.Connection, telegram_user_id: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM digest_history "
        "WHERE date(sent_at) = date('now') AND telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()
    return row["cnt"] > 0


def _record_digest(
    conn: sqlite3.Connection,
    digest_json: dict,
    job_ids: list[int],
    resource_ids: list[int],
    telegram_user_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO digest_history
            (digest_json, jobs_included, resources_included, telegram_user_id)
        VALUES (?, ?, ?, ?)
        """,
        (
            json.dumps(digest_json),
            json.dumps(job_ids),
            json.dumps(resource_ids),
            telegram_user_id,
        ),
    )
    if resource_ids:
        placeholders = ",".join("?" * len(resource_ids))
        conn.execute(
            f"UPDATE learning_resources SET sent_count = sent_count + 1 "
            f"WHERE id IN ({placeholders})",
            resource_ids,
        )
    conn.commit()


# ── Formatter ─────────────────────────────────────────────────────────────────

def _bar(count: int, total: int, width: int = 8) -> str:
    filled = round(count / total * width) if total else 0
    return "█" * filled + "░" * (width - filled)


def format_digest(data: dict) -> str:
    """Format collected digest data as Telegram HTML."""
    today = datetime.now().strftime("%B %#d, %Y") if os.name == "nt" else datetime.now().strftime("%B %-d, %Y")
    lines: list[str] = []

    lines.append(f"<b>Job Learning Digest — {today}</b>")

    jobs = data.get("jobs", [])
    if jobs:
        lines.append("")
        lines.append("<b>Top Job Matches</b>")
        for i, job in enumerate(jobs, 1):
            score = job["match_score"]
            url = job.get("url", "")
            loc = f" · {job['location']}" if job.get("location") else ""
            link = f'<a href="{url}">{job["title"]}</a>' if url else job["title"]
            lines.append(f"{i}. {link} @ {job['company']}{loc} — <b>{score:.0%}</b>")
    else:
        lines.append("")
        lines.append("<i>No job matches yet. Send /run to start the pipeline.</i>")

    trends = data.get("trends", [])
    if trends:
        total_jobs = trends[0][2] if trends else 1
        lines.append("")
        lines.append(f"<b>Trending Tech Stack</b> <i>({total_jobs} jobs)</i>")
        for tool, count, total in trends:
            pct = count / total * 100 if total else 0
            lines.append(f"  {_bar(count, total)} {tool} — {pct:.0f}%")

    gaps = data.get("missing_skills", [])
    if gaps:
        lines.append("")
        lines.append("<b>Your Top Skill Gaps</b>")
        lines.append("  " + " · ".join(f"{s} ({n}x)" for s, n in gaps))

    resources = data.get("resources", [])
    if resources:
        lines.append("")
        lines.append("<b>Learn Today</b>")
        for r in resources:
            lines.append("")
            lines.append(f"<b>[{r['skill']}]</b> {r['title']}")
            url = r["url"]
            lines.append(f'<a href="{url}">{url[:60]}{"..." if len(url) > 60 else ""}</a>')
            if r.get("summary"):
                lines.append(f"<i>{r['summary']}</i>")
    else:
        lines.append("")
        lines.append("<i>No resources yet. Send /run to find some.</i>")

    lines.append("")
    lines.append("<i>Job Learning Bot · daily digest</i>")
    return "\n".join(lines)


# ── Per-user send ─────────────────────────────────────────────────────────────

def send_digest(
    telegram_user_id: str,
    chat_id: str,
    db_path: str | None = None,
    force: bool = False,
) -> bool:
    """
    Build and send a digest for one user.

    Args:
        telegram_user_id: The user whose profile and gaps to use.
        chat_id:          The Telegram chat to send the message to.
        db_path:          Override DB path.
        force:            Send even if already sent today.
    """
    from bot import send_message_to

    init_db(db_path)
    conn = get_connection(db_path)

    if not force and _already_sent_today(conn, telegram_user_id):
        print(f"[{telegram_user_id}] Digest already sent today, skipping.")
        conn.close()
        return False

    profile_ids = _get_profile_ids(conn, telegram_user_id)
    if not profile_ids:
        print(f"[{telegram_user_id}] No active profile, skipping.")
        conn.close()
        return False

    data = {
        "jobs": _top_job_matches(conn, profile_ids),
        "trends": _tech_trends(conn, profile_ids),
        "missing_skills": _top_missing_skills(conn, profile_ids),
        "resources": _fresh_resources(conn, profile_ids),
    }

    if not data["jobs"] and not data["resources"]:
        print(f"[{telegram_user_id}] Nothing to send yet.")
        conn.close()
        return False

    message = format_digest(data)
    print(f"[{telegram_user_id}] Sending digest to chat {chat_id}...")

    try:
        send_message_to(chat_id, message)
    except Exception as e:
        print(f"[{telegram_user_id}] Send failed: {e}")
        conn.close()
        return False

    resource_ids = [r["id"] for r in data["resources"]]
    job_ids = [j["id"] for j in data["jobs"] if j.get("id")]
    _record_digest(conn, data, job_ids, resource_ids, telegram_user_id)
    conn.close()
    print(f"[{telegram_user_id}] Done.")
    return True


def preview_digest(
    telegram_user_id: str | None = None,
    db_path: str | None = None,
) -> None:
    """Print the digest for a user without sending it."""
    init_db(db_path)
    conn = get_connection(db_path)

    if telegram_user_id:
        profile_ids = _get_profile_ids(conn, telegram_user_id)
    else:
        # Fallback: use all profiles (useful for CLI testing)
        rows = conn.execute("SELECT id FROM resume_profile WHERE is_active = 1").fetchall()
        profile_ids = [r["id"] for r in rows]

    data = {
        "jobs": _top_job_matches(conn, profile_ids),
        "trends": _tech_trends(conn, profile_ids),
        "missing_skills": _top_missing_skills(conn, profile_ids),
        "resources": _fresh_resources(conn, profile_ids),
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
        sys.exit(0)

    if "--preview" in args:
        preview_digest()
    else:
        print(f"Unknown argument: {args[0]}")
        sys.exit(1)
