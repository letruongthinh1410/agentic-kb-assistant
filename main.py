"""
main.py — Daily pipeline: scrape Zendesk articles, detect changes, and
synchronise the Gemini File Search Store (add / update / delete).

Usage:
    python main.py

Required environment variables:
    GEMINI_API_KEY    — Gemini Developer API key (never hard-code)

    GEMINI_STORE_NAME — Full resource name of an existing File Search Store
                        (e.g. fileSearchStores/optisignssupportdocs-xxxxx).
                        Set this to skip the list/create step on re-runs.
    GEMINI_MODEL      — Gemini model name (default: gemini-2.5-flash)
    STATE_FILE        — Path to the state JSON file (default: state.json)
    DOCS_DIR          — Directory to write Markdown files (default: docs)
    ZENDESK_TOKEN     — Optional Zendesk Bearer token for private endpoints

Exit codes:
    0 — pipeline completed successfully.
    1 — unrecoverable error (API failure, missing config, etc.).

Deployment:
    Run once locally:        python main.py
    Run via Docker:          docker run -e GEMINI_API_KEY=... <image>
    Scheduled via CI/CD:     .github/workflows/daily-sync.yml (GitHub Actions)
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Import helpers from the sibling modules.
# We use specific imports to keep the dependency surface explicit.
from scraper import (
    build_session,
    fetch_all_articles,
    save_article,
    derive_slug,
)
from upload_file_search import (
    build_client,
    get_or_create_file_search_store,
    _call_with_retry,
)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
load_dotenv()

# Path to the persistent state file (maps article_id → upload metadata).
STATE_FILE: Path = Path(os.getenv("STATE_FILE", "state.json"))

# Directory where scraped Markdown files are written.
DOCS_DIR: Path = Path(os.getenv("DOCS_DIR", "docs"))

# Seconds to sleep between individual file uploads (free-tier rate-limit).
UPLOAD_DELAY_SECONDS: float = 6.0

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
# State helpers  (crash-safe: written after every successful operation)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """
    Load the persisted state from STATE_FILE.

    The state is a dict mapping str(article_id) to a record::

        {
          "updated_at":    "2025-01-01T00:00:00Z",
          "slug":          "how-to-use-youtube-with-optisigns",
          "document_name": "fileSearchStores/.../documents/..."
        }

    Returns:
        dict: Loaded state, or an empty dict if the file does not exist.
    """
    if not STATE_FILE.exists():
        logger.info("No state file found at %s — treating all articles as new.", STATE_FILE)
        return {}
    try:
        with STATE_FILE.open(encoding="utf-8") as fh:
            state = json.load(fh)
        logger.info("Loaded state for %d article(s) from %s.", len(state), STATE_FILE)
        return state
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read state file (%s) — starting fresh.", exc)
        return {}


def save_state(state: dict) -> None:
    """
    Persist the current state dict to STATE_FILE (atomic write via temp file).

    Called after every successful add / update / delete operation so that a
    crash mid-run does not lose already-committed progress.

    Args:
        state: The full state dict to persist.
    """
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
        tmp.replace(STATE_FILE)  # atomic on POSIX; best-effort on Windows
    except OSError as exc:
        logger.error("Failed to save state: %s", exc)


# ---------------------------------------------------------------------------
# Gemini File Search Store helpers
# ---------------------------------------------------------------------------

def upload_single_file(
    client: genai.Client,
    store_name: str,
    path: Path,
) -> str | None:
    """
    Upload a single Markdown file to the Gemini File Search Store.

    Uses _call_with_retry from upload_file_search for consistent backoff
    behaviour (handles 429, 503, connection errors).

    Args:
        client:     Authenticated Gemini client.
        store_name: Full resource name of the target File Search Store.
        path:       Path to the local Markdown file.

    Returns:
        str: The document resource name (e.g. "fileSearchStores/.../documents/..."),
             or None if the upload failed.
    """
    display_name = path.name
    try:
        response = _call_with_retry(
            client.file_search_stores.upload_to_file_search_store,
            file_search_store_name=store_name,
            file=path,
            config=types.UploadToFileSearchStoreConfig(
                display_name=display_name,
                mime_type="text/plain",
            ),
            label=f"upload {display_name}",
        )
        # The SDK returns the created Document object; extract its resource name.
        doc_name = getattr(response, "name", None)
        if not doc_name:
            # Fallback: search the store for the newly created document.
            doc_name = _find_document_name(client, store_name, display_name)
        return doc_name
    except Exception as exc:  # noqa: BLE001
        logger.error("Upload failed for %s: %s", display_name, exc)
        return None


def _find_document_name(
    client: genai.Client,
    store_name: str,
    display_name: str,
) -> str | None:
    """
    Look up a document's resource name by its display_name in the store.

    Used as a fallback when the upload response does not include the name.

    Args:
        client:       Authenticated Gemini client.
        store_name:   Full resource name of the File Search Store.
        display_name: The display_name to search for.

    Returns:
        str: The document resource name, or None if not found.
    """
    try:
        for doc in client.file_search_stores.documents.list(parent=store_name):
            if doc.display_name == display_name:
                return doc.name
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not search for document '%s': %s", display_name, exc)
    return None


def delete_store_document(
    client: genai.Client,
    document_name: str,
) -> bool:
    """
    Delete a document from the Gemini File Search Store by its resource name.

    Args:
        client:        Authenticated Gemini client.
        document_name: Full resource name of the document to delete.

    Returns:
        bool: True if deleted successfully, False otherwise.
    """
    if not document_name:
        return False
    try:
        _call_with_retry(
            client.file_search_stores.documents.delete,
            name=document_name,
            label=f"delete {document_name}",
        )
        logger.info("    → Deleted document: %s", document_name)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("    → Could not delete document '%s': %s", document_name, exc)
        return False


# ---------------------------------------------------------------------------
# Core pipeline logic
# ---------------------------------------------------------------------------

def categorise_articles(
    current_articles: list[dict],
    state: dict,
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """
    Compare the current Zendesk article list against the persisted state and
    classify each article into one of four buckets.

    Args:
        current_articles: All articles returned by the Zendesk API this run.
        state:            Previously persisted state (may be empty on first run).

    Returns:
        tuple:
            added   — Articles not previously seen (new article IDs).
            updated — Articles whose updated_at timestamp changed.
            skipped — Articles with no changes.
            deleted — State keys whose article IDs are no longer in the API.
    """
    current_ids = {str(a["id"]) for a in current_articles}
    current_map = {str(a["id"]): a for a in current_articles}

    added: list[dict] = []
    updated: list[dict] = []
    skipped: list[dict] = []

    for article in current_articles:
        aid = str(article["id"])
        prev = state.get(aid)
        if prev is None:
            added.append(article)
        elif article.get("updated_at") != prev.get("updated_at"):
            updated.append(article)
        else:
            skipped.append(article)

    # Articles that existed in previous state but are gone from the API.
    deleted_ids: list[str] = [k for k in state if k not in current_ids]

    logger.info(
        "Delta: %d added, %d updated, %d skipped, %d deleted.",
        len(added), len(updated), len(skipped), len(deleted_ids),
    )
    return added, updated, skipped, deleted_ids


def process_added(
    articles: list[dict],
    client: genai.Client,
    store_name: str,
    state: dict,
) -> int:
    """
    Scrape, convert, and upload each new article to the File Search Store.

    Updates state after each successful upload (crash-safe).

    Args:
        articles:   List of new article dicts from the Zendesk API.
        client:     Authenticated Gemini client.
        store_name: Full resource name of the File Search Store.
        state:      Mutable state dict; updated in-place.

    Returns:
        int: Number of articles successfully uploaded.
    """
    success_count = 0
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    for idx, article in enumerate(articles, start=1):
        aid = str(article["id"])
        title = article.get("title", aid)
        logger.info("[ADD %d/%d] %s", idx, len(articles), title)

        ok = save_article(article, DOCS_DIR)
        if not ok:
            logger.error("  → Skipping upload (scrape/save failed).")
            continue

        slug = derive_slug(article.get("html_url", ""), article["id"])
        path = DOCS_DIR / f"{slug}.md"

        doc_name = upload_single_file(client, store_name, path)
        if doc_name is None:
            logger.error("  → Upload failed — article will be retried on next run.")
            continue

        state[aid] = {
            "updated_at": article.get("updated_at", ""),
            "slug": slug,
            "document_name": doc_name,
        }
        save_state(state)
        success_count += 1
        logger.info("  → Added OK (document: %s)", doc_name)

        if idx < len(articles):
            time.sleep(UPLOAD_DELAY_SECONDS)

    return success_count


def process_updated(
    articles: list[dict],
    client: genai.Client,
    store_name: str,
    state: dict,
) -> int:
    """
    Re-scrape, delete the old document, and upload a new version for each
    updated article.

    Upload-before-delete strategy: if the upload succeeds but the delete
    fails, the old document remains as an orphan (acceptable minor waste).
    If the upload fails, the old document is preserved and the state is
    unchanged so the article will be retried on the next run.

    Args:
        articles:   List of changed article dicts from the Zendesk API.
        client:     Authenticated Gemini client.
        store_name: Full resource name of the File Search Store.
        state:      Mutable state dict; updated in-place.

    Returns:
        int: Number of articles successfully updated.
    """
    success_count = 0
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    for idx, article in enumerate(articles, start=1):
        aid = str(article["id"])
        title = article.get("title", aid)
        old_doc_name = state.get(aid, {}).get("document_name", "")
        logger.info("[UPDATE %d/%d] %s", idx, len(articles), title)

        ok = save_article(article, DOCS_DIR)
        if not ok:
            logger.error("  → Skipping update (scrape/save failed).")
            continue

        slug = derive_slug(article.get("html_url", ""), article["id"])
        path = DOCS_DIR / f"{slug}.md"

        # Upload new version first (preserves old doc if upload fails).
        new_doc_name = upload_single_file(client, store_name, path)
        if new_doc_name is None:
            logger.error("  → Upload failed — old version preserved; will retry next run.")
            continue

        # Delete old document (best-effort; failure is non-fatal).
        if old_doc_name:
            delete_store_document(client, old_doc_name)

        state[aid] = {
            "updated_at": article.get("updated_at", ""),
            "slug": slug,
            "document_name": new_doc_name,
        }
        save_state(state)
        success_count += 1
        logger.info("  → Updated OK (new document: %s)", new_doc_name)

        if idx < len(articles):
            time.sleep(UPLOAD_DELAY_SECONDS)

    return success_count


def process_deleted(
    deleted_ids: list[str],
    client: genai.Client,
    state: dict,
) -> int:
    """
    Remove from the File Search Store any articles that no longer exist in
    the Zendesk API, then remove them from the state.

    Args:
        deleted_ids: List of article IDs (as strings) to remove.
        client:      Authenticated Gemini client.
        state:       Mutable state dict; updated in-place.

    Returns:
        int: Number of articles successfully removed.
    """
    success_count = 0
    for idx, aid in enumerate(deleted_ids, start=1):
        doc_name = state.get(aid, {}).get("document_name", "")
        slug = state.get(aid, {}).get("slug", aid)
        logger.info("[DELETE %d/%d] article_id=%s slug=%s", idx, len(deleted_ids), aid, slug)

        deleted = delete_store_document(client, doc_name)
        if deleted or not doc_name:
            state.pop(aid, None)
            save_state(state)
            success_count += 1

    return success_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Orchestrate the full daily sync pipeline:

    1. Fetch all Zendesk articles (current state).
    2. Load previous state from state.json.
    3. Categorise: added / updated / skipped / deleted.
    4. Process each category (write state.json after each success).
    5. Print a final summary.

    Exit codes:
        0 — success.
        1 — unrecoverable failure.
    """
    logger.info("=" * 60)
    logger.info("  OptiBot Daily Sync — starting pipeline")
    logger.info("=" * 60)

    # ── Step 1: Gemini client + File Search Store ────────────────────────────
    try:
        client = build_client()
        store = get_or_create_file_search_store(
            client,
            display_name="optisigns-support-docs",
        )
        store_name: str = store.name
        logger.info("Using File Search Store: %s", store_name)
    except SystemExit:
        raise  # propagate intentional exit(1) from build_client
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to initialise Gemini client/store: %s", exc)
        sys.exit(1)

    # ── Step 2: Fetch current articles from Zendesk API ──────────────────────
    try:
        session = build_session()
        articles = fetch_all_articles(session)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to fetch articles from Zendesk: %s", exc)
        sys.exit(1)

    if not articles:
        logger.error("No articles returned from Zendesk API — aborting.")
        sys.exit(1)

    logger.info("Fetched %d article(s) from Zendesk.", len(articles))

    # ── Step 3: Load previous state ──────────────────────────────────────────
    state = load_state()

    # ── Step 4: Categorise articles ──────────────────────────────────────────
    added, updated, skipped, deleted_ids = categorise_articles(articles, state)

    # ── Step 5: Process each category ────────────────────────────────────────
    n_added = process_added(added, client, store_name, state)
    n_updated = process_updated(updated, client, store_name, state)
    n_deleted = process_deleted(deleted_ids, client, state)
    n_skipped = len(skipped)

    # ── Step 6: Final summary ────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  DAILY SYNC SUMMARY")
    print("=" * 60)
    print(f"  Articles from Zendesk : {len(articles)}")
    print(f"  Added                 : {n_added}")
    print(f"  Updated               : {n_updated}")
    print(f"  Skipped (no change)   : {n_skipped}")
    print(f"  Deleted               : {n_deleted}")
    print(f"  State file            : {STATE_FILE.resolve()}")
    print("=" * 60 + "\n")

    # Exact format required by GitHub Actions workflow grep / job summary.
    summary_line = (
        f"SUMMARY: Added={n_added} Updated={n_updated} "
        f"Skipped={n_skipped} Deleted={n_deleted}"
    )
    logger.info(summary_line)
    print(summary_line)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled exception: %s", exc, exc_info=True)
        sys.exit(1)
