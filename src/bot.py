"""
Telegram bot:
  - send_message(): one-shot send used by digest.py
  - run_bot():      interactive polling mode — /setjob, /run, /digest, /trends, /status
"""

import asyncio
import json
import os

from dotenv import load_dotenv
from telegram import BotCommand, ForceReply, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

MAX_MESSAGE_CHARS = 4096

# ConversationHandler states
WAITING_TITLE = 1
WAITING_DESCRIPTION = 2


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN not set.\n"
            "  1. Message @BotFather on Telegram → /newbot\n"
            "  2. Copy the token into your .env file."
        )
    return token


def _chunk(text: str, size: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split text into chunks that fit within Telegram's message limit."""
    if len(text) <= size:
        return [text]
    chunks = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, size)
        if split_at == -1:
            split_at = size
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# ── One-shot send (used by digest.py) ────────────────────────────────────────

async def _send_async(text: str, parse_mode: str) -> None:
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise ValueError(
            "TELEGRAM_CHAT_ID not set.\n"
            "  Run: python bot.py --get-chat-id"
        )
    from telegram import Bot
    async with Bot(token=_get_token()) as bot:
        for chunk in _chunk(text):
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)


def send_message(text: str, parse_mode: str = ParseMode.HTML) -> None:
    """Send a message to the configured Telegram chat (blocking)."""
    asyncio.run(_send_async(text, parse_mode))


async def _send_to_async(chat_id: str, text: str, parse_mode: str) -> None:
    from telegram import Bot
    async with Bot(token=_get_token()) as bot:
        for chunk in _chunk(text):
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)


def send_message_to(chat_id: str, text: str, parse_mode: str = ParseMode.HTML) -> None:
    """Send a message to a specific chat ID (used by digest.py for multi-user sends)."""
    asyncio.run(_send_to_async(chat_id, text, parse_mode))


# ── Chat ID discovery ─────────────────────────────────────────────────────────

async def _get_updates_async() -> list:
    from telegram import Bot
    async with Bot(token=_get_token()) as bot:
        return await bot.get_updates()


def get_chat_id() -> None:
    """
    Print chat IDs from recent messages.
    Send any message to your bot first, then run this.
    """
    try:
        updates = asyncio.run(_get_updates_async())
    except TelegramError as e:
        print(f"Telegram error: {e}")
        return

    if not updates:
        print("No messages found yet.")
        print("  1. Open Telegram and search for your bot by its @username.")
        print("  2. Send it any message (e.g. 'hello').")
        print("  3. Re-run: python bot.py --get-chat-id")
        return

    seen: set[int] = set()
    for update in updates:
        if update.message:
            chat = update.message.chat
            if chat.id not in seen:
                seen.add(chat.id)
                name = chat.username or chat.first_name or "unknown"
                print(f"Chat ID : {chat.id}")
                print(f"Name    : {name}")
                print(f"  → Add to .env: TELEGRAM_CHAT_ID={chat.id}")
                print()


# ── Profile helper ────────────────────────────────────────────────────────────

def _save_profile(title: str, description: str, user_id: str, chat_id: str) -> None:
    """Save a manual profile to the DB (no API call)."""
    from parser import save_manual_profile
    save_manual_profile(title, description, telegram_user_id=user_id, telegram_chat_id=chat_id)


# ── Interactive command handlers ──────────────────────────────────────────────

HELP_TEXT = (
    "<b>Commands:</b>\n"
    "/setjob  — set your target job title and skills\n"
    "/run     — scrape jobs, analyze gaps, find resources\n"
    "/digest  — send today's learning digest\n"
    "/trends  — show trending tech stack across scraped jobs\n"
    "/status  — show current profile and database stats\n"
    "/help    — show this message\n"
    "/cancel  — cancel current operation"
)

WELCOME_TEXT = (
    "<b>Welcome to Job Learning Bot!</b>\n\n"
    "I track job market skill gaps for your target role and send you "
    "a daily digest of learning resources — straight to Telegram.\n\n"
    "<b>Get started in 3 steps:</b>\n"
    "1. /setjob — tell me what role you're targeting\n"
    "2. /run — scrape jobs and analyze skill gaps\n"
    "3. /digest — receive today's learning digest\n\n"
    + HELP_TEXT
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Greet new users; show a shorter help reminder for returning users."""
    from database import get_connection
    conn = get_connection()
    has_profile = conn.execute(
        "SELECT COUNT(*) AS n FROM resume_profile WHERE is_active = 1"
    ).fetchone()["n"] > 0
    conn.close()

    if has_profile:
        # Returning user — skip the long intro
        await update.message.reply_text(
            "Welcome back!\n\n" + HELP_TEXT + "\n\nTap / to see all commands.",
            parse_mode=ParseMode.HTML,
        )
    else:
        # First time — full onboarding welcome
        await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to any non-command message with a short nudge."""
    await update.message.reply_text(
        "I only respond to commands. Send /help to see what I can do.",
    )


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch unrecognised commands."""
    await update.message.reply_text(
        "Unknown command. Send /help to see all available commands.",
    )


# /setjob conversation ─────────────────────────────────────────────────────────

async def cmd_setjob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "What job title are you targeting?\n\n"
        "<i>Examples: Data Analyst · ML Engineer · Backend Developer · DevOps Engineer</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. Data Analyst"),
    )
    return WAITING_TITLE


async def received_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("Please enter a job title to continue.")
        return WAITING_TITLE

    context.user_data["job_title"] = title
    await update.message.reply_text(
        f"Got it — <b>{title}</b>.\n\n"
        "Now add specific skills or tools you already have, or want to focus on.\n"
        "<i>Example: Python, SQL, Tableau, AWS, dbt</i>\n\n"
        "Or send /skip to use just the job title.",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. Python, SQL, Tableau"),
    )
    return WAITING_DESCRIPTION


async def received_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    description = update.message.text.strip()
    title = context.user_data.get("job_title", "")
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    _save_profile(title, description, user_id, chat_id)

    await update.message.reply_text(
        f"Profile saved.\n\n"
        f"<b>Title:</b> {title}\n"
        f"<b>Skills:</b> {description}\n\n"
        "Ready to go. Send /run to scrape jobs and analyze skill gaps.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def skip_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = context.user_data.get("job_title", "")
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    _save_profile(title, "", user_id, chat_id)

    await update.message.reply_text(
        f"Profile saved with title: <b>{title}</b>\n\n"
        "Send /run to scrape jobs and analyze skill gaps.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled. Send /setjob to start over.")
    return ConversationHandler.END


# /run — full pipeline ─────────────────────────────────────────────────────────

async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loop = asyncio.get_event_loop()
    await update.message.reply_text(
        "Starting pipeline. This may take a minute or two...\n\n"
        "<i>Step 1/3: Scraping jobs</i>",
        parse_mode=ParseMode.HTML,
    )

    user_id = str(update.effective_user.id)

    try:
        from scraper import scrape_jobs
        new_jobs = await loop.run_in_executor(None, scrape_jobs)
        await update.message.reply_text(
            f"<i>Step 1/3 done — {new_jobs} new job(s) found.\nStep 2/3: Analyzing skill gaps</i>",
            parse_mode=ParseMode.HTML,
        )

        from analyzer import analyze_jobs
        analyzed = await loop.run_in_executor(
            None, lambda: analyze_jobs(telegram_user_id=user_id)
        )
        await update.message.reply_text(
            f"<i>Step 2/3 done — {analyzed} job(s) analyzed.\nStep 3/3: Finding learning resources</i>",
            parse_mode=ParseMode.HTML,
        )

        from resources import fetch_resources
        saved = await loop.run_in_executor(
            None, lambda: fetch_resources(telegram_user_id=user_id)
        )

        await update.message.reply_text(
            f"Pipeline complete.\n\n"
            f"<b>New jobs scraped:</b> {new_jobs}\n"
            f"<b>Jobs analyzed:</b> {analyzed}\n"
            f"<b>Resources saved:</b> {saved}\n\n"
            "Run /digest to receive today's learning digest\n"
            "or /trends to see the trending tech stack.",
            parse_mode=ParseMode.HTML,
        )

    except ValueError as e:
        # Likely no active profile
        await update.message.reply_text(
            f"Could not run pipeline: {e}\n\n"
            "Make sure you have set a job profile first with /setjob.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(
            f"Something went wrong: {e}\n\n"
            "<i>Check that your SERPAPI_KEY and ANTHROPIC_API_KEY are set in .env.</i>",
            parse_mode=ParseMode.HTML,
        )


# /digest ──────────────────────────────────────────────────────────────────────

async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Building your digest...")
    loop = asyncio.get_event_loop()
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    try:
        from digest import send_digest
        sent = await loop.run_in_executor(
            None,
            lambda: send_digest(telegram_user_id=user_id, chat_id=chat_id, force=True),
        )
        if not sent:
            await update.message.reply_text(
                "Nothing to send yet. Send /run first to scrape jobs and find resources."
            )
    except Exception as e:
        await update.message.reply_text(f"Error sending digest: {e}")


# /trends ──────────────────────────────────────────────────────────────────────

async def cmd_trends(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from database import get_connection
    user_id = str(update.effective_user.id)
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT g.tech_stack FROM skill_gaps g
        JOIN resume_profile p ON p.id = g.resume_profile_id
        WHERE g.tech_stack IS NOT NULL
          AND p.telegram_user_id = ?
          AND p.is_active = 1
        """,
        (user_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "No trend data for your profile yet. Send /run first to analyze jobs."
        )
        return

    total = len(rows)
    counts: dict[str, int] = {}
    for row in rows:
        for tool in json.loads(row["tech_stack"] or "[]"):
            key = tool.strip()
            if key:
                counts[key] = counts.get(key, 0) + 1

    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:15]
    lines = [f"<b>Trending Tech Stack</b> <i>(from {total} analyzed jobs)</i>\n"]
    for tool, count in ranked:
        pct = count / total * 100
        filled = round(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)
        lines.append(f"{bar} <b>{tool}</b> — {pct:.0f}%")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# /status ──────────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from database import get_connection
    user_id = str(update.effective_user.id)
    conn = get_connection()

    profile_row = conn.execute(
        "SELECT profile_json FROM resume_profile WHERE is_active = 1 AND telegram_user_id = ? LIMIT 1",
        (user_id,),
    ).fetchone()

    jobs_total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    gaps_total = conn.execute(
        "SELECT COUNT(*) AS n FROM skill_gaps g "
        "JOIN resume_profile p ON p.id = g.resume_profile_id WHERE p.telegram_user_id = ?",
        (user_id,),
    ).fetchone()["n"]
    resources_total = conn.execute("SELECT COUNT(*) AS n FROM learning_resources").fetchone()["n"]
    last_digest = conn.execute(
        "SELECT sent_at FROM digest_history WHERE telegram_user_id = ? ORDER BY sent_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()

    if profile_row:
        profile = json.loads(profile_row["profile_json"])
        profile_str = (
            f"<b>{profile.get('title', 'Unknown')}</b>\n"
            f"  Skills: {', '.join(profile.get('skills', [])[:6]) or 'none'}"
        )
    else:
        profile_str = "<i>No profile set. Use /setjob to get started.</i>"

    digest_str = last_digest["sent_at"] if last_digest else "Never"

    await update.message.reply_text(
        f"<b>Your Status</b>\n\n"
        f"<b>Active Profile:</b>\n{profile_str}\n\n"
        f"<b>Your Stats:</b>\n"
        f"  Jobs in database  : {jobs_total}\n"
        f"  Your skill gaps   : {gaps_total}\n"
        f"  Learning resources: {resources_total}\n\n"
        f"<b>Last digest sent:</b> {digest_str}",
        parse_mode=ParseMode.HTML,
    )


# ── Bot startup ───────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Register commands in Telegram's native '/' menu on startup."""
    await app.bot.set_my_commands([
        BotCommand("setjob",  "Set your target job title and skills"),
        BotCommand("run",     "Scrape jobs, analyze gaps, find resources"),
        BotCommand("digest",  "Send today's learning digest"),
        BotCommand("trends",  "Show trending tech stack"),
        BotCommand("status",  "Show current profile and stats"),
        BotCommand("help",    "Show all commands"),
    ])


def run_bot() -> None:
    """Start the interactive Telegram bot (blocking)."""
    token = _get_token()

    app = Application.builder().token(token).post_init(_post_init).build()

    # /setjob multi-step conversation
    setjob_handler = ConversationHandler(
        entry_points=[CommandHandler("setjob", cmd_setjob)],
        states={
            WAITING_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_title)
            ],
            WAITING_DESCRIPTION: [
                CommandHandler("skip", skip_description),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_description),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(setjob_handler)
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("trends", cmd_trends))
    app.add_handler(CommandHandler("status", cmd_status))

    # Catch-all handlers — same group 0, registered LAST so they only fire
    # when no earlier handler (including the ConversationHandler) matched.
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))

    print("Bot is running. Press Ctrl+C to stop.")
    print("Open Telegram and message your bot to get started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


# ── Test message ──────────────────────────────────────────────────────────────

def _test_message() -> str:
    return (
        "<b>Job Learning Bot — test message</b>\n\n"
        "Setup is working correctly.\n\n"
        "<b>Available commands:</b>\n"
        "/setjob  — set your target job title\n"
        "/run     — run the full pipeline\n"
        "/digest  — get today's learning digest\n"
        "/trends  — see trending tech stack\n"
        "/status  — show current stats"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    args = sys.argv[1:]

    if not args:
        print("Usage:")
        print("  python bot.py --start          # run interactive bot (keep terminal open)")
        print("  python bot.py --get-chat-id    # find your Telegram chat ID")
        print("  python bot.py --test           # send a test message")
        print("  python bot.py --message 'text' # send a custom message")
        sys.exit(0)

    if args[0] == "--start":
        run_bot()

    elif args[0] == "--get-chat-id":
        get_chat_id()

    elif args[0] == "--test":
        print("Sending test message...")
        try:
            send_message(_test_message())
            print("Sent. Check your Telegram.")
        except Exception as e:
            print(f"Error: {e}")

    elif args[0] == "--message" and len(args) > 1:
        text = " ".join(args[1:])
        try:
            send_message(text, parse_mode=ParseMode.MARKDOWN)
            print("Sent.")
        except Exception as e:
            print(f"Error: {e}")

    else:
        print(f"Unknown argument: {args[0]}")
        sys.exit(1)
