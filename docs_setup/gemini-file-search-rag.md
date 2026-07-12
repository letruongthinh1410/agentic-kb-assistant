# Gemini File Search & RAG Architecture

## What is RAG?

**RAG = Retrieval-Augmented Generation** — a pattern where an LLM generates answers grounded in external documents.

### Typical RAG Flow

```
User Query
    ↓
[RETRIEVE] Search knowledge base for relevant passages
    ↓
[AUGMENT] Inject passages into the prompt as context
    ↓
[GENERATE] LLM reads query + context → produces answer with citations
```

### Why RAG Instead of Raw LLM?

- **Accuracy**: Answers grounded in actual product docs (not hallucinations)
- **Freshness**: Answers reflect current documentation
- **Citation**: Every answer includes source URL

**Example:**
- Query: *"How do I add a YouTube video?"*
- System retrieves relevant OptiSigns support articles
- Gemini generates answer citing specific article URLs

---

## Gemini File Search Store

### What It Is

File Search Store is a **managed vector database** maintained by Google. When you upload a `.md` file:

1. Gemini **splits file into chunks** (~256–512 tokens, automatic)
2. Each chunk is **embedded** into a vector (e.g., 768-dimensional)
3. Vectors are **indexed** in a database inside the store

On query:
1. Query is **embedded** to same vector space
2. **Similarity search** finds top-K closest chunks (cosine similarity)
3. Chunks are **injected into generation prompt**

### Comparison: Gemini vs. OpenAI Vector Stores

| Feature | Gemini File Search | OpenAI Vector Store |
|---|---|---|
| **SDK Package** | `google-genai` | `openai` |
| **Upload API** | `client.file_search_stores.upload_to_file_search_store()` | `client.vector_stores.upload_and_poll()` |
| **Chunking** | Automatic (server-side) | Automatic (server-side) |
| **Idempotency Check** | `client.file_search_stores.documents.list()` | Manual (no built-in) |
| **Free Tier** | ✅ Yes (~10 RPM) | ❌ Requires payment |
| **Citations** | `grounding_metadata.grounding_chunks` | `annotations[].file_citation` |
| **Persistence** | ✅ Permanent | ✅ Permanent |

---

## Chunking & Embedding (Server-Side)

When `upload_to_file_search_store(file=path)` is called:

```
Raw Markdown File
    ↓
[CHUNKING] Split on sentence/paragraph boundaries (~256–512 tokens/chunk)
    ↓
[EMBEDDING] Convert each chunk to vector (Gemini embedding model)
    ↓
[INDEXING] Store vectors in internal index
```

You **cannot** customize chunking from the SDK (unlike OpenAI's older Semantic Retrieval API). Gemini handles it transparently.

### Document vs. Chunk

- **Document**: A file you upload (visible in `documents.list()`)
- **Chunk**: Sub-segments within a document (not directly exposed via SDK)

---

## Grounding & Citation

When File Search is attached to generation, the response includes `grounding_metadata`:

```python
response.grounding_metadata.grounding_chunks
# Returns list of chunks used, with document name + segment index
```

This is **automatically included** in Gemini's response. You can extract URLs from the `document_name` field and display them to users.

---

## Key Workflow

```python
from google import genai

client = genai.Client(api_key=GEMINI_API_KEY)

# 1. Create or get store
store = client.file_search_stores.create(display_name="optisignssupportdocs")

# 2. Upload markdown files
for md_file in glob("docs/*.md"):
    client.file_search_stores.upload_to_file_search_store(
        file=Path(md_file)
    )

# 3. Attach to generation
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents=[
        "How do I add a YouTube video?",
        types.Tool(file_search=types.FileSearch(
            file_search_store=store.name
        ))
    ]
)

# 4. Extract grounded citations
for chunk in response.grounding_metadata.grounding_chunks:
    print(f"Source: {chunk.document_name}")
```

---

## Advantages for OptiBot

1. **No external vector DB required** → zero infrastructure cost
2. **Auto-updated** → re-upload new files, old versions are replaced
3. **Citation-aware** → Gemini returns which documents were used
4. **Scalable** → handles 405+ documents without degradation
5. **API-first** → programmatically manage store lifecycle
