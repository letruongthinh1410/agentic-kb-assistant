# Pipeline State Management & Delta Detection

## Overview

`main.py` orchestrates a **delta detection pipeline** that:
1. Fetches current articles from Zendesk API
2. Loads previous state from `state.json`
3. Classifies each article (Added / Updated / Skipped / Deleted)
4. Performs targeted uploads/deletes in Gemini File Search Store
5. Persists updated state back to disk

---

## Why State Management?

### Without State

Every run would re-upload all 405 articles:
- Wastes Gemini API quota
- Takes 30–45 minutes per run
- Creates duplicate documents in the store

### With State

Only changed articles are re-processed:
- Fast (seconds for unchanged articles)
- Efficient API usage
- No duplicates

---

## Classification Logic

| Scenario | Condition | Action | Counted As |
|---|---|---|---|
| **New Article** | Article ID not in state | Scrape → upload → add to state | `ADDED` |
| **Updated Article** | ID in state, but `updated_at` changed | Re-scrape → upload new → delete old → update state | `UPDATED` |
| **Unchanged** | ID in state, `updated_at` identical | Skip | `SKIPPED` |
| **Deleted Article** | ID in state but missing from current API fetch | Delete from store → remove from state | `DELETED` |

### Example Timeline

```
Run 1 (Initial):
  - State: {} (empty)
  - Zendesk: 405 articles
  - Result: 405 ADDED → state.json saved

Run 2 (Next day):
  - State: 405 articles with timestamps
  - Zendesk: 405 articles (3 have newer timestamps)
  - Result: 3 UPDATED, 402 SKIPPED

Run 3 (Another day):
  - State: 405 articles
  - Zendesk: 406 articles (1 deleted, 2 new)
  - Result: 2 ADDED, 1 DELETED, 402 SKIPPED
```

---

## State File Structure

```json
{
  "360051014713": {
    "updated_at": "2026-06-18T05:00:56Z",
    "slug": "how-to-use-youtube-with-optisigns",
    "document_name": "fileSearchStores/optisignssupportdocs-xxx/documents/yyy"
  },
  ...
}
```

### Fields

- **Key**: Zendesk article ID (numeric string)
- **`updated_at`**: ISO8601 timestamp from Zendesk; used to detect changes
- **`slug`**: Human-readable filename (e.g., `how-to-use-youtube-with-optisigns`)
- **`document_name`**: Gemini File Search resource ID; needed for targeted deletion

---

## Crash-Safe Design

State is **only updated after successful operations**:

```python
# Pseudo-code
for article in articles:
    if article needs update:
        scrape()      # Network call
        upload()      # Gemini call
        delete_old()  # Gemini call
        # Only NOW update state
        state[article_id] = {...}

# Save state to disk only after all operations succeed
save_state(state)
```

If the pipeline crashes mid-run:
- Partial uploads are **idempotent** (re-running is safe)
- State file is unchanged (next run will retry)
- No data loss

---

## GitHub Actions Integration

In CI/CD, the runner is **ephemeral** (destroyed after each run):

```
Run 1 → state.json created → saved to GitHub
Run 2 → git checkout fetches previous state.json → uses it
Run 3 → continues cycle
```

Workflow commits `state.json` back to repo after each execution:

```yaml
- name: Commit updated state.json
  run: |
    git add state.json
    if git diff --cached --quiet; then
      echo "No changes"
    else
      git commit -m "chore: update state.json"
      git push
    fi
```

This ensures state **persists across ephemeral runner instances**.

---

## Key Functions

**`load_state(file_path)`**
- Reads `state.json` from disk; returns `{}` if not found (first run)

**`categorise_articles(current, state)`**
- Compares current Zendesk articles with stored state
- Returns: `{added: [...], updated: [...], skipped: [...], deleted: [...]}`

**`save_state(state, file_path)`**
- Persists state to `state.json` after all operations complete
