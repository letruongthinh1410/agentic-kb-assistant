"""
main.py - Daily sync pipeline.
Scrapes Zendesk articles, calculates deltas, and updates the Gemini File Search Store.
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

load_dotenv()

# App settings
STATE_FILE: Path = Path(os.getenv("STATE_FILE", "state.json"))
DOCS_DIR: Path = Path(os.getenv("DOCS_DIR", "docs"))
UPLOAD_DELAY_SECONDS: float = 6.0

# Setup basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_state() -> dict:
    """Loads article tracking state from JSON file."""
    if not STATE_FILE.exists():
        logger.info("No state file found at %s. Treating all articles as new.", STATE_FILE)
        return {}
    try:
        with STATE_FILE.open(encoding="utf-8") as fh:
            state = json.load(fh)
        logger.info("Loaded state for %d article(s) from %s.", len(state), STATE_FILE)
        return state
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load state (%s), starting fresh.", exc)
        return {}


def save_state(state: dict) -> None:
    """Saves state dict atomically using a temp file."""
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
        tmp.replace(STATE_FILE)
    except OSError as exc:
        logger.error("Failed to save state: %s", exc)


# Gemini File Search Store helpers

def upload_single_file(
    client: genai.Client,
    store_name: str,
    path: Path,
) -> str | None:
    """Uploads a single markdown file to File Search Store."""
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
        doc_name = getattr(response, "name", None)
        if not doc_name:
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
    """Finds document resource name by its display name in the store."""
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
    """Deletes a document from File Search Store."""
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


# Core pipeline logic

def categorise_articles(
    current_articles: list[dict],
    state: dict,
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Compares current articles with state and groups them into added, updated, skipped, deleted."""
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
    """Scrapes, converts and uploads new articles to the store."""
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
    """Re-scrapes, deletes old document, and uploads new version for updated articles."""
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

        new_doc_name = upload_single_file(client, store_name, path)
        if new_doc_name is None:
            logger.error("  → Upload failed — old version preserved; will retry next run.")
            continue

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
    """Deletes no-longer-existent articles from the store and state."""
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


def main() -> None:
    """Daily sync pipeline main entrypoint."""
    logger.info("=" * 60)
    logger.info("  OptiBot Daily Sync — starting pipeline")
    logger.info("=" * 60)

    try:
        client = build_client()
        store = get_or_create_file_search_store(
            client,
            display_name="optisigns-support-docs",
        )
        store_name: str = store.name
        logger.info("Using File Search Store: %s", store_name)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to initialise Gemini client/store: %s", exc)
        sys.exit(1)

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

    state = load_state()
    added, updated, skipped, deleted_ids = categorise_articles(articles, state)

    MAX_UPLOADS_PER_RUN = 60
    added_to_process = added[:MAX_UPLOADS_PER_RUN]
    remaining = MAX_UPLOADS_PER_RUN - len(added_to_process)
    updated_to_process = updated[:remaining] if remaining > 0 else []

    n_added = process_added(added_to_process, client, store_name, state)
    n_updated = process_updated(updated_to_process, client, store_name, state)
    n_deleted = process_deleted(deleted_ids, client, state)
    
    n_skipped = len(skipped) + (len(added) - len(added_to_process)) + (len(updated) - len(updated_to_process))

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
