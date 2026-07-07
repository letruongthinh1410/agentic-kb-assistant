"""
scraper.py - Fetches Zendesk Help Center articles and converts them to Markdown files.
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

load_dotenv()

BASE_API_URL: str = (
    "https://support.optisigns.com/api/v2/help_center/en-us/articles.json"
)
ARTICLES_PER_PAGE: int = 100
OUTPUT_DIR: Path = Path("docs")

MAX_RETRIES: int = 5
BACKOFF_BASE_SECONDS: float = 2.0
REQUEST_TIMEOUT_SECONDS: int = 30

MD_OPTIONS: dict = {
    "heading_style": "ATX",
    "bullets": "-",
    "strip": ["script", "style"],
    "newline_style": "backslash",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def build_session() -> requests.Session:
    """Builds a requests Session with optional auth header from ZENDESK_TOKEN."""
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    token = os.getenv("ZENDESK_TOKEN")
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})
        logger.info("Using authenticated requests.")
    else:
        logger.info("Using unauthenticated requests.")

    return session


def fetch_with_retry(session: requests.Session, url: str) -> dict:
    """Fetches a URL with exponential backoff retries for transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", BACKOFF_BASE_SECONDS ** attempt))
                logger.warning("Rate-limited (429). Waiting %s s...", retry_after)
                time.sleep(retry_after)
                continue

            if response.status_code >= 500:
                wait = BACKOFF_BASE_SECONDS ** attempt
                logger.warning("Server error %d. Waiting %.1f s...", response.status_code, wait)
                time.sleep(wait)
                continue

            response.raise_for_status()
            return response.json()

        except (requests.ConnectionError, requests.Timeout) as exc:
            wait = BACKOFF_BASE_SECONDS ** attempt
            logger.warning("Network error (%s). Waiting %.1f s...", exc, wait)
            time.sleep(wait)

    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} retries.")


def fetch_all_articles(session: requests.Session) -> list[dict]:
    """Paginates and fetches all articles from Zendesk Help Center API."""
    articles: list[dict] = []
    url: str | None = f"{BASE_API_URL}?per_page={ARTICLES_PER_PAGE}"
    page_number = 0

    while url:
        page_number += 1
        logger.info("Fetching page %d — %s", page_number, url)
        data = fetch_with_retry(session, url)

        page_articles = data.get("articles", [])
        articles.extend(page_articles)
        logger.info("  → %d articles on this page (total: %d)", len(page_articles), len(articles))
        url = data.get("next_page")

    return articles


def derive_slug(html_url: str, article_id: int) -> str:
    """Extracts a safe filename slug from the Zendesk article URL."""
    path = urlparse(html_url).path
    match = re.search(r"/articles/\d+-(.+)$", path)
    if match:
        slug = match.group(1).rstrip("/")
        slug = re.sub(r"[^\w-]", "-", slug)
        return slug.lower()
    return str(article_id)


def make_absolute_links(html: str, base_url: str) -> str:
    """Rewrites relative links/src in HTML to absolute URLs using base_url domain."""
    parsed = urlparse(base_url)
    domain_root = f"{parsed.scheme}://{parsed.netloc}"

    html = re.sub(
        r'(href|src)="(/[^"]*)"',
        lambda m: f'{m.group(1)}="{domain_root}{m.group(2)}"',
        html,
    )
    html = re.sub(
        r"(href|src)='(/[^']*)'",
        lambda m: f"{m.group(1)}='{domain_root}{m.group(2)}'",
        html,
    )
    return html


def html_to_markdown(html: str, base_url: str) -> str:
    """Converts HTML content to clean Markdown."""
    html = make_absolute_links(html, base_url)
    markdown_text = md(html, **MD_OPTIONS)
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)
    return markdown_text.strip()


def build_file_content(article: dict, body_markdown: str) -> str:
    """Combines metadata headers and body content for the Markdown file."""
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
    """Saves a single article to a markdown file in output_dir."""
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
        return True

    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to save article %d ('%s'): %s", article_id, title, exc)
        return False


def main() -> None:
    """Scraper entrypoint."""
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
