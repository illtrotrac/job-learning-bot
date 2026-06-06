"""
Resume parser: handles PDF upload or raw text input.
Calls Claude Haiku once to extract a compact JSON profile, then stores it.
Never reprocesses the same resume (content-hash dedup).
"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from database import get_connection, init_db

load_dotenv()

MODEL = "claude-haiku-4-5"

EXTRACTION_PROMPT = """Extract a structured profile from the following resume text.
Return ONLY valid JSON with these exact keys:

{
  "name": "string",
  "title": "current or desired job title",
  "skills": ["list", "of", "technical", "skills"],
  "soft_skills": ["communication", "etc"],
  "experience_years": 0,
  "education": "highest degree + field",
  "recent_roles": [
    {"title": "...", "company": "...", "years": 0}
  ],
  "industries": ["list", "of", "industries"],
  "keywords": ["important", "domain", "keywords"]
}

Keep lists concise (max 15 items each). No markdown, no explanation, just JSON.

RESUME TEXT:
{resume_text}"""


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract raw text from a PDF file using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ImportError("Run: pip install pymupdf")

    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text.strip()


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def get_active_profile(conn: sqlite3.Connection) -> dict | None:
    """Return the currently active resume profile, or None."""
    row = conn.execute(
        "SELECT * FROM resume_profile WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row:
        return dict(row)
    return None


def call_claude_extract(resume_text: str) -> dict:
    """Send resume text to Claude Haiku and return parsed JSON profile."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Truncate to ~3000 tokens (~12000 chars) to stay cheap
    truncated = resume_text[:12000]

    response = client.messages.create(
        model=MODEL,
        max_tokens=800,
        messages=[
            {
                "role": "user",
                "content": EXTRACTION_PROMPT.replace("{resume_text}", truncated),
            }
        ],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    return json.loads(raw)


def save_manual_profile(
    title: str,
    description: str = "",
    db_path: str | None = None,
) -> dict:
    """
    Save a manually entered job title + description as the active profile.
    No API call — keywords extracted locally from the description text.
    """
    import re

    STOP_WORDS = {
        "the", "and", "for", "are", "with", "that", "this", "have", "from",
        "they", "will", "your", "what", "when", "make", "like", "time", "just",
        "know", "take", "into", "year", "good", "some", "could", "them", "see",
        "other", "than", "then", "look", "only", "come", "over", "think", "also",
        "back", "after", "use", "how", "our", "work", "first", "well", "even",
        "want", "because", "any", "these", "give", "most", "about", "you",
        "was", "there", "been", "has", "but", "not", "all", "were", "can",
        "each", "which", "their", "more", "who", "been", "said",
    }

    # Extract meaningful words: 4+ chars, not stop words, preserve original casing
    words = re.findall(r'[a-zA-Z][a-zA-Z0-9+#.]*', description)
    seen: set[str] = set()
    keywords: list[str] = []
    for w in words:
        key = w.lower()
        if len(w) >= 4 and key not in STOP_WORDS and key not in seen:
            seen.add(key)
            keywords.append(w)
        if len(keywords) == 15:
            break

    profile = {
        "name": "",
        "title": title.strip(),
        "skills": keywords[:10],
        "soft_skills": [],
        "experience_years": 0,
        "education": "",
        "recent_roles": [],
        "industries": [],
        "keywords": keywords,
    }

    init_db(db_path)
    conn = get_connection(db_path)

    raw_text = f"{title}\n{description}".strip()
    content_hash = hash_content(raw_text)

    conn.execute("UPDATE resume_profile SET is_active = 0")
    conn.execute(
        """
        INSERT INTO resume_profile (source_type, source_hash, raw_text, profile_json, is_active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(source_hash) DO UPDATE SET
            is_active = 1,
            profile_json = excluded.profile_json
        """,
        ("text", content_hash, raw_text, json.dumps(profile)),
    )
    conn.commit()
    conn.close()

    print("Profile saved (no API call):")
    print(json.dumps(profile, indent=2))
    return profile


def parse_resume(
    source: str,
    source_type: str = "text",
    db_path: str | None = None,
    force: bool = False,
) -> dict:
    """
    Parse a resume and store the extracted profile.

    Args:
        source: File path (PDF) or raw text string.
        source_type: 'pdf' or 'text'.
        db_path: Override DB path (uses .env default if None).
        force: Re-extract even if hash already exists.

    Returns:
        The profile dict stored in the database.
    """
    init_db(db_path)
    conn = get_connection(db_path)

    # Get raw text
    if source_type == "pdf":
        raw_text = extract_text_from_pdf(source)
    else:
        raw_text = source.strip()

    if not raw_text:
        raise ValueError("No text could be extracted from the provided source.")

    content_hash = hash_content(raw_text)

    # Check if we already processed this exact resume
    existing = conn.execute(
        "SELECT * FROM resume_profile WHERE source_hash = ?", (content_hash,)
    ).fetchone()

    if existing and not force:
        print(f"Resume already processed (hash match). Returning cached profile.")
        conn.close()
        return json.loads(dict(existing)["profile_json"])

    print(f"Extracting profile via Claude Haiku ({MODEL})...")
    profile = call_claude_extract(raw_text)

    # Deactivate previous active profiles
    conn.execute("UPDATE resume_profile SET is_active = 0")

    conn.execute(
        """
        INSERT INTO resume_profile (source_type, source_hash, raw_text, profile_json, is_active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(source_hash) DO UPDATE SET
            is_active = 1,
            profile_json = excluded.profile_json
        """,
        (source_type, content_hash, raw_text, json.dumps(profile)),
    )
    conn.commit()
    conn.close()

    print(f"Profile extracted and stored:")
    print(json.dumps(profile, indent=2))
    return profile


# ── CLI helper ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    args = sys.argv[1:]

    if not args:
        print("Usage:")
        print("  python parser.py resume.pdf                          # PDF (calls API)")
        print("  python parser.py --text 'Resume text...'            # raw text (calls API)")
        print("  python parser.py --job --title 'Data Analyst'       # manual, no API")
        print("  python parser.py --job --title 'ML Engineer' --description 'Python, TensorFlow...'")
        sys.exit(1)

    if args[0] == "--job":
        job_title = ""
        job_desc = ""
        i = 1
        while i < len(args):
            if args[i] == "--title" and i + 1 < len(args):
                job_title = args[i + 1]
                i += 2
            elif args[i] == "--description" and i + 1 < len(args):
                job_desc = args[i + 1]
                i += 2
            else:
                i += 1
        if not job_title:
            print("Error: --job requires --title")
            sys.exit(1)
        save_manual_profile(job_title, job_desc)

    elif args[0] == "--text":
        text_input = " ".join(args[1:])
        parse_resume(text_input, source_type="text")

    else:
        pdf_path = args[0]
        if not Path(pdf_path).exists():
            print(f"File not found: {pdf_path}")
            sys.exit(1)
        parse_resume(pdf_path, source_type="pdf")
