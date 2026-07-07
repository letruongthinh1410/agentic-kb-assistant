# Giải thích kỹ thuật: Gemini File Search Store & RAG

> Tài liệu này viết cho lập trình viên junior đọc trước buổi phỏng vấn kỹ thuật.  
> Mục tiêu: hiểu rõ tại sao hệ thống được thiết kế như vậy, không chỉ là "code chạy được".

---

## 1. RAG là gì?

**RAG = Retrieval-Augmented Generation** (Sinh văn bản có tăng cường tìm kiếm).

Khi bạn hỏi một câu hỏi, thay vì để model AI trả lời chỉ dựa vào kiến thức được huấn luyện sẵn (có thể lỗi thời hoặc không biết về sản phẩm của bạn), hệ thống RAG sẽ:

```
1. RETRIEVE  → Tìm kiếm các đoạn văn bản liên quan từ kho tài liệu nội bộ
2. AUGMENT   → Đính kèm các đoạn đó vào câu hỏi gốc ("context injection")
3. GENERATE  → Model AI đọc cả câu hỏi + context rồi mới trả lời
```

**Ví dụ với dự án này:**
- Người dùng hỏi: *"Làm thế nào để thêm video YouTube?"*
- Hệ thống tìm trong 405 bài viết docs của OptiSigns và tìm ra bài viết về YouTube
- Gemini đọc nội dung bài viết đó + câu hỏi → trả lời chính xác, có citation

Lợi ích so với "hỏi thẳng AI không có docs":
- Câu trả lời **chính xác** theo tài liệu thực tế của sản phẩm
- Model **không bịa** (hallucinate) vì bị giới hạn bởi system prompt "Only answer using the uploaded docs"
- Có thể **trích dẫn nguồn** cụ thể (Article URL)

---

## 2. File Search Store của Gemini là gì?

**File Search Store** là một dạng **cơ sở dữ liệu vector (vector database)** được Gemini quản lý hoàn toàn.

Khi bạn upload một file `.md` vào File Search Store:
1. Gemini **đọc nội dung** file
2. Tự động **chia nhỏ thành các đoạn** (chunking)
3. Với mỗi đoạn, chạy qua **embedding model** để tạo ra một vector số học (embedding)
4. Lưu các vector đó vào **cơ sở dữ liệu vector** bên trong File Search Store

Khi có câu hỏi đến:
1. Câu hỏi được chuyển thành vector (embedding)
2. Hệ thống tìm các đoạn văn bản có vector **gần giống nhất** với câu hỏi (cosine similarity)
3. Trả về top-K đoạn phù hợp nhất

### So sánh với Vector Store của OpenAI

| Đặc điểm | Gemini File Search Store | OpenAI Vector Store |
|---|---|---|
| **SDK** | `google-genai` v2 | `openai` v2 |
| **API endpoint** | `client.file_search_stores.*` | `client.vector_stores.*` |
| **Upload file** | `upload_to_file_search_store(file=path)` | `upload_and_poll(files=[...])` |
| **Attach vào generation** | `types.Tool(file_search=...)` | `tool_resources` trong Assistant |
| **Chunking** | Tự động (model Gemini) | Tự động (OpenAI engine) |
| **Idempotency** | Phải tự kiểm tra `documents.list()` | Phải tự kiểm tra (không built-in) |
| **Free tier** | ✅ Có, nhưng ~10 RPM | ❌ Cần nạp tiền |
| **Citation format** | `grounding_metadata.grounding_chunks` | `annotations[].file_citation` |
| **Persistent** | ✅ Lưu vĩnh viễn | ✅ Lưu vĩnh viễn |

---

## 3. Chunking & Embedding trong Gemini

Khi bạn gọi `upload_to_file_search_store()`, Gemini thực hiện quy trình sau **hoàn toàn tự động, bên phía server**:

```
File .md (raw text)
       │
       ▼
  [CHUNKING]
  Tách thành các đoạn ~256-512 tokens
  (dựa vào dấu xuống dòng, heading, paragraph)
       │
       ▼
  [EMBEDDING]
  Mỗi đoạn → vector 768+ chiều
  (dùng Gemini embedding model)
       │
       ▼
  [INDEX]
  Lưu vào cơ sở dữ liệu vector bên trong store
```

Bạn **không cần** và **không thể** can thiệp vào quá trình chunking này từ SDK (khác với OpenAI Semantic Retrieval API cũ, nơi bạn phải tự tạo chunks).

**Document vs Chunk:**
- Trong API của Gemini, mỗi file upload thành công sẽ xuất hiện là một **Document** trong store
- Mỗi Document bên trong có nhiều **Chunk** (nhưng chunk không hiển thị ra SDK)
- Bạn chỉ tương tác với Document level: `client.file_search_stores.documents.list()`

---

## 4. Grounding & Citation trong Gemini

Khi model trả lời câu hỏi có dùng File Search, response trả về có thêm trường `grounding_metadata`.

### Cấu trúc dữ liệu citation

```python
response.candidates[0].grounding_metadata
  ├── grounding_chunks: List[GroundingChunk]
  │     └── GroundingChunk
  │           └── retrieved_context:
  │                 ├── uri      # ID/đường dẫn tới document trong store
  │                 ├── title    # display_name của document
  │                 └── text     # đoạn văn bản cụ thể được trích dẫn
  │
  └── grounding_supports: List[GroundingSupport]
        └── (mapping từ phần nào của câu trả lời → chunk nào)
```

### Tại sao dòng `Article URL:` ở đầu file quan trọng?

Mỗi file `.md` trong `docs/` bắt đầu bằng:
```
Article URL: https://support.optisigns.com/hc/en-us/articles/...
```

Khi Gemini chunk file này, **dòng đầu tiên luôn nằm trong chunk đầu tiên** (vì nó nằm đầu file). Điều này đảm bảo:

1. **Model thấy URL** trong context → có thể trích dẫn chính xác theo system prompt:
   *"Cite up to 3 Article URL: lines per reply"*

2. **Người dùng nhận được link** thực tế để đọc thêm, không chỉ là câu trả lời mơ hồ

3. **Grounding chunk** sẽ chứa URL này trong `retrieved_context.text` → dễ extract và hiển thị

Nếu bỏ dòng `Article URL:`, model vẫn có thể trả lời nhưng không thể cung cấp citation chính xác về nguồn gốc tài liệu.

---

## 5. Tại sao cần Rate-limit / Backoff với Free Tier?

### Gemini Free Tier Rate Limits

Khi dùng Gemini API với API key miễn phí, bạn bị giới hạn:

| Operation | Giới hạn |
|---|---|
| Upload file / generation | ~10-15 requests/phút (RPM) |
| Tokens/phút | ~1M tokens/phút |
| Requests/ngày | ~1500 requests/ngày |

### Vì sao không dùng tight loop?

Nếu bạn upload 405 file trong một vòng lặp không có delay:
```python
# SAI - sẽ bị 429 ngay sau vài giây đầu
for file in all_files:
    client.file_search_stores.upload_to_file_search_store(file=file, ...)
```

Trong ~6 giây đầu bạn gửi 10+ request → vượt ngưỡng 10 RPM → **HTTP 429 Too Many Requests**.

### Giải pháp trong code

```python
# ĐÚNG - delay 6 giây giữa các upload
UPLOAD_DELAY_SECONDS: float = 6.0   # ≈ 10 requests/phút

for file in all_files:
    upload(file)
    time.sleep(UPLOAD_DELAY_SECONDS)  # ← Nghỉ giữa mỗi request
```

Nếu vẫn gặp 429 (do burst limit ngắn hạn), `_call_with_retry()` sẽ retry với exponential backoff:
- Lần 1 thất bại → đợi 10 giây rồi thử lại
- Lần 2 thất bại → đợi 20 giây rồi thử lại
- Lần 3 thất bại → đợi 40 giây rồi thử lại
- ...tối đa 5 lần

### So sánh với OpenAI Paid Tier

OpenAI paid tier thường có:
- **5000+ RPM** cho API calls thông thường
- **Batch upload** xử lý 50 file cùng lúc không lo rate limit
- Chỉ bị giới hạn khi token/phút vượt ngưỡng (thường rất cao)

Đó là lý do `upload_vector_store.py` không cần delay giữa các file (chỉ cần batch).

---

## 6. Idempotency (Chạy nhiều lần an toàn)

Script `upload_file_search.py` được thiết kế để **chạy lại nhiều lần mà không bị trùng lặp**.

### Cơ chế hoạt động

```python
# Trước khi upload, liệt kê các document đã có
existing_display_names = set()
for doc in client.file_search_stores.documents.list(parent=store_name):
    existing_display_names.add(doc.display_name)

# Khi upload, bỏ qua file đã có
for file in all_files:
    if file.name in existing_display_names:
        logger.info("SKIP %s (đã có trong store)", file.name)
        continue
    upload(file)
```

### Tại sao quan trọng?

Nếu script bị ngắt giữa chừng (mất điện, lỗi mạng) sau khi đã upload 200/405 file:
- Chạy lại script: 200 file đầu sẽ bị **SKIP**, chỉ upload 205 file còn lại
- Không có file nào bị duplicate trong store
- Không lãng phí API quota vào việc re-upload

### Tối ưu hóa: Bỏ qua bước List/Create Store (`GEMINI_STORE_NAME`)

Trong thực tế triển khai, API `file_search_stores.list()` của Gemini có thể bị chậm hoặc timeout. Để giải quyết dứt điểm:
- Khi store đã tạo thành công lần đầu, chúng ta lấy ID của nó (vd: `fileSearchStores/optisignssupportdocs-9umxs3xlf0el`).
- Gán vào biến môi trường `GEMINI_STORE_NAME` trong `.env`.
- Script sẽ đọc biến này và dùng API `get()` trực tiếp thay vì phải `list()` tất cả các store. Đây là một dạng "Fast Path" giúp script chạy mượt hơn rất nhiều khi mạng chập chờn.

---

## 7. Cách chạy thử và verify kết quả

### Bước 1: Chuẩn bị API Key

```bash
# Lấy API key miễn phí tại: https://aistudio.google.com/apikey
# Copy .env.sample → .env và điền key vào
copy .env.sample .env
# Mở file .env, sửa GEMINI_API_KEY=your-key-here
```

### Bước 2: Chạy script

```bash
# Kích hoạt virtual environment
venv\Scripts\activate

# Chạy script upload + test
python upload_file_search.py
```

Kết quả mong đợi trên terminal:
```
2026-07-07 14:00:00 [INFO] Looking for existing File Search Store 'optisigns-support-docs' …
2026-07-07 14:00:01 [INFO]   Not found — creating new File Search Store …
2026-07-07 14:00:02 [INFO]   Created: name=fileSearchStores/abc123
2026-07-07 14:00:02 [INFO] Found 405 .md files in docs/
2026-07-07 14:00:02 [INFO] Checking existing documents …
2026-07-07 14:00:02 [INFO]   [1/405] Uploading what-is-optisigns.md …
2026-07-07 14:00:03 [INFO]     → uploaded OK
...
============================================================
  UPLOAD SUMMARY
============================================================
  Files found locally  : 405
  Newly uploaded       : 405
  Skipped (in store)   : 0
  Failed               : 0
============================================================

============================================================
  SMOKE-TEST RESULT
============================================================
  Question: How do I add a YouTube video?
------------------------------------------------------------
  To add a YouTube video to OptiSigns, follow these steps:
  ...
  Grounding citations:
    [1] URI   : ...
         Snippet: 'Article URL: https://support.optisigns.com/...'
============================================================
```

> ⚠️ **Lưu ý thời gian**: Upload 405 file với delay 6 giây mỗi file sẽ mất khoảng **40 phút**. Đây là do giới hạn của free tier. Có thể tăng tốc bằng cách giảm `UPLOAD_DELAY_SECONDS` xuống 4-5 giây nếu tài khoản của bạn ổn định.

### Bước 3: Verify trên Google AI Studio

1. Truy cập [https://aistudio.google.com](https://aistudio.google.com)
2. Đăng nhập bằng Google account có API key
3. Vào menu **"File Search"** hoặc **"Storage"** ở thanh bên trái
4. Tìm store tên **"optisigns-support-docs"** → xem danh sách documents đã upload
5. Tạo một **New Chat** → thêm File Search Store vào tools → hỏi thử câu hỏi

> 💡 **Tip**: Chụp screenshot màn hình Google AI Studio với chat bot đang trả lời đúng câu hỏi về OptiSigns để đưa vào báo cáo/demo cho nhà tuyển dụng.

### Bước 4: Chạy lại an toàn

Nếu muốn test lại (sau khi đã upload 1 lần):

```bash
# Lần 2 trở đi: tất cả 405 file sẽ bị SKIP, chỉ chạy smoke test
python upload_file_search.py
```

Output sẽ cho thấy:
```
  Newly uploaded       : 0
  Skipped (in store)   : 405
```

---

## 8. Tóm tắt kiến trúc tổng thể

```
scraper.py                    upload_file_search.py
    │                                  │
    ▼                                  ▼
Zendesk API                    Google Gemini API
    │                            │           │
    ▼                            ▼           ▼
docs/*.md ──────────────► File Search    generate_content()
(405 bài viết)               Store            │
                            (vector DB)        │
                                │              │
                                └──── RAG ─────┘
                                         │
                                         ▼
                                  OptiBot trả lời
                                  (có citation URL)
```

---

## 9. Các lỗi thường gặp và cách xử lý

| Lỗi | Nguyên nhân | Cách xử lý |
|---|---|---|
| `GEMINI_API_KEY is not set` | Chưa tạo file `.env` | Copy `.env.sample` → `.env`, điền key |
| `429 Too Many Requests` | Vượt rate limit | Script tự retry, chờ thêm 10-40s |
| `ConnectTimeout` liên tục (0s) | Lỗi của SDK `google-genai` | Tuyệt đối **không** truyền tham số `http_options={"timeout": 30.0}` vào `genai.Client()` vì thư viện `httpx` ngầm bên dưới sẽ bị kẹt và timeout ngay lập tức. |
| `ConnectTimeout` (khi list store) | Mạng chập chờn với Google | Thêm biến `GEMINI_STORE_NAME` vào `.env` để lấy trực tiếp thay vì list. |
| `503 Service Unavailable` | Model đang bị quá tải (high demand) | Cơ chế retry của script sẽ tự lặp lại sau vài chục giây cho đến khi có slot trống. |
| `No grounding chunks` | Store chưa index xong | Đợi ~60s sau upload rồi test lại |
| `Only answer using the uploaded docs` (bot không trả lời) | Câu hỏi ngoài phạm vi docs | Đây là đúng theo system prompt |
