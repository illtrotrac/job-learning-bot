# Job Learning Bot

A personal Telegram bot that tracks job market skill gaps for your target role and delivers on-demand learning digests.

Tell it what job you're targeting. It scrapes real listings, compares them against your profile, identifies what you're missing, and finds learning resources — all accessible through Telegram commands.

---

## What it does

1. **Profile setup** — you tell the bot your target role and skills via `/setjob`
2. **Job scraping** — pulls live listings from Google Jobs via SerpAPI
3. **Skill gap analysis** — Claude Haiku compares your profile against each job description
4. **Tech stack trends** — aggregates which tools appear most across all scraped jobs
5. **Learning resources** — Tavily finds tutorials and articles for your skill gaps, summarised by Claude
6. **On-demand digest** — `/digest` assembles everything into one formatted Telegram message

Supports multiple users. Each person who chats with the bot gets their own profile, gaps, and digest.

---

## Tech stack

| Layer | Tool |
|---|---|
| Bot | [python-telegram-bot](https://python-telegram-bot.org/) v21+ |
| LLM | Claude Haiku 4.5 (Anthropic API) |
| Job search | SerpAPI — Google Jobs engine |
| Resource search | Tavily API |
| Database | SQLite (local file) |
| Language | Python 3.11+ |

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/job-learning-bot.git
cd job-learning-bot
pip install -r requirements.txt
```

### 2. Get your API keys

| Key | Where to get it | Free tier |
|---|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | $5 free credit |
| `SERPAPI_KEY` | [serpapi.com](https://serpapi.com) | 100 searches/month |
| `TAVILY_API_KEY` | [tavily.com](https://tavily.com) | 1000 searches/month |
| `TELEGRAM_BOT_TOKEN` | Message `@BotFather` on Telegram → `/newbot` | Free |

### 3. Create your `.env`

```bash
cp .env.example .env
```

Fill in your keys:

```env
ANTHROPIC_API_KEY=your_key_here
SERPAPI_KEY=your_key_here
TAVILY_API_KEY=your_key_here
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_CHAT_ID=           # leave blank for now
DB_PATH=data/job_learning.db
```

### 4. Find your Telegram chat ID

```bash
cd src
python bot.py --get-chat-id
```

Send any message to your bot first if nothing shows up, then re-run. Add the chat ID to `.env`.

### 5. Initialise the database

```bash
cd src
python database.py
```

### 6. Start the bot

```bash
cd src
python bot.py --start
```

Open Telegram, find your bot, and send `/start`.

---

## Bot commands

| Command | What it does |
|---|---|
| `/start` | Welcome message and command list |
| `/setjob` | Set your target job title and skills (guided conversation) |
| `/run` | Run the full pipeline: scrape → analyze → find resources |
| `/digest` | Send your personalised learning digest |
| `/trends` | Show trending tech stack across all scraped jobs |
| `/status` | Show your active profile and stats |
| `/help` | Show all commands |

### Typical first-time flow

```
/setjob
→ Bot: What job title are you targeting?
→ You: Data Analyst
→ Bot: Add specific skills or send /skip
→ You: Python, SQL, Tableau
→ Bot: Profile saved. Run /run to start.

/run
→ Bot: Scraping jobs... Analyzing gaps... Finding resources...
→ Bot: Done. 8 jobs scraped, 5 analyzed, 12 resources saved.

/digest
→ Bot: [sends your full learning digest]

/trends
→ Bot: Python 90% ████████ · SQL 80% ███████░ · ...
```

---

## Cost per `/run`

| Step | API calls | Cost |
|---|---|---|
| Scrape | SerpAPI × 2 queries | Free (100/month) |
| Analyze | Claude Haiku × 5 jobs | ~$0.008 |
| Resources (first run) | Tavily + Haiku × 15 summaries | ~$0.017 |
| **First `/run`** | | **~$0.025** |
| **Repeat `/run`** (resources cached 30 days) | | **~$0.008** |

$5 of Anthropic credit covers approximately 200+ runs.

---

## Project structure

```
job-learning-bot/
├── src/
│   ├── bot.py          # Telegram bot — commands and conversation handlers
│   ├── parser.py       # Resume/profile parser (PDF or manual entry)
│   ├── scraper.py      # SerpAPI job scraper
│   ├── analyzer.py     # Claude Haiku skill gap analyzer
│   ├── resources.py    # Tavily resource finder + Haiku summarizer
│   ├── digest.py       # Digest builder and formatter
│   ├── database.py     # SQLite schema and migrations
│   └── data/
│       └── job_learning.db
├── .env.example
├── requirements.txt
└── README.md
```

---

## Adding more users

No configuration needed. Any Telegram user who messages the bot and runs `/setjob` is automatically registered with their own profile. Their `/run`, `/digest`, and `/trends` results are isolated to their own data.
