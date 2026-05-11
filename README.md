# ThreadVault

A full-stack web application designed as an advanced Reddit research and intelligence tool. ThreadVault acts as a market validation and discovery engine, automatically extracting, archiving, and analyzing vast amounts of Reddit data to identify viral growth opportunities.

## 🚀 Core Features

- **Validation Hub (Search & Extract):** Search Google for Reddit posts matching specific keywords, auto-paginate through up to 30 pages of results, and extract structured content (titles, body, scores, comments, replies) using Reddit's public JSON API.
- **Bulk Subreddit Archiver:** Background job runner capable of massive, parallel data extraction from multiple subreddits simultaneously. Features robust rate-limit handling and real-time UI progress bars.
- **Community Explorer & Database:** Tracks a curated database of 10K-1M subscriber subreddits. Includes local tools to crawl, rank, and categorize thousands of communities based on raw activity metrics.
- **Daily Intelligence AI Pipeline:** An automated pipeline that scans tracked communities for sudden viral velocity, utilizing the Gemini API to analyze market opportunity, urgency, and generate a highly detailed, formatted `.docx` intelligence briefing.

## 📁 Project Structure

```text
ThreadVault/
├── backend/
│   ├── main.py                   # FastAPI app — API endpoints + job orchestration
│   ├── google_crawler.py         # Playwright Google Search crawler
│   ├── reddit_extractor.py       # Reddit data extraction via public JSON API
│   ├── docx_generator.py         # python-docx DOCX builder
│   ├── community_discovery.py    # Subreddit crawling and ranking engine
│   └── daily_intelligence.py     # Viral tracking and AI analysis pipeline
├── data/
│   ├── community_database.json   # Primary tracker database
│   ├── db_tier*.json             # Split tier databases (Tier 1-5)
│   └── community_database_summary.txt
├── frontend/                     # Next.js / Vanilla JS Web UI components
├── static/                       # Static UI assets
├── tests/                        # Pytest suite
├── .github/workflows/            # CI/CD and 5 Tier-based Intelligence Pipelines
├── outputs/                      # Generated .docx files (auto-created locally)
├── requirements.txt
├── split_community_databases.py  # Splits the master database into 5 tiers
└── run_weekly_discovery.py       # Local discovery script
```

## 🛠️ Setup Instructions

### 1. Create and activate a virtual environment

```bash
python -m venv venv
# Windows PowerShell:
venv\Scripts\Activate.ps1
# Windows Git Bash:
source venv/Scripts/activate
# macOS / Linux:
source venv/bin/activate
```

### 2. Install dependencies

```bash
# Windows PowerShell / macOS / Linux
pip install -r requirements.txt

# Windows Git Bash
python -m pip install -r requirements.txt
```

### 3. Install Playwright browsers

```bash
# Windows PowerShell / macOS / Linux
playwright install chromium

# Windows Git Bash
python -m playwright install chromium
```

### 4. Build the Community Database

Because Reddit heavily throttles GitHub Actions IPs, community discovery must be run locally first.

```bash
# Windows PowerShell
.\venv\Scripts\python run_weekly_discovery.py

# Windows Git Bash
venv/Scripts/python run_weekly_discovery.py

# macOS / Linux
venv/bin/python run_weekly_discovery.py
```

This takes 15-25 minutes to crawl Reddit. The script will automatically run `split_community_databases.py` at the end to split the master database into 5 tiers.

Once finished, **commit** the updated `data/community_database.json` and the 5 `data/db_tier*.json` files to GitHub.

> [!TIP]
> **Fast Testing:** You can run `.\venv\Scripts\python run_weekly_discovery.py --test` (Windows PowerShell), `venv/Scripts/python run_weekly_discovery.py --test` (Windows Git Bash), or `venv/bin/python run_weekly_discovery.py --test` (Mac/Linux) to limit the crawl to 10 hardcoded subreddits and bypass the full queue, allowing you to test the logic in under 30 seconds.

### 5. Run the SaaS backend

```bash
# Windows PowerShell / CMD
.\run.ps1
# or
.\run.bat

# Windows Git Bash / macOS / Linux
./run.sh
```

Alternatively, run it manually:

```bash
uvicorn backend.main:app --reload --port 8000
```

Visit `http://localhost:8000` to open the app.

## 📡 API Endpoints

| Method | Path                            | Description                                  |
| ------ | ------------------------------- | -------------------------------------------- |
| `POST` | `/search`                       | Start a standard validation hub research job |
| `GET`  | `/status/{job_id}`              | Poll single-job progress                     |
| `GET`  | `/download/{job_id}`            | Download the generated `.docx`               |
| `POST` | `/bulk-extract`                 | Start a massive multi-subreddit archive job  |
| `GET`  | `/bulk-extract/status/{job_id}` | Poll bulk extraction progress                |

## 🤖 Daily Intelligence Pipeline & Gemini API

ThreadVault runs an automated **Daily Intelligence Pipeline** via GitHub Actions to scan your databases for trending posts. To bypass GitHub's 4-hour timeout limits, the pipeline is split into **5 autonomous, parallel tiers** based on subscriber count:

- **Tier 1:** 10K – 100K subscribers
- **Tier 2:** 100K – 200K subscribers
- **Tier 3:** 200K – 500K subscribers
- **Tier 4:** 500K – 700K subscribers
- **Tier 5:** 700K – 1M+ subscribers

All 5 pipelines run concurrently every day at 6:00 AM Dhaka time, generating 5 separate `.docx` reports.

To unlock full AI-powered analysis inside the generated reports:

1. Get an API key from Google AI Studio.
2. Go to your ThreadVault repository on GitHub.
3. Navigate to **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
4. Name: `GEMINI_API_KEY`
5. Secret: _(paste your API key here)_

### CI/CD Testing & Test Modes

The GitHub Actions workflows come with a powerful `test_mode` toggle. By triggering the pipeline manually via `workflow_dispatch` and checking the `test_mode` box:

- The pipeline isolates only 3-5 subreddits.
- Automatically bypasses API rate limits and long extraction queues.
- Generates a `⚠ TEST MODE REPORT` DOCX in under 3 minutes so you can verify backend syntax, logic, and file generation without wasting action minutes or Gemini API credits.

## 🧪 Run Tests

```bash
pytest tests/ -v
```
