# GitHub Actions Deployment Strategy

## Why GitHub Actions?

As of 2026, free-tier cloud platforms (Render, Railway, Fly.io) no longer support **long-running scheduled jobs** at zero cost.

| Platform | Limitation |
|---|---|
| **Render** | Free tier limited to 15 min/run; full pipeline needs 30–45 min → gets killed mid-execution |
| **Railway** | Free tier ended mid-2024; requires payment |
| **Fly.io** | Complex setup, limited resources, high cost risk |
| **GitHub Actions** | ✅ **100% free** for public repos; private repos get 2,000 min/month (enough for daily ~30 min job) |

**Choice:** GitHub Actions

---

## Runners & Ephemeral Infrastructure

### What Is a Runner?

A **GitHub Actions Runner** is a temporary virtual machine (Ubuntu Linux) that GitHub spawns to execute your workflow steps.

### Ephemeral = Stateless

After workflow completion:
- Runner is **destroyed completely**
- All filesystem data is **erased**
- Next run starts on a **brand new machine**

### Problem for State

```
Run 1: Create state.json → Runner destroyed → state.json lost
Run 2: New runner has no state.json → re-upload all 405 articles
```

### Solution

After `main.py` finishes, commit `state.json` back to the GitHub repo:

```yaml
- name: Commit state.json
  run: |
    git config user.name "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    git add state.json
    if git diff --cached --quiet; then
      echo "No changes"
    else
      git commit -m "chore: update state.json"
      git push
    fi
```

Next run fetches updated `state.json` from repo → delta detection works correctly.

---

## Workflow File Structure (`.github/workflows/daily-sync.yml`)

```yaml
name: Daily OptiBot Sync
on:
  schedule:
    - cron: "0 2 * * *"  # Every day at 02:00 UTC
  workflow_dispatch:      # Manual trigger for testing

jobs:
  sync:
    runs-on: ubuntu-latest
    
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: "3.11"
      
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
      
      - name: Run sync pipeline
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          GEMINI_STORE_NAME: ${{ secrets.GEMINI_STORE_NAME }}
        run: python main.py
      
      - name: Commit updated state.json
        if: always()  # Run even if pipeline fails
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add state.json
          if git diff --cached --quiet; then
            echo "state.json unchanged"
          else
            git commit -m "chore: update state.json [skip ci]"
            git push
          fi
```

---

## Setup Instructions

### 1. Add GitHub Secrets

Go to repo **Settings** → **Secrets and variables** → **Actions**:

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | Your Gemini API key (required) |
| `GEMINI_STORE_NAME` | Existing store name, e.g., `fileSearchStores/optisignssupportdocs-xxxx` (optional but recommended) |

Secrets are **encrypted** and never appear in logs.

### 2. Verify Workflow File

Ensure `.github/workflows/daily-sync.yml` exists in repo.

### 3. Test Manually

1. Go to repo **Actions** tab
2. Click **"Daily OptiBot Sync"** workflow
3. Click **"Run workflow"** button
4. Watch logs for `SUMMARY: Added=X Updated=Y Skipped=Z Deleted=W`

### 4. View Production Logs

After scheduled run (daily at 02:00 UTC):
1. **Actions** tab → click the run
2. Click **"sync"** job
3. Expand individual steps to see full output

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| **Docker container** | Ensures same environment locally & in CI; reproducible builds |
| **Volume mount state.json** | Container runs in isolation; mount ensures state persists to runner filesystem |
| **`if: always()`** | Commit state even if pipeline fails (crashed mid-sync, state might still be valid) |
| **`[skip ci]` in commit** | Prevents commit from triggering another workflow run (avoids loops) |

---

## Troubleshooting

**Workflow doesn't start at scheduled time:**
- GitHub Actions scheduling can have 15–30 min variance
- Check **Actions** tab for queued runs

**API quota exceeded:**
- Check `GEMINI_STORE_NAME` is set (avoids re-creating store each run)
- Review Gemini API usage dashboard

**`state.json` not updating:**
- Ensure GitHub token has write permissions (default for workflows)
- Check git config is correct in workflow step
