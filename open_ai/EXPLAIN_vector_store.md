# EXPLAIN_vector_store.md — Giải thích `upload_vector_store.py` cho junior developer

> **Mục tiêu:** Hiểu rõ cách hệ thống RAG (Retrieval-Augmented Generation) hoạt
> động với OpenAI Assistants API, để có thể giải thích lưu thông trong buổi
> phỏng vấn kỹ thuật.

---

## 1. RAG là gì và vì sao cần Vector Store?

### Vấn đề của LLM thuần

Một mô hình ngôn ngữ lớn (LLM) như GPT-4 được train đến một ngày cắt dữ liệu
cố định, và **không biết** nội dung tài liệu nội bộ của công ty bạn. Nếu bạn
hỏi "Cách cài OptiSigns trên Raspberry Pi?", nó sẽ trả lời chung chung hoặc
bịa ra thông tin.

### Giải pháp: RAG

**RAG** (Retrieval-Augmented Generation) = *Tìm kiếm trước, sinh câu trả lời
sau*. Quy trình gồm 3 bước:

```
[1] Lập chỉ mục (Indexing)
   Tài liệu .md → chia nhỏ thành chunks → tạo embedding vector
   → lưu vào Vector Store (cơ sở dữ liệu vector)

[2] Truy vấn (Retrieval)
   Câu hỏi người dùng → tạo embedding → tìm kiếm vector gần nhất
   → lấy ra 3-5 đoạn văn liên quan nhất

[3] Sinh câu trả lời (Generation)
   LLM nhận: [system prompt] + [đoạn văn tìm được] + [câu hỏi]
   → trả lời DỰA TRÊN tài liệu, không bịa
```

### Vector Store trong OpenAI

**Vector Store** là dịch vụ lưu trữ và tìm kiếm vector embedding do OpenAI
quản lý. Bạn upload file lên, OpenAI tự động:

1. Đọc nội dung văn bản
2. Chia thành chunks nhỏ
3. Tạo embedding (biểu diễn số học của ý nghĩa văn bản)
4. Lưu vào cơ sở dữ liệu có thể tìm kiếm theo ngữ nghĩa

Khi có câu hỏi, hệ thống tìm các chunks có embedding gần với embedding của câu
hỏi — tức là tìm theo *ý nghĩa*, không phải từ khoá chính xác.

---

## 2. Assistant, Thread, Run — ý nghĩa và quan hệ giữa chúng

### Sơ đồ quan hệ

```
OpenAI Platform
│
├── Assistant (cấu hình chatbot)
│   ├── name: "OptiBot"
│   ├── instructions: system prompt
│   ├── model: "gpt-4o-mini"
│   ├── tools: [file_search]
│   └── tool_resources:
│       └── file_search:
│           └── vector_store_ids: ["vs_xxxxx"]
│
└── Thread (1 cuộc hội thoại cụ thể)
    ├── Message 1: "How do I add a YouTube video?"
    │
    └── Run (1 lần Assistant xử lý Thread)
        ├── Gọi file_search để tìm tài liệu liên quan
        ├── Gửi context + câu hỏi tới model
        └── Sinh Message 2: câu trả lời của Assistant
```

### Giải thích từng khái niệm

| Khái niệm | Tương tự trong đời thực | Lưu trữ trạng thái? |
|-----------|-------------------------|---------------------|
| **Assistant** | Nhân viên hỗ trợ với hồ sơ, kỹ năng cụ thể | Có (cấu hình lâu dài) |
| **Thread** | Chuỗi email/chat với khách hàng | Có (lịch sử hội thoại) |
| **Run** | Một lần nhân viên đọc và trả lời email | Không (sự kiện một lần) |

**Assistant** là bản thiết kế (blueprint) — bạn tạo một lần, dùng mãi cho nhiều
cuộc hội thoại khác nhau.

**Thread** là container lưu toàn bộ lịch sử tin nhắn của một cuộc hội thoại.
Bạn có thể tiếp tục thread cũ để assistant nhớ ngữ cảnh.

**Run** là một lần assistant "suy nghĩ và trả lời" trong một thread. Mỗi Run có
thể dùng tools (file_search), tham chiếu vào memory, v.v.

### Vòng đời của một Run

```
created → queued → in_progress → completed
                              ↘ failed
                              ↘ cancelled
                              ↘ expired (nếu quá thời gian)
```

Script của chúng ta poll (kiểm tra định kỳ mỗi 2 giây) cho đến khi Run đạt
trạng thái `completed` hoặc timeout sau 120 giây.

---

## 3. Chunking — OpenAI chia file thành chunks như thế nào?

### Chunking là gì?

LLM có giới hạn về số token có thể xử lý trong một lần (context window). File
.md của chúng ta có thể dài hàng nghìn từ — không thể đưa toàn bộ vào một lần.

**Chunking** là quá trình cắt tài liệu thành các đoạn nhỏ (chunks) trước khi
tạo embedding.

### Mặc định của OpenAI (auto chunking)

```
Chiến lược: auto (mặc định)
├── chunk_size:    800 tokens (~600 từ tiếng Anh)
└── chunk_overlap: 400 tokens (~300 từ)
```

**Chunk overlap** là số token bị lặp lại giữa hai chunk liền kề. Ví dụ:

```
Chunk 1: [token 1 → 800]
Chunk 2: [token 401 → 1200]   ← 400 token đầu trùng với cuối Chunk 1
Chunk 3: [token 801 → 1600]
```

Overlap giúp câu trả lời không bị mất ngữ cảnh ở đầu/cuối mỗi chunk.

### Tùy chỉnh chunking qua `chunking_strategy`

Bạn có thể truyền `chunking_strategy` khi tạo Vector Store hoặc khi upload file:

```python
# Tùy chỉnh chunk size (ví dụ: chunk nhỏ hơn để tìm kiếm chính xác hơn)
client.beta.vector_stores.create(
    name="optisigns-support-docs",
    chunking_strategy={
        "type": "static",
        "static": {
            "max_chunk_size_tokens": 400,   # nhỏ hơn mặc định 800
            "chunk_overlap_tokens": 200,    # overlap 50%
        }
    }
)
```

| Tùy chọn | Giá trị | Ảnh hưởng |
|----------|---------|-----------|
| `type: "auto"` | Mặc định | OpenAI chọn tham số tự động |
| `type: "static"` | Bạn tự đặt | Kiểm soát được chunk_size & overlap |
| `max_chunk_size_tokens` | 100 – 4096 | Chunk nhỏ → tìm kiếm chính xác hơn nhưng mất ngữ cảnh rộng |
| `chunk_overlap_tokens` | 0 – 50% of chunk_size | Overlap lớn → ít mất ngữ cảnh nhưng tốn storage hơn |

**Trong project này:** Chúng ta dùng chiến lược `auto` (mặc định, không cấu
hình thêm) vì các bài viết của Zendesk đã có cấu trúc tốt (mỗi bài một chủ đề).

---

## 4. Annotation/Citation hoạt động như thế nào?

### Vấn đề: Model trả lời nhưng không có nguồn

Nếu model chỉ nói "Để thêm YouTube, hãy vào Settings > Apps", người dùng không
biết đó là thông tin từ tài liệu nào, có thể kiểm tra thêm ở đâu.

### Cơ chế citation của Assistants API

Khi `file_search` tool tìm được chunks liên quan, nó truyền cả **metadata**
(tên file, đoạn trích) vào context của model. Model được khuyến khích chèn
**annotation placeholder** vào trong câu trả lời:

```
"Để thêm YouTube, bạn vào Apps & Integrations và tìm YouTube【4:0†source】."
```

Placeholder dạng `【số:số†source】` là ký hiệu nội bộ của OpenAI, được trả về
trong field `annotations` của response message:

```python
for ann in message.content[0].text.annotations:
    if ann.type == "file_citation":
        print(ann.file_citation.file_id)   # ID của file gốc được trích dẫn
        print(ann.file_citation.quote)     # Đoạn trích từ file đó
        print(ann.text)                    # Placeholder gốc "【4:0†source】"
```

### Vì sao dòng "Article URL:" quan trọng

Mỗi file .md của chúng ta bắt đầu với:

```
Article URL: https://support.optisigns.com/hc/en-us/articles/12345678-...
Last Updated: 2024-03-15T08:30:00Z
```

Khi chunk này được đưa vào context, model "nhìn thấy" URL ngay đầu tài liệu.
System prompt hướng dẫn model trích dẫn tối đa 3 `"Article URL:"` trong mỗi
câu trả lời — model học cách tìm và tái sử dụng dòng này như một nguồn trích
dẫn có thể click được.

**Tóm lại:**
```
File format đúng (Article URL: ở đầu)
    ↓
Chunk đầu tiên của mỗi bài chứa URL
    ↓
Model tìm thấy chunk + URL khi file_search
    ↓
Model chèn URL vào câu trả lời theo system prompt
    ↓
Người dùng thấy link có thể click để đọc thêm ✓
```

---

## 5. Cách chạy và verify assistant hoạt động đúng

### Bước 1: Chuẩn bị environment

```bash
# Sao chép file sample và điền API key thật
copy .env.sample .env
# Mở .env và điền: OPENAI_API_KEY=sk-...
```

### Bước 2: Cài thư viện

```bash
pip install -r requirements.txt
```

### Bước 3: Chạy script upload

```bash
python upload_vector_store.py
```

Script sẽ in log từng bước. Output mong đợi:

```
2026-07-07 12:00:00 [INFO] Looking for existing Vector Store named 'optisigns-support-docs' …
2026-07-07 12:00:01 [INFO]   Created: id=vs_xxxxxxxxxxxxxxxx
2026-07-07 12:00:01 [INFO] Found 405 .md files in D:\...\docs
2026-07-07 12:00:01 [INFO] Uploading 405 files in 9 batch(es) of up to 50 …
2026-07-07 12:00:05 [INFO]   Batch 1/9 — 50 files …
...
============================================================
  VECTOR STORE STATUS
============================================================
  Name            : optisigns-support-docs
  Files completed : 405
  Files failed    : 0
  ...

============================================================
  SMOKE-TEST RESULT
============================================================
  Question: How do I add a YouTube video?
  ----
  To add a YouTube video to OptiSigns, you can:
  • Go to Apps & Integrations in the left menu...
  Citations:
    [Citation 1] file_id=file-xxx, quote='Article URL: https://...'
```

### Bước 4: Verify kết quả trên OpenAI Dashboard

1. Vào https://platform.openai.com/storage → xem Vector Store "optisigns-support-docs"
2. Vào https://platform.openai.com/assistants → xem "OptiBot"
3. Bấm "Test in Playground" để chat trực tiếp trong UI

### Bước 5: Chạy lại script (idempotent)

Script **không** tạo trùng lặp nếu chạy lại:
- Vector Store đã tồn tại → reuse (nhưng sẽ upload thêm file, gây trùng lặp)
- Assistant đã tồn tại → update system prompt và model

> **Lưu ý:** Nếu bạn chạy script nhiều lần, file sẽ bị upload trùng vào cùng
> Vector Store. Để tránh điều này khi đã upload xong, hãy comment out dòng
> `upload_docs_to_vector_store(...)` trong `main()`.

---

## 6. Giới hạn và tradeoff cần biết

| Vấn đề | Giải thích | Cách xử lý |
|--------|------------|------------|
| Chunk count không hiển thị | OpenAI API không trả về số chunks đã tạo; chỉ có file count | Xem status `completed/failed` thay thế |
| Upload 405 file mất thời gian | `upload_and_poll` block đến khi xong; mỗi batch ~30-60 giây | Chia batch 50 file, log tiến độ |
| File trùng khi chạy lại | Cùng file upload 2 lần → 2 bản trong Vector Store | Kiểm tra `file_counts` trước khi upload |
| Model ảo giác (hallucination) | Nếu không tìm thấy tài liệu liên quan, model có thể bịa | System prompt: "Only answer using the uploaded docs" |
| Citation không 100% chính xác | Model chọn annotation tự động, không phải lúc nào cũng đúng article | Kiểm tra `ann.file_citation.quote` để xác thực |

---

*Tài liệu này được tạo song song với `upload_vector_store.py` để phục vụ mục
đích học tập và phỏng vấn. Cập nhật tài liệu khi code thay đổi đáng kể.*
