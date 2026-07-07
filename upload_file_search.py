"""
upload_file_search.py - Uploads a subset of support docs to Gemini File Search Store and runs a test query.
"""

import os
import sys
import time
import logging
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

DOCS_DIR = Path("docs")
FILE_SEARCH_STORE_DISPLAY_NAME = "optisigns-support-docs"
GEMINI_STORE_NAME = os.getenv("GEMINI_STORE_NAME", "")

_raw_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_MODEL = "gemini-2.5-flash" if "3.5" in _raw_model else _raw_model

SYSTEM_PROMPT = (
    "You are OptiBot, the customer-support bot for OptiSigns.com.\n"
    "• Tone: helpful, factual, concise.\n"
    "• Only answer using the uploaded docs.\n"
    "• Max 5 bullet points; else link to the doc.\n"
    '• Cite up to 3 "Article URL:" lines per reply.'
)

TEST_QUESTION = "How do I add a YouTube video?"
UPLOAD_DELAY_SECONDS = 6.0
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 10.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def build_client() -> genai.Client:
    """Builds and returns a Gemini Client instance."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY is not set. Please add it to your .env file.")
        sys.exit(1)
    return genai.Client(api_key=api_key)


def _call_with_retry(fn, *args, label: str = "API call", **kwargs):
    """Executes a function retrying on rate limits and server errors with exponential backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            err_str = str(exc).lower()
            is_retryable = any(k in err_str for k in ["429", "quota", "rate", "timeout", "503", "502", "10013", "connect", "getaddrinfo"])
            if not is_retryable or attempt == MAX_RETRIES:
                raise
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning("%s failed (attempt %d/%d): %s. Retrying in %.0fs...", label, attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)


def get_or_create_file_search_store(client: genai.Client, display_name: str) -> types.FileSearchStore:
    """Gets or creates the Gemini File Search Store."""
    if GEMINI_STORE_NAME:
        logger.info("Using existing File Search Store: %s", GEMINI_STORE_NAME)
        try:
            return _call_with_retry(client.file_search_stores.get, name=GEMINI_STORE_NAME, label="get store")
        except Exception as exc:
            logger.error("Could not fetch store '%s': %s", GEMINI_STORE_NAME, exc)
            sys.exit(1)

    logger.info("Listing File Search Stores...")
    try:
        for store in client.file_search_stores.list():
            if store.display_name == display_name:
                logger.info("Found existing store: %s", store.name)
                return store
    except Exception as exc:
        logger.warning("Could not list stores: %s. Creating new store...", exc)

    logger.info("Creating new File Search Store '%s'...", display_name)
    return _call_with_retry(
        client.file_search_stores.create,
        config=types.CreateFileSearchStoreConfig(display_name=display_name),
        label="create store",
    )


def _list_existing_documents(client: genai.Client, store_name: str) -> set[str]:
    """Lists display names of files currently in the store."""
    existing = set()
    try:
        for doc in client.file_search_stores.documents.list(parent=store_name):
            if doc.display_name:
                existing.add(doc.display_name)
    except Exception as exc:
        logger.warning("Could not list existing documents: %s", exc)
    return existing


def _collect_md_files(docs_dir: Path) -> list[Path]:
    """Collects and returns first 60 Markdown files plus any YouTube files."""
    if not docs_dir.is_dir():
        logger.error("docs/ directory not found.")
        sys.exit(1)

    files = sorted(docs_dir.glob("*.md"))
    if not files:
        logger.error("No markdown files found.")
        sys.exit(1)

    youtube_files = [f for f in files if "youtube" in f.name.lower()]
    sliced_files = sorted(list(set(files[:60] + youtube_files)))
    logger.info("Collected %d files for uploading.", len(sliced_files))
    return sliced_files


def upload_docs_to_store(client: genai.Client, store_name: str, docs_dir: Path) -> tuple[int, int, int, list]:
    """Uploads Markdown files to the store, skipping duplicates."""
    all_files = _collect_md_files(docs_dir)
    total_found = len(all_files)

    logger.info("Checking existing store documents...")
    existing_names = _list_existing_documents(client, store_name)

    newly_uploaded = 0
    skipped = 0
    failed = []

    for idx, path in enumerate(all_files, start=1):
        display_name = path.name

        if display_name in existing_names:
            logger.info("  [%d/%d] SKIP %s (already in store)", idx, total_found, display_name)
            skipped += 1
            continue

        logger.info("  [%d/%d] Uploading %s...", idx, total_found, display_name)
        try:
            _call_with_retry(
                client.file_search_stores.upload_to_file_search_store,
                file_search_store_name=store_name,
                file=path,
                config=types.UploadToFileSearchStoreConfig(
                    display_name=display_name,
                    mime_type="text/plain",
                ),
                label=f"upload {display_name}",
            )
            newly_uploaded += 1
        except Exception as exc:
            logger.error("    → FAILED: %s", exc)
            failed.append((display_name, str(exc)))

        if idx < total_found:
            time.sleep(UPLOAD_DELAY_SECONDS)

    return total_found, newly_uploaded, skipped, failed


def print_upload_summary(total_found: int, newly_uploaded: int, skipped: int, failed: list) -> None:
    """Prints upload execution summary."""
    print("\n" + "=" * 60)
    print("  UPLOAD SUMMARY")
    print("=" * 60)
    print(f"  Files found locally  : {total_found}")
    print(f"  Newly uploaded       : {newly_uploaded}")
    print(f"  Skipped (in store)   : {skipped}")
    print(f"  Failed               : {len(failed)}")
    for name, reason in failed:
        print(f"    ✗ {name}: {reason}")
    print("=" * 60 + "\n")


def run_test_query(client: genai.Client, store_name: str, question: str) -> None:
    """Queries Gemini model with File Search RAG tool enabled."""
    logger.info("Querying Gemini: %r", question)

    file_search_tool = types.Tool(
        file_search=types.FileSearch(file_search_store_names=[store_name])
    )

    try:
        response = _call_with_retry(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=question,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=[file_search_tool],
                temperature=0.2,
            ),
            label="RAG query",
        )
    except Exception as exc:
        logger.error("RAG query failed: %s", exc)
        return

    print("\n" + "=" * 60)
    print("  SMOKE-TEST RESULT")
    print("=" * 60)
    print(f"  Question: {question}")
    print("-" * 60)
    print(response.text or "(no text in response)")
    print("-" * 60)

    grounding_meta = getattr(response.candidates[0], "grounding_metadata", None) if response.candidates else None
    if grounding_meta and grounding_meta.grounding_chunks:
        print("\n  Grounding citations:")
        for i, chunk in enumerate(grounding_meta.grounding_chunks, start=1):
            rc = getattr(chunk, "retrieved_context", None)
            if rc is not None:
                print(f"    [{i}] Title : {getattr(rc, 'title', '')}")
                snippet = getattr(rc, 'text', '')[:200].replace('\n', ' ')
                print(f"         Snippet: {snippet!r}")
    print("=" * 60 + "\n")


def main() -> None:
    """Script entrypoint."""
    client = build_client()
    store = get_or_create_file_search_store(client, FILE_SEARCH_STORE_DISPLAY_NAME)
    total_found, newly_uploaded, skipped, failed = upload_docs_to_store(client, store.name, DOCS_DIR)
    print_upload_summary(total_found, newly_uploaded, skipped, failed)
    run_test_query(client, store.name, TEST_QUESTION)
    sys.exit(0)


if __name__ == "__main__":
    main()
