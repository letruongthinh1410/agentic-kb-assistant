# AlphaSphere Knowledge Assistant

AI support-bot powered by Gemini RAG — scrapes 405 Zendesk articles, uploads
them to a Gemini File Search Store, and answers questions with cited URLs.

---

## Setup

```bash
git clone https://github.com/letruongthinh1410/agentic-kb-assistant.git && cd agentic-kb-assistant
python -m venv venv && venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp .env.sample .env   # then fill in GEMINI_API_KEY
```

**Required env vars** (see `.env.sample`):

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | Free key from aistudio.google.com/apikey |
| `GEMINI_STORE_NAME` | Existing store name (skip create on re-runs) |

---

## How to Run Locally

```bash
# 1. Scrape articles → docs/*.md
python scraper.py

# 2. Upload docs → Gemini File Search Store
python upload_file_search.py

# 3. Full pipeline (scrape + delta-detect + upload)
python main.py
```

**Via Docker (same image used in CI):**

```bash
docker build -t optibot .

# PowerShell:
docker run --rm `
  -e GEMINI_API_KEY="your-key" `
  -e STATE_FILE=/app/state/state.json `
  -v "${PWD}:/app/state" optibot
```

Container runs once and exits 0 on success.

---

## Chunking Strategy

Chunking is handled **automatically by Gemini's File Search Store** — no
manual splitting needed. Each article is uploaded as one `.md` file; Gemini
chunks and embeds it server-side. Every file begins with `Article URL: <url>`
so replies always include a citable source.

---

## Daily Sync

Runs via **GitHub Actions** (`.github/workflows/daily-sync.yml`), not
Render/Railway/Fly.io — those no longer offer a free tier for cron jobs.

**Daily job logs:**
> `https://github.com/letruongthinh1410/agentic-kb-assistant/actions/runs/28889263778/job/85697364199`

---

## Screenshot

![Assistant answering "How do I add a YouTube video?"](docs_setup/result.png)

---

## Technical Documentation

| Doc | Topic |
|---|---|
| [scraper-architecture.md](docs_setup/scraper-architecture.md) | Zendesk API → Markdown, pagination, retry logic |
| [gemini-file-search-rag.md](docs_setup/gemini-file-search-rag.md) | RAG pattern, File Search Store, chunking, embeddings |
| [pipeline-state-management.md](docs_setup/pipeline-state-management.md) | Delta detection, state.json, crash-safe design |
| [docker-containerization.md](docs_setup/docker-containerization.md) | Dockerfile, volume mounts, local testing |
| [github-actions-deployment.md](docs_setup/github-actions-deployment.md) | GitHub Actions workflow, secrets, scheduled runs, ephemeral runners |
