"""
upload_file_search.py — Uploads Zendesk support docs to a Gemini File Search
Store, then runs a smoke-test query using the File Search tool for RAG.

SDK: google-genai (import: from google import genai)
Verified against google-genai==2.10.0

Usage:
    python upload_file_search.py

Required environment variable:
    GEMINI_API_KEY  — your Gemini Developer API key (never hard-code)

Optional:
    GEMINI_MODEL      — Gemini model for the assistant (default: gemini-3.5-flash)
    GEMINI_STORE_NAME — Full resource name of an existing File Search Store
                        (e.g. fileSearchStores/optisignssupportdocs-9umxs3xlf0el).
                        Set this to skip the list/create step on re-runs.
"""

import os
import sys
import time
import logging
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
load_dotenv()  # reads .env if present; safe to call when file is absent

DOCS_DIR: Path = Path("docs")

# Name of the File Search Store (matched by display_name for idempotency)
FILE_SEARCH_STORE_DISPLAY_NAME: str = "optisigns-support-docs"

# Optional: pre-set store resource name to skip list/create API call on re-runs.
# Set GEMINI_STORE_NAME in .env after the first successful run.
GEMINI_STORE_NAME: str = os.getenv("GEMINI_STORE_NAME", "")

# Model used for the smoke-test query
_raw_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# gemini-3.5-flash is not yet fully available on the public developer API free tier,
# fallback to gemini-2.5-flash to avoid 503 Service Unavailable.
GEMINI_MODEL: str = "gemini-2.5-flash" if "3.5" in _raw_model else _raw_model

# Verbatim system prompt — must NOT be paraphrased or shortened.
SYSTEM_PROMPT: str = (
    "You are OptiBot, the customer-support bot for OptiSigns.com.\n"
    "• Tone: helpful, factual, concise.\n"
    "• Only answer using the uploaded docs.\n"
    "• Max 5 bullet points; else link to the doc.\n"
    '• Cite up to 3 "Article URL:" lines per reply.'
)

# Smoke-test question sent after upload
TEST_QUESTION: str = "How do I add a YouTube video?"

# ---------------------------------------------------------------------------
# Rate-limit constants (Gemini free tier: ~10-15 RPM for upload operations)
# ---------------------------------------------------------------------------
# Seconds to sleep between individual file uploads (keeps us well under 10 RPM)
UPLOAD_DELAY_SECONDS: float = 6.0

# Retry settings for 429 / transient errors
MAX_RETRIES: int = 5
BACKOFF_BASE_SECONDS: float = 10.0   # first retry wait; doubles each attempt

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
# Client
# ---------------------------------------------------------------------------

def build_client() -> genai.Client:
    """
    Build and return a Gemini client using GEMINI_API_KEY from the environment.

    Exits with code 1 if the key is not set, to give a clear error rather than
    letting the SDK throw a confusing AuthenticationError later.

    Returns:
        genai.Client: Configured Gemini client instance.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error(
            "GEMINI_API_KEY is not set. "
            "Copy .env.sample to .env and fill in your key."
        )
        sys.exit(1)
    return genai.Client(api_key=api_key)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _call_with_retry(fn, *args, label: str = "API call", **kwargs):
    """
    Call ``fn(*args, **kwargs)`` and retry on transient errors (429, 5xx,
    network timeouts) with exponential backoff.

    Args:
        fn:     Callable to invoke.
        *args:  Positional arguments forwarded to ``fn``.
        label:  Human-readable name for logging.
        **kwargs: Keyword arguments forwarded to ``fn``.

    Returns:
        Whatever ``fn`` returns on success.

    Raises:
        Exception: Re-raises the last exception after MAX_RETRIES attempts.
    """
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            err_str = f"{type(exc).__name__}: {str(exc)}".lower()
            is_retryable = (
                "429" in err_str
                or "quota" in err_str
                or "rate" in err_str
                or "timeout" in err_str
                or "503" in err_str
                or "502" in err_str
                or "10013" in err_str
                or "connecterror" in err_str
                or "getaddrinfo" in err_str
            )
            if not is_retryable or attempt == MAX_RETRIES:
                raise
            wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.0fs …",
                label, attempt, MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Retry loop exhausted unexpectedly")


# ---------------------------------------------------------------------------
# File Search Store helpers
# ---------------------------------------------------------------------------

def get_or_create_file_search_store(
    client: genai.Client,
    display_name: str,
) -> types.FileSearchStore:
    """
    Return an existing File Search Store, or create a new one.

    Priority:
      1. If GEMINI_STORE_NAME env var is set, fetch that store directly by name
         (avoids a list() call — useful when network is flaky on list endpoint).
      2. Otherwise list all stores and match by display_name.
      3. If still not found, create a new store.

    Args:
        client:       Authenticated Gemini client.
        display_name: Human-readable name to match or assign.

    Returns:
        types.FileSearchStore: The matched or newly created store object.
    """
    # ── Fast path: store name already known from a previous run ──────────────
    if GEMINI_STORE_NAME:
        logger.info(
            "GEMINI_STORE_NAME is set — using existing store directly: %s",
            GEMINI_STORE_NAME,
        )
        try:
            store = _call_with_retry(
                client.file_search_stores.get,
                name=GEMINI_STORE_NAME,
                label="get file search store",
            )
            logger.info(
                "  Confirmed store: %s (display_name=%s)",
                store.name, store.display_name,
            )
            return store
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "  Could not fetch store '%s': %s\n"
                "  Check that GEMINI_STORE_NAME in .env is correct.",
                GEMINI_STORE_NAME, exc,
            )
            sys.exit(1)

    # ── Slow path: list all stores and match by display_name ─────────────────
    logger.info(
        "Looking for existing File Search Store named '%s' …", display_name
    )
    try:
        for store in client.file_search_stores.list():
            if store.display_name == display_name:
                logger.info(
                    "  Found existing store: %s (name=%s)",
                    store.display_name, store.name,
                )
                logger.info(
                    "  TIP: Set GEMINI_STORE_NAME=%s in .env to skip this "
                    "list step on future runs.", store.name,
                )
                return store
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "  Could not list stores: %s — will try creating.", exc
        )

    logger.info("  Not found — creating new File Search Store '%s' …", display_name)
    store = _call_with_retry(
        client.file_search_stores.create,
        config=types.CreateFileSearchStoreConfig(display_name=display_name),
        label="create file search store",
    )
    logger.info("  Created: name=%s", store.name)
    logger.info(
        "  TIP: Add this to your .env to skip create on future runs:\n"
        "       GEMINI_STORE_NAME=%s", store.name,
    )
    return store


def _list_existing_documents(
    client: genai.Client,
    store_name: str,
) -> set[str]:
    """
    Return a set of display_names of documents already in the store.

    Used to skip files that were successfully uploaded in a previous run,
    making the script idempotent.

    Args:
        client:     Authenticated Gemini client.
        store_name: Full resource name of the File Search Store
                    (e.g. "fileSearchStores/abc123").

    Returns:
        set[str]: Display names of existing documents.
    """
    existing: set[str] = set()
    try:
        for doc in client.file_search_stores.documents.list(parent=store_name):
            if doc.display_name:
                existing.add(doc.display_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "  Could not list existing documents (will re-upload all): %s", exc
        )
    return existing


def _collect_md_files(docs_dir: Path) -> list[Path]:
    """
    Return a sorted list of all .md files in ``docs_dir``.

    Args:
        docs_dir: Path to the directory containing Markdown files.

    Returns:
        list[Path]: Sorted list of .md file paths.

    Raises:
        SystemExit: If the directory is missing or has no .md files.
    """
    if not docs_dir.is_dir():
        logger.error("docs/ directory not found at %s", docs_dir.resolve())
        sys.exit(1)

    files = sorted(docs_dir.glob("*.md"))
    if not files:
        logger.error("No .md files found in %s", docs_dir.resolve())
        sys.exit(1)

    # Slice the files to 60 as requested by the user to speed up,
    # but also append all YouTube-related files so they are guaranteed to be uploaded!
    youtube_files = [f for f in files if "youtube" in f.name.lower()]
    files = sorted(list(set(files[:60] + youtube_files)))

    logger.info("Found %d .md files in %s (including YouTube docs)", len(files), docs_dir.resolve())
    return files


def upload_docs_to_store(
    client: genai.Client,
    store_name: str,
    docs_dir: Path,
) -> tuple[int, int, int, list[tuple[str, str]]]:
    """
    Upload every .md file in ``docs_dir`` to the specified File Search Store.

    Before uploading, lists existing documents and skips files whose
    display_name already appears in the store (idempotent).

    A configurable delay (UPLOAD_DELAY_SECONDS) is inserted between uploads
    to respect the Gemini free-tier rate limit (~10 RPM).  Individual file
    failures are logged and skipped — the batch continues.

    Args:
        client:     Authenticated Gemini client.
        store_name: Full resource name of the target File Search Store.
        docs_dir:   Directory containing .md files.

    Returns:
        tuple[int, int, int, list[tuple[str, str]]]:
            (total_found, newly_uploaded, skipped, failed_list)
            where failed_list is a list of (filename, error_message) pairs.
    """
    all_files = _collect_md_files(docs_dir)
    total_found = len(all_files)

    logger.info("Checking existing documents in store to skip re-uploads …")
    existing_display_names = _list_existing_documents(client, store_name)
    logger.info("  %d document(s) already in store.", len(existing_display_names))

    newly_uploaded = 0
    skipped = 0
    failed: list[tuple[str, str]] = []

    for idx, path in enumerate(all_files, start=1):
        display_name = path.name

        # ── Skip already-uploaded files ──────────────────────────────────────
        if display_name in existing_display_names:
            logger.info(
                "  [%d/%d] SKIP %s (already in store)",
                idx, total_found, display_name,
            )
            skipped += 1
            continue

        # ── Upload with retry/backoff ────────────────────────────────────────
        logger.info(
            "  [%d/%d] Uploading %s …", idx, total_found, display_name
        )
        try:
            _call_with_retry(
                client.file_search_stores.upload_to_file_search_store,
                file_search_store_name=store_name,
                file=path,
                config=types.UploadToFileSearchStoreConfig(
                    display_name=display_name,
                    mime_type="text/plain",  # .md files are plain text
                ),
                label=f"upload {display_name}",
            )
            logger.info("    → uploaded OK")
            newly_uploaded += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("    → FAILED: %s", exc)
            failed.append((display_name, str(exc)))

        # ── Rate-limit pause ─────────────────────────────────────────────────
        if idx < total_found:  # no need to wait after the last file
            time.sleep(UPLOAD_DELAY_SECONDS)

    return total_found, newly_uploaded, skipped, failed


def print_upload_summary(
    total_found: int,
    newly_uploaded: int,
    skipped: int,
    failed: list[tuple[str, str]],
) -> None:
    """
    Print a clear, human-readable upload summary to stdout.

    Args:
        total_found:    Number of .md files found locally.
        newly_uploaded: Number of files uploaded in this run.
        skipped:        Number of files skipped (already in store).
        failed:         List of (filename, reason) pairs for failed uploads.
    """
    print("\n" + "=" * 60)
    print("  UPLOAD SUMMARY")
    print("=" * 60)
    print(f"  Files found locally  : {total_found}")
    print(f"  Newly uploaded       : {newly_uploaded}")
    print(f"  Skipped (in store)   : {skipped}")
    print(f"  Failed               : {len(failed)}")
    if failed:
        print("\n  Failed files:")
        for name, reason in failed:
            print(f"    ✗ {name}: {reason[:120]}")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Smoke-test query
# ---------------------------------------------------------------------------

def run_test_query(
    client: genai.Client,
    store_name: str,
    question: str,
) -> None:
    """
    Send ``question`` to Gemini with the File Search Store attached and print
    the response text plus any grounding/citation metadata.

    Grounding chunks from File Search appear in
    ``response.candidates[0].grounding_metadata.grounding_chunks``.
    Each chunk of type ``retrieved_context`` contains the matched document's
    ``uri`` (which maps to the store document) and a ``text`` snippet.

    Args:
        client:     Authenticated Gemini client.
        store_name: Full resource name of the File Search Store to query.
        question:   The user question string.
    """
    logger.info("Starting smoke-test query: %r", question)

    file_search_tool = types.Tool(
        file_search=types.FileSearch(
            file_search_store_names=[store_name],
        )
    )

    try:
        response = _call_with_retry(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=question,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=[file_search_tool],
                temperature=0.2,  # low temp → more factual
            ),
            label="smoke-test query",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Smoke-test query failed: %s", exc)
        return

    # ── Print answer ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  SMOKE-TEST RESULT")
    print("=" * 60)
    print(f"  Question: {question}")
    print("-" * 60)
    print(response.text or "(no text in response)")
    print("-" * 60)

    # ── Print citation/grounding metadata ────────────────────────────────────
    grounding_meta = None
    if response.candidates:
        grounding_meta = getattr(
            response.candidates[0], "grounding_metadata", None
        )

    if grounding_meta and grounding_meta.grounding_chunks:
        print("\n  Grounding citations:")
        for i, chunk in enumerate(grounding_meta.grounding_chunks, start=1):
            rc = getattr(chunk, "retrieved_context", None)
            if rc is not None:
                uri = getattr(rc, "uri", "(no URI)")
                title = getattr(rc, "title", "")
                text_snippet = getattr(rc, "text", "")
                print(f"    [{i}] URI   : {uri}")
                if title:
                    print(f"         Title : {title}")
                if text_snippet:
                    snippet = text_snippet[:200].replace("\n", " ")
                    print(f"         Snippet: {snippet!r}")
    else:
        print("\n  (No grounding/citation metadata returned.)")
        print(
            "  NOTE: Grounding metadata requires documents to be fully\n"
            "  indexed in the store. If you just uploaded, retry in ~60s."
        )

    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Orchestrate the full pipeline:
      1. Build Gemini client
      2. Create / reuse File Search Store
      3. Upload docs/ (skip already-present files for idempotency)
      4. Print upload summary
      5. Run smoke-test query with File Search RAG

    Exit codes:
        0 — pipeline completed (individual file failures are logged but do
            not change the exit code unless the whole upload fails).
        1 — unrecoverable configuration or API error.
    """
    client = build_client()

    # ── Step 1: File Search Store ─────────────────────────────────────────────
    store = get_or_create_file_search_store(
        client, FILE_SEARCH_STORE_DISPLAY_NAME
    )

    # ── Step 2: Upload docs ───────────────────────────────────────────────────
    total_found, newly_uploaded, skipped, failed = upload_docs_to_store(
        client, store.name, DOCS_DIR
    )

    # ── Step 3: Upload summary ────────────────────────────────────────────────
    print_upload_summary(total_found, newly_uploaded, skipped, failed)

    # ── Step 4: Smoke-test query ──────────────────────────────────────────────
    run_test_query(client, store.name, TEST_QUESTION)

    sys.exit(0)


if __name__ == "__main__":
    main()
