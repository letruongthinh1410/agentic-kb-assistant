"""
upload_vector_store.py — Uploads Zendesk support docs to an OpenAI Vector
Store, creates (or reuses) the "OptiBot" Assistant, and runs a smoke-test
query to verify end-to-end retrieval.

Usage:
    python upload_vector_store.py

Required environment variable:
    OPENAI_API_KEY  — your OpenAI secret key (never hard-code)

Optional:
    OPENAI_MODEL    — chat model for the Assistant (default: gpt-4o-mini)
"""

import os
import sys
import time
import logging
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from openai.types import VectorStore
from openai.types.beta import Assistant

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
load_dotenv()  # reads .env if present; safe to call even when the file is absent

DOCS_DIR: Path = Path("docs")

VECTOR_STORE_NAME: str = "optisigns-support-docs"
ASSISTANT_NAME: str = "OptiBot"
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Verbatim system prompt — must NOT be paraphrased or shortened.
SYSTEM_PROMPT: str = (
    "You are OptiBot, the customer-support bot for OptiSigns.com.\n"
    "• Tone: helpful, factual, concise.\n"
    "• Only answer using the uploaded docs.\n"
    "• Max 5 bullet points; else link to the doc.\n"
    '• Cite up to 3 "Article URL:" lines per reply.'
)

# How many files to include in each upload batch sent to the API.
# The OpenAI SDK's upload_and_poll helper handles the file byte limit
# internally; this constant controls the Python-side grouping only.
UPLOAD_BATCH_SIZE: int = 50

# Seconds to wait for a Run to complete before timing out.
RUN_POLL_TIMEOUT_SECONDS: int = 120
TEST_QUESTION: str = "How do I add a YouTube video?"

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
# OpenAI client
# ---------------------------------------------------------------------------

def build_client() -> OpenAI:
    """
    Build and return an OpenAI client using OPENAI_API_KEY from the environment.

    Exits with code 1 if the key is not set, rather than letting the SDK
    raise a confusing AuthenticationError later.

    Returns:
        OpenAI: Configured client instance.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error(
            "OPENAI_API_KEY is not set. "
            "Copy .env.sample to .env and fill in your key."
        )
        sys.exit(1)
    return OpenAI(api_key=api_key)


# ---------------------------------------------------------------------------
# Vector Store helpers
# ---------------------------------------------------------------------------

def get_or_create_vector_store(client: OpenAI, name: str) -> VectorStore:
    """
    Return an existing Vector Store with ``name``, or create a new one.

    Lists all Vector Stores visible to the account and returns the first
    whose name matches exactly.  If none is found, creates a new one.

    Args:
        client: Authenticated OpenAI client.
        name:   Exact name to look up / create.

    Returns:
        VectorStore: The matching or newly created Vector Store object.
    """
    logger.info("Looking for existing Vector Store named '%s' …", name)
    # In openai v2, vector_stores moved out of .beta to the top-level client.
    for vs in client.vector_stores.list():
        if vs.name == name:
            logger.info("  Found existing Vector Store: %s (id=%s)", vs.name, vs.id)
            return vs

    logger.info("  Not found — creating new Vector Store '%s' …", name)
    vs = client.vector_stores.create(name=name)
    logger.info("  Created: id=%s", vs.id)
    return vs


def _collect_md_files(docs_dir: Path) -> list[Path]:
    """
    Return a sorted list of all .md files directly inside ``docs_dir``.

    Args:
        docs_dir: Path to the directory containing Markdown files.

    Returns:
        list[Path]: Sorted list of .md file paths.

    Raises:
        SystemExit: If the directory does not exist or contains no .md files.
    """
    if not docs_dir.is_dir():
        logger.error("docs/ directory not found at %s", docs_dir.resolve())
        sys.exit(1)

    files = sorted(docs_dir.glob("*.md"))
    if not files:
        logger.error("No .md files found in %s", docs_dir.resolve())
        sys.exit(1)

    logger.info("Found %d .md files in %s", len(files), docs_dir.resolve())
    return files


def upload_docs_to_vector_store(
    client: OpenAI,
    vector_store_id: str,
    docs_dir: Path,
) -> tuple[int, int]:
    """
    Upload every .md file in ``docs_dir`` to the specified Vector Store.

    Files are uploaded in batches of UPLOAD_BATCH_SIZE.  If a single file
    fails to open/read, it is logged and skipped — the batch continues.
    Uses ``upload_and_poll`` which blocks until OpenAI has processed each
    batch (chunked, embedded, indexed).

    Args:
        client:           Authenticated OpenAI client.
        vector_store_id:  ID of the target Vector Store.
        docs_dir:         Directory containing .md files to upload.

    Returns:
        tuple[int, int]: (total_attempted, total_succeeded) counts.
    """
    files = _collect_md_files(docs_dir)
    total_attempted = len(files)
    total_succeeded = 0

    # Split into batches
    batches = [
        files[i : i + UPLOAD_BATCH_SIZE]
        for i in range(0, len(files), UPLOAD_BATCH_SIZE)
    ]
    logger.info(
        "Uploading %d files in %d batch(es) of up to %d …",
        total_attempted,
        len(batches),
        UPLOAD_BATCH_SIZE,
    )

    for batch_idx, batch_paths in enumerate(batches, start=1):
        logger.info(
            "  Batch %d/%d — %d files …", batch_idx, len(batches), len(batch_paths)
        )

        # Open files; skip any that cannot be read
        file_handles = []
        skipped = []
        for path in batch_paths:
            try:
                file_handles.append(open(path, "rb"))  # noqa: WPS515
            except OSError as exc:
                logger.error("    Cannot open %s: %s — skipping", path.name, exc)
                skipped.append(path)

        if not file_handles:
            logger.warning("    Entire batch %d skipped (no readable files).", batch_idx)
            continue

        try:
            # In openai v2, vector_stores.file_batches is top-level (not .beta)
            batch_result = client.vector_stores.file_batches.upload_and_poll(
                vector_store_id=vector_store_id,
                files=file_handles,
            )
            counts = batch_result.file_counts
            logger.info(
                "    Batch %d done — completed=%d, failed=%d, cancelled=%d, in_progress=%d",
                batch_idx,
                counts.completed,
                counts.failed,
                counts.cancelled,
                counts.in_progress,
            )
            total_succeeded += counts.completed

        except Exception as exc:  # noqa: BLE001
            logger.error("    Batch %d failed entirely: %s", batch_idx, exc)

        finally:
            for fh in file_handles:
                try:
                    fh.close()
                except OSError:
                    pass

        if skipped:
            logger.warning(
                "    %d file(s) skipped in batch %d: %s",
                len(skipped),
                batch_idx,
                [p.name for p in skipped],
            )

    return total_attempted, total_succeeded


def print_vector_store_status(client: OpenAI, vector_store_id: str) -> None:
    """
    Fetch the Vector Store and print a human-readable status summary.

    The OpenAI API exposes per-file processing status counts
    (completed / failed / in_progress / cancelled) but does NOT expose
    the total number of text chunks/embeddings created — that detail is
    internal to the OpenAI infrastructure.

    Args:
        client:           Authenticated OpenAI client.
        vector_store_id:  ID of the Vector Store to inspect.
    """
    vs = client.vector_stores.retrieve(vector_store_id)
    fc = vs.file_counts

    print("\n" + "=" * 60)
    print("  VECTOR STORE STATUS")
    print("=" * 60)
    print(f"  Name            : {vs.name}")
    print(f"  ID              : {vs.id}")
    print(f"  Status          : {vs.status}")
    print(f"  Files completed : {fc.completed}")
    print(f"  Files failed    : {fc.failed}")
    print(f"  Files in_progress: {fc.in_progress}")
    print(f"  Files cancelled : {fc.cancelled}")
    print(f"  Total tracked   : {fc.total}")
    print(
        "  NOTE: OpenAI does not expose the raw chunk/embedding count via API.\n"
        "        The 'completed' count above is the number of successfully\n"
        "        processed and indexed files."
    )
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Assistant helpers
# ---------------------------------------------------------------------------

def get_or_create_assistant(
    client: OpenAI,
    name: str,
    vector_store_id: str,
    system_prompt: str,
    model: str,
) -> Assistant:
    """
    Return an existing Assistant with ``name``, creating one if absent.

    If an existing Assistant is found, its system prompt, model, and Vector
    Store attachment are updated to the current configuration so the script
    is idempotent.

    Args:
        client:           Authenticated OpenAI client.
        name:             Display name to search for / assign.
        vector_store_id:  ID of the Vector Store to attach to file_search.
        system_prompt:    Verbatim system prompt (not paraphrased).
        model:            OpenAI model name (e.g. "gpt-4o-mini").

    Returns:
        Assistant: The matching or newly created Assistant object.
    """
    tool_resources = {
        "file_search": {"vector_store_ids": [vector_store_id]}
    }

    logger.info("Looking for existing Assistant named '%s' …", name)
    for asst in client.beta.assistants.list(order="desc", limit=100):
        if asst.name == name:
            logger.info("  Found existing Assistant: %s (id=%s)", asst.name, asst.id)
            logger.info("  Updating system prompt, model, and vector store attachment …")
            updated = client.beta.assistants.update(
                asst.id,
                instructions=system_prompt,
                model=model,
                tools=[{"type": "file_search"}],
                tool_resources=tool_resources,
            )
            logger.info("  Assistant updated.")
            return updated

    logger.info("  Not found — creating new Assistant '%s' …", name)
    asst = client.beta.assistants.create(
        name=name,
        instructions=system_prompt,
        model=model,
        tools=[{"type": "file_search"}],
        tool_resources=tool_resources,
    )
    logger.info("  Created: id=%s", asst.id)
    return asst


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def run_test_query(client: OpenAI, assistant_id: str, question: str) -> None:
    """
    Send ``question`` to the Assistant via a one-shot Thread + Run and
    print the response, including any file-citation annotations.

    The function polls for Run completion up to RUN_POLL_TIMEOUT_SECONDS
    seconds and then gives up with an error log rather than hanging.

    Args:
        client:       Authenticated OpenAI client.
        assistant_id: ID of the Assistant to query.
        question:     The user question string.
    """
    logger.info("Starting smoke-test query: %r", question)

    # Create a fresh Thread with the question
    thread = client.beta.threads.create(
        messages=[{"role": "user", "content": question}]
    )

    # Start a Run on that Thread
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=assistant_id,
    )

    # Poll until terminal state or timeout
    deadline = time.time() + RUN_POLL_TIMEOUT_SECONDS
    while run.status in ("queued", "in_progress", "cancelling"):
        if time.time() > deadline:
            logger.error(
                "Run %s timed out after %d seconds (status=%s).",
                run.id,
                RUN_POLL_TIMEOUT_SECONDS,
                run.status,
            )
            return
        time.sleep(2)
        run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)

    if run.status != "completed":
        logger.error(
            "Run finished with unexpected status: %s. "
            "Last error: %s",
            run.status,
            getattr(run, "last_error", None),
        )
        return

    # Retrieve the assistant's reply messages
    messages = client.beta.threads.messages.list(
        thread_id=thread.id, order="asc"
    )
    assistant_messages = [m for m in messages.data if m.role == "assistant"]

    print("\n" + "=" * 60)
    print("  SMOKE-TEST RESULT")
    print("=" * 60)
    print(f"  Question: {question}")
    print("-" * 60)

    for msg in assistant_messages:
        for block in msg.content:
            if block.type == "text":
                text_value = block.text.value
                annotations = block.text.annotations

                # Replace annotation placeholders with readable citations
                citation_lines = []
                for ann in annotations:
                    if ann.type == "file_citation":
                        # The model inserted a placeholder like 【4:0†source】
                        # Replace it with a human-readable marker
                        marker = f"[Citation {annotations.index(ann) + 1}]"
                        text_value = text_value.replace(ann.text, marker)
                        # Try to surface the file name or quote
                        quote = getattr(ann.file_citation, "quote", "")
                        file_id = ann.file_citation.file_id
                        citation_lines.append(
                            f"  {marker} file_id={file_id}"
                            + (f", quote={quote[:80]!r}" if quote else "")
                        )

                print(text_value)
                if citation_lines:
                    print("\n  Citations:")
                    for line in citation_lines:
                        print(line)

    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Orchestrate the full pipeline:
      1. Build OpenAI client
      2. Create / reuse Vector Store
      3. Upload docs/
      4. Print Vector Store status
      5. Create / update Assistant
      6. Run smoke-test query

    Exit codes:
        0 — pipeline completed (individual file failures are logged but do
            not change the exit code unless the whole upload fails).
        1 — unrecoverable configuration or API error.
    """
    client = build_client()

    # ── Step 1: Vector Store ─────────────────────────────────────────────────
    vs = get_or_create_vector_store(client, VECTOR_STORE_NAME)

    # ── Step 2: Upload docs ──────────────────────────────────────────────────
    # attempted, succeeded = upload_docs_to_vector_store(client, vs.id, DOCS_DIR)
    # logger.info(
    #     "Upload complete: %d/%d files succeeded.", succeeded, attempted
    # )

    # ── Step 3: Status report ────────────────────────────────────────────────
    print_vector_store_status(client, vs.id)

    # ── Step 4: Assistant ────────────────────────────────────────────────────
    assistant = get_or_create_assistant(
        client,
        name=ASSISTANT_NAME,
        vector_store_id=vs.id,
        system_prompt=SYSTEM_PROMPT,
        model=OPENAI_MODEL,
    )

    # ── Step 5: Smoke test ───────────────────────────────────────────────────
    run_test_query(client, assistant.id, TEST_QUESTION)

    sys.exit(0)


if __name__ == "__main__":
    main()
