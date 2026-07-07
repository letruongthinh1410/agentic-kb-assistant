"""
scraper.py — Fetches all public articles from a Zendesk Help Center and
saves each one as a clean Markdown file under docs/<slug>.md.

Usage:
    python scraper.py

Environment variables (optional, for future authenticated endpoints):
    ZENDESK_TOKEN   — API token (Bearer auth); leave unset for public access.
"""

import os
import re
import sys
import time
import logging
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from markdownify import markdownify as md

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
load_dotenv()  # reads from .env if present; safe to call even if file absent

BASE_API_URL: str = (
    "https://support.optisigns.com/api/v2/help_center/en-us/articles.json"
)
ARTICLES_PER_PAGE: int = 100
OUTPUT_DIR: Path = Path("docs")

# Retry / back-off settings
MAX_RETRIES: int = 5
BACKOFF_BASE_SECONDS: float = 2.0   # wait = BACKOFF_BASE ** attempt
REQUEST_TIMEOUT_SECONDS: int = 30

# markdownify options
# Note: markdownify converts <a href> links and <img> tags by default;
# there is no 'convert_links' option in this library — that key is invalid
# and was silently ignored in the previous version.
MD_OPTIONS: dict = {
    "heading_style": "ATX",   # use # / ## / ### …
    "bullets": "-",
    "strip": ["script", "style"],  # drop script/style elements
    "newline_style": "backslash",  # preserve line breaks inside <pre> blocks
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    """
    Build a requests Session pre-configured with optional auth headers.

    Reads ZENDESK_TOKEN from the environment.  If the variable is absent the
    session is returned without auth headers (suitable for public endpoints).

    Returns:
        requests.Session: Configured HTTP session.
    """
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    token = os.getenv("ZENDESK_TOKEN")
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})
        logger.info("ZENDESK_TOKEN found — using authenticated requests.")
    else:
        logger.info("No ZENDESK_TOKEN set — using unauthenticated requests.")

    return session


def fetch_with_retry(session: requests.Session, url: str) -> dict:
    """
    Perform a GET request with exponential back-off on transient failures.

    Retries on connection errors, timeouts, and HTTP 429 / 5xx responses.
    Raises on unrecoverable errors (4xx other than 429) or after MAX_RETRIES.

    Args:
        session: A pre-configured requests.Session.
        url:     The URL to fetch.

    Returns:
        dict: Parsed JSON response body.

    Raises:
        requests.HTTPError: If a non-retryable HTTP error is received.
        RuntimeError:       If all retries are exhausted.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)

            # Rate-limited: back off and retry
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", BACKOFF_BASE_SECONDS ** attempt))
                logger.warning("Rate-limited (429). Waiting %s s before retry %d/%d …",
                               retry_after, attempt, MAX_RETRIES)
                time.sleep(retry_after)
                continue

            # Server errors: back off and retry
            if response.status_code >= 500:
                wait = BACKOFF_BASE_SECONDS ** attempt
                logger.warning("Server error %d. Waiting %.1f s before retry %d/%d …",
                               response.status_code, wait, attempt, MAX_RETRIES)
                time.sleep(wait)
                continue

            response.raise_for_status()  # raise for other 4xx
            return response.json()

        except (requests.ConnectionError, requests.Timeout) as exc:
            wait = BACKOFF_BASE_SECONDS ** attempt
            logger.warning("Network error (%s). Waiting %.1f s before retry %d/%d …",
                           exc, wait, attempt, MAX_RETRIES)
            time.sleep(wait)

    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} retries.")


def fetch_all_articles(session: requests.Session) -> list[dict]:
    """
    Paginate through the Zendesk articles endpoint and collect all articles.

    Follows the ``next_page`` cursor in each response until it is null.

    Args:
        session: A pre-configured requests.Session.

    Returns:
        list[dict]: All article objects returned by the API.
    """
    articles: list[dict] = []
    url: str | None = f"{BASE_API_URL}?per_page={ARTICLES_PER_PAGE}"
    page_number = 0

    while url:
        page_number += 1
        logger.info("Fetching page %d — %s", page_number, url)
        data = fetch_with_retry(session, url)

        page_articles = data.get("articles", [])
        articles.extend(page_articles)
        logger.info("  → %d articles on this page (total so far: %d)",
                    len(page_articles), len(articles))

        url = data.get("next_page")  # None when we've reached the last page

    return articles


def derive_slug(html_url: str, article_id: int) -> str:
    """
    Derive a filesystem-safe slug from an article's html_url.

    The Zendesk URL pattern is:
        https://support.example.com/hc/en-us/articles/<id>-some-slug-here

    We extract the part after the numeric ID (``some-slug-here``).  If no
    slug portion is found we fall back to the article ID itself.

    Args:
        html_url:   The full URL of the article.
        article_id: The numeric article ID (used as fallback).

    Returns:
        str: A URL-safe slug string.
    """
    path = urlparse(html_url).path            # e.g. /hc/en-us/articles/12345-my-title
    # Match pattern: …/articles/<digits>-<rest>  OR  …/articles/<digits>
    match = re.search(r"/articles/\d+-(.+)$", path)
    if match:
        slug = match.group(1).rstrip("/")
        # Sanitise: keep alphanumerics and hyphens only
        slug = re.sub(r"[^\w-]", "-", slug)
        return slug.lower()
    return str(article_id)


def make_absolute_links(html: str, base_url: str) -> str:
    """
    Rewrite relative ``href`` and ``src`` attributes to absolute URLs.

    Zendesk bodies sometimes contain relative links (e.g. ``/hc/en-us/…``).
    This replaces them with fully-qualified URLs using the domain of
    ``base_url``.

    Handles both double-quoted (``href="/path"``) and single-quoted
    (``href='/path'``) attribute syntax so no relative link slips through.

    Args:
        html:     Raw HTML string from the article body.
        base_url: The article's html_url (used to extract the domain).

    Returns:
        str: HTML with relative links replaced by absolute ones.
    """
    parsed = urlparse(base_url)
    domain_root = f"{parsed.scheme}://{parsed.netloc}"

    # Double-quoted: href="/..." and src="/..."
    html = re.sub(
        r'(href|src)="(/[^"]*)"',
        lambda m: f'{m.group(1)}="{domain_root}{m.group(2)}"',
        html,
    )
    # Single-quoted: href='/...' and src='/...'
    html = re.sub(
        r"(href|src)='(/[^']*)'" ,
        lambda m: f"{m.group(1)}='{domain_root}{m.group(2)}'",
        html,
    )
    return html


def html_to_markdown(html: str, base_url: str) -> str:
    """
    Convert an HTML article body to clean Markdown.

    Steps:
        1. Make relative links absolute.
        2. Run markdownify with project-specific options.
        3. Collapse excessive blank lines.

    Args:
        html:     Raw HTML string (the ``body`` field from the API).
        base_url: Article URL used to resolve relative links.

    Returns:
        str: Clean Markdown string.
    """
    html = make_absolute_links(html, base_url)
    markdown_text = md(html, **MD_OPTIONS)
    # Collapse 3+ consecutive blank lines to at most 2
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)
    return markdown_text.strip()


def build_file_content(article: dict, body_markdown: str) -> str:
    """
    Compose the full content of a .md output file.

    The mandatory header lines (Article URL and Last Updated) are always the
    first two lines so that the OpenAI Vector Store / retrieval system can
    use them for citations.

    Args:
        article:       Raw article dict from the Zendesk API.
        body_markdown: Pre-converted Markdown body.

    Returns:
        str: Complete file content ready to be written to disk.
    """
    html_url = article["html_url"]
    updated_at = article["updated_at"]
    title = article["title"]

    header = (
        f"Article URL: {html_url}\n"
        f"Last Updated: {updated_at}\n"
        f"\n"
        f"# {title}\n"
        f"\n"
    )
    return header + body_markdown + "\n"


def save_article(article: dict, output_dir: Path) -> bool:
    """
    Convert a single article and write it to a Markdown file.

    Returns True on success, False if an error occurred (errors are logged
    but do not propagate so the batch continues).

    Args:
        article:    Raw article dict from the Zendesk API.
        output_dir: Directory where .md files are written.

    Returns:
        bool: True if the file was written successfully, False otherwise.
    """
    article_id: int = article.get("id", 0)
    html_url: str = article.get("html_url", "")
    body_html: str = article.get("body") or ""
    title: str = article.get("title", f"article-{article_id}")

    try:
        body_markdown = html_to_markdown(body_html, html_url)
        content = build_file_content(article, body_markdown)

        slug = derive_slug(html_url, article_id)
        output_path = output_dir / f"{slug}.md"

        output_path.write_text(content, encoding="utf-8")
        logger.debug("Saved: %s", output_path)
        return True

    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to save article %d ('%s'): %s", article_id, title, exc)
        return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Orchestrate the full scrape-and-save pipeline.

    Exit codes:
        0 — success (all articles processed without unrecoverable error).
        1 — unrecoverable failure (e.g. could not fetch any articles).
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    session = build_session()

    try:
        articles = fetch_all_articles(session)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not fetch articles: %s", exc)
        sys.exit(1)

    if not articles:
        logger.error("No articles returned from the API — aborting.")
        sys.exit(1)

    logger.info("Total articles fetched: %d", len(articles))

    saved_count = 0
    failed_count = 0

    for article in articles:
        success = save_article(article, OUTPUT_DIR)
        if success:
            saved_count += 1
        else:
            failed_count += 1

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  SCRAPE SUMMARY")
    print("=" * 60)
    print(f"  Articles fetched : {len(articles)}")
    print(f"  Files written    : {saved_count}")
    print(f"  Failed / skipped : {failed_count}")
    print(f"  Output directory : {OUTPUT_DIR.resolve()}")
    print("=" * 60 + "\n")

    if failed_count > 0:
        logger.warning("%d article(s) could not be saved.", failed_count)

    sys.exit(0)


if __name__ == "__main__":
    main()
