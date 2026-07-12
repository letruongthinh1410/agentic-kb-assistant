# Scraper Architecture — Zendesk to Markdown Pipeline

## Overview

The scraper fetches all support articles from OptiSigns' Zendesk Help Center via REST API and converts them to Markdown files for later ingestion into the Gemini File Search Store.

---

## Why Zendesk API Instead of HTML Scraping?

### Problems with Direct HTML Crawling

When fetching raw HTML from `https://support.optisigns.com/hc/en-us/articles`, you receive:
- Full page markup (header, footer, navigation, ads)
- JavaScript-rendered content
- Numerous irrelevant DOM nodes
- Risk of Cloudflare/bot protection blocking

### Zendesk API Advantage

Zendesk is a managed Help Center platform with a **public, unauthenticated REST API**:

```
GET /api/v2/help_center/en-us/articles.json
```

Returns structured JSON with title, body, URL, last updated timestamp — zero parsing required.

| Criteria | HTML Scraping | Zendesk API |
|---|---|---|
| Stability | Low (layout changes break parser) | High (schema contract) |
| Performance | Slow (parse & extract) | Fast |
| Blocking Risk | High | Very Low |
| Data Cleanliness | Requires filtering | Ready to use |

---

## Implementation Details

### Pagination Flow

```
GET /api/v2/help_center/en-us/articles.json?per_page=100
  ├─ Response includes "articles" array + "next_page" URL
  ├─ If next_page != null → fetch next page
  └─ If next_page == null → stop
```

### Per-Article Processing

For each article:
1. **Convert HTML body to Markdown** using `markdownify` library
2. **Prepend metadata header** with article URL and update timestamp
3. **Extract slug** from URL (e.g., `how-to-use-youtube-with-optisigns`)
4. **Write to file** as `docs/<slug>.md`

### Error Handling

- **Connection retries** with exponential backoff (3 attempts, 2–8 second delays)
- **Per-article error tracking** (logs count of successes vs. failures)
- **Exit code** reflects pipeline status (0 = success, 1 = critical error)

---

## Key Functions

**`fetch_all_articles(session, api_url, page_url=None)`**
- Recursive pagination through all articles
- Handles network errors with retry logic
- Returns: `List[Dict]` of article objects

**`article_to_markdown(article)`**
- Converts Zendesk JSON to Markdown format
- Prepends `Article URL:` header for citation
- Returns: formatted Markdown string

**`save_articles_to_disk(articles, output_dir)`**
- Iterates through articles, calls `article_to_markdown()`
- Writes each to `docs/<slug>.md`
- Tracks success/error counts

---

## Execution

```bash
python scraper.py
```

Output: ~405 Markdown files in `docs/` directory, each with embedded source URL.
