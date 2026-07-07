# Deploy: GitHub Actions Daily Sync

This document explains how to schedule the OptiBot scraper + uploader to run
automatically every day using **GitHub Actions** (free, no credit card required).

---

## Why GitHub Actions instead of Render / Railway / Fly.io?

As of mid-2025, the free tiers of Render, Railway, and Fly.io **no longer support
scheduled/cron jobs at zero cost** for always-on or scheduled workloads.
GitHub Actions `schedule` trigger is 100% free for public repos and for private
repos within the standard free-tier minutes (2,000 min/month), which is more than
enough for one ~30-minute daily job.

---

## Prerequisites

- A GitHub account with this repo pushed.
- A Gemini API key from [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey).
- (Optional) The `GEMINI_STORE_NAME` from a previous successful run.

---

## Step 1 — Add GitHub Actions Secrets

API keys must **never** be committed to the repo. Store them as encrypted Secrets:

1. Go to your GitHub repo → **Settings** tab.
2. In the left sidebar: **Secrets and variables** → **Actions**.
3. Click **"New repository secret"** for each variable:

| Secret Name | Value |
|---|---|
| `GEMINI_API_KEY` | Your Gemini API key (required) |
| `GEMINI_STORE_NAME` | Store resource name, e.g. `fileSearchStores/optisignssupportdocs-xxxx` (optional but recommended) |

These values are encrypted and **never appear in logs** — GitHub masks them automatically.

---

## Step 2 — Verify the Workflow File Is Present

The workflow lives at `.github/workflows/daily-sync.yml` in the repo.

It runs:
- **Automatically** every day at **02:00 UTC** (`cron: "0 2 * * *"`).
- **Manually** via the Actions tab (for testing).

---

## Step 3 — Manually Trigger a Test Run

To verify everything works before waiting for the scheduled run:

1. Go to your GitHub repo → **Actions** tab.
2. In the left sidebar, click **"Daily OptiBot Sync"**.
3. Click the **"Run workflow"** button (top right of the workflow list).
4. Leave branch as `main` → click **"Run workflow"**.
5. The run will appear in the list within a few seconds. Click it to watch logs live.

---

## Step 4 — View Logs

1. Go to **Actions** tab → click the run you want to inspect.
2. Click the **"sync"** job.
3. Expand individual steps to see full output:
   - **"Run sync pipeline"** → shows all `[ADD]`, `[UPDATE]`, `[SKIP]` log lines from `main.py`.
   - Look for the final `SUMMARY: Added=X Updated=Y Skipped=Z Deleted=W` line.
4. To share a link to a specific run log: copy the URL from the browser — it looks like
   `https://github.com/<user>/<repo>/actions/runs/<run-id>`.

---

## Step 5 — How state.json Persists Between Runs

GitHub Actions runners are **ephemeral** (stateless): each run starts on a fresh
virtual machine with no memory of previous runs. Without special handling,
`state.json` would be lost after every run and every article would be re-uploaded
each day.

**Solution:** After `main.py` finishes, the workflow commits the updated `state.json`
back to the repository:

```yaml
- name: Commit updated state.json
  run: |
    git config user.name  "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    git add state.json
    if git diff --cached --quiet; then
      echo "state.json unchanged — nothing to commit."
    else
      git commit -m "chore: update state.json [skip ci]"
      git push
    fi
```

The `[skip ci]` tag in the commit message prevents this commit from triggering
another workflow run (infinite loop prevention).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Job fails with `GEMINI_API_KEY not set` | Secret not added | Add secret in Settings → Secrets |
| All articles re-uploaded every day | `state.json` not committed | Check the "Commit updated state.json" step logs |
| Job times out | Too many articles in one run | Increase timeout or add `DOCS_DIR` filtering |
| `503 UNAVAILABLE` errors | Gemini free tier spike | Retries handle this automatically; re-run if it persists |
