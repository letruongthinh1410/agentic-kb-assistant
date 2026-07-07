# EXPLAIN_scraper.md — Giải thích script `scraper.py` cho junior developer

> **Mục tiêu của tài liệu này:** Giúp bạn hiểu toàn bộ cách script hoạt động
> trước khi bước vào buổi phỏng vấn kỹ thuật. Tất cả thuật ngữ đều được giải
> thích bằng ngôn ngữ đơn giản.

---

## 1. Luồng chạy từ đầu đến cuối (flow tổng quát)

```
[Bắt đầu]
    │
    ▼
Tạo thư mục đầu ra (docs/)
    │
    ▼
Khởi tạo HTTP Session (có hoặc không có auth token)
    │
    ▼
Gọi API trang đầu tiên
https://support.optisigns.com/api/v2/help_center/en-us/articles.json?per_page=100
    │
    ▼
┌─────────────────────────────────────────┐
│  Lặp lại (pagination):                  │
│  - Lấy danh sách articles từ trang hiện │
│  - Lưu vào danh sách tổng               │
│  - Đọc trường "next_page" trong JSON     │
│  - Nếu "next_page" != null → gọi tiếp  │
│  - Nếu "next_page" == null → dừng lặp  │
└─────────────────────────────────────────┘
    │
    ▼
Với MỖI article trong danh sách:
    │
    ├─ Chuyển HTML body → Markdown (markdownify)
    │
    ├─ Tạo nội dung file (header + nội dung)
    │
    ├─ Lấy slug từ URL
    │
    └─ Ghi file vào docs/<slug>.md
    │
    ▼
In bảng tóm tắt: tổng bài, số file ghi thành công, số lỗi
    │
    ▼
[Kết thúc — exit 0 nếu OK, exit 1 nếu lỗi nghiêm trọng]
```

---

## 2. Vì sao dùng Zendesk API thay vì crawl HTML thông thường?

### Crawl HTML trực tiếp có vấn đề gì?

Khi bạn dùng `requests.get("https://support.optisigns.com/hc/en-us/articles")`,
bạn nhận về toàn bộ trang HTML bao gồm:

- Header, footer, menu điều hướng
- Quảng cáo, popup chat
- JavaScript làm thay đổi giao diện
- Rất nhiều thẻ HTML "rác" không liên quan đến nội dung bài viết

Việc phải dùng BeautifulSoup để lọc ra đúng phần nội dung bài viết rất dễ sai,
và còn dễ bị block bởi Cloudflare / bot protection.

### Zendesk API cho sẵn dữ liệu sạch

Zendesk là nền tảng Help Center phổ biến. Họ cung cấp sẵn một REST API công khai
(không cần đăng nhập) trả về dữ liệu JSON có cấu trúc rõ ràng:

```
GET /api/v2/help_center/en-us/articles.json
```

→ Nhận ngay danh sách tất cả bài viết dưới dạng JSON, bao gồm tiêu đề, nội dung,
URL, ngày cập nhật — không cần parse HTML phức tạp.

**Lợi ích:**
| Tiêu chí         | Crawl HTML           | Zendesk API             |
|------------------|----------------------|-------------------------|
| Độ ổn định       | Thấp (layout thay đổi) | Cao (schema ổn định)  |
| Tốc độ xử lý    | Chậm                 | Nhanh                   |
| Rủi ro bị block  | Cao                  | Rất thấp               |
| Dữ liệu sạch?   | Cần lọc nhiều        | Sẵn sàng dùng ngay      |

---

## 3. Cấu trúc JSON trả về từ API và ý nghĩa từng field

Mỗi lần gọi API, server trả về một JSON có dạng như sau:

```json
{
  "articles": [
    {
      "id": 12345678,
      "title": "How to set up digital signage",
      "body": "<p>To set up...</p><ul><li>Step 1</li></ul>",
      "html_url": "https://support.optisigns.com/hc/en-us/articles/12345678-how-to-set-up",
      "updated_at": "2024-03-15T08:30:00Z",
      ...
    }
  ],
  "next_page": "https://support.optisigns.com/api/v2/help_center/en-us/articles.json?page=2&per_page=100",
  "previous_page": null,
  "page_count": 5,
  "count": 450
}
```

### Giải thích từng field script dùng:

| Field        | Kiểu dữ liệu | Ý nghĩa                                                        |
|--------------|--------------|----------------------------------------------------------------|
| `id`         | số nguyên    | Mã định danh duy nhất của bài viết trong hệ thống Zendesk     |
| `title`      | chuỗi        | Tiêu đề bài viết (hiển thị trong file .md)                    |
| `body`       | chuỗi HTML   | Nội dung bài viết — cần chuyển đổi sang Markdown              |
| `html_url`   | URL          | Đường dẫn người dùng có thể truy cập bài viết trên trình duyệt|
| `updated_at` | ISO 8601     | Thời điểm bài viết được cập nhật lần cuối (UTC)               |
| `next_page`  | URL hoặc null| URL trang tiếp theo (dùng để phân trang); null nếu hết trang  |

---

## 4. Cách xử lý phân trang (pagination) hoạt động

Khi số lượng bài viết lớn (ví dụ 500 bài), Zendesk không trả về tất cả trong
một lần gọi. Thay vào đó, nó chia thành nhiều "trang" (pages), mỗi trang tối
đa 100 bài.

### Cơ chế Page-based (offset) pagination của Zendesk

Mỗi response JSON có trường `next_page`:

```
Trang 1:  GET /articles.json?per_page=100
          → articles[0..99], next_page = ".../articles.json?page=2&per_page=100"

Trang 2:  GET /articles.json?page=2&per_page=100
          → articles[100..199], next_page = ".../articles.json?page=3&per_page=100"

Trang N:  GET /articles.json?page=N&per_page=100
          → articles[...], next_page = null  ← dừng lại ở đây
```

### Code trong script:

```python
url = f"{BASE_API_URL}?per_page={ARTICLES_PER_PAGE}"

while url:                          # vòng lặp tiếp tục khi còn URL
    data = fetch_with_retry(session, url)
    articles.extend(data["articles"])   # gom bài vào danh sách tổng
    url = data.get("next_page")         # None → thoát vòng lặp
```

Đây là pattern phổ biến khi làm việc với REST API có pagination — đơn giản
và hiệu quả, không cần biết trước tổng số trang.

---

## 5. Format mỗi file .md và vì sao có dòng "Article URL:" ở đầu

### Ví dụ nội dung một file `docs/how-to-set-up-digital-signage.md`:

```markdown
Article URL: https://support.optisigns.com/hc/en-us/articles/12345678-how-to-set-up
Last Updated: 2024-03-15T08:30:00Z

# How to set up digital signage

To set up your digital signage, follow these steps:

- Step 1: ...
- Step 2: ...
```

### Giải thích từng dòng:

| Dòng                   | Mục đích                                                                    |
|------------------------|-----------------------------------------------------------------------------|
| `Article URL: <url>`  | Dòng bắt buộc — cho phép chatbot trích dẫn nguồn chính xác khi trả lời     |
| `Last Updated: <time>` | Thông tin ngày cập nhật để người dùng biết nội dung còn mới không           |
| *(dòng trống)*         | Phân cách header metadata với nội dung Markdown                              |
| `# <title>`            | Tiêu đề bài viết theo cú pháp Markdown (heading cấp 1)                      |
| *(nội dung)*           | Nội dung đã chuyển đổi từ HTML sang Markdown                                |

### Vì sao dòng "Article URL:" quan trọng?

Bước tiếp theo của dự án là nạp các file .md này vào **OpenAI Vector Store**.
Khi chatbot được hỏi một câu hỏi, nó sẽ tìm kiếm trong Vector Store và trả lời
kèm trích dẫn nguồn.

Nếu không có dòng `Article URL:`, chatbot sẽ không biết cần trích dẫn link nào
để người dùng có thể đọc thêm. Dòng này hoạt động như một "nhãn nguồn" gắn sẵn
trong mỗi tài liệu.

---

## 6. Xử lý lỗi và retry (back-off)

Script không cho phép một bài viết lỗi làm sập toàn bộ quá trình.

### Với lỗi mạng / server:

```python
for attempt in range(1, MAX_RETRIES + 1):
    try:
        response = session.get(url, timeout=30)
        if response.status_code == 429:   # Rate limit
            time.sleep(retry_after)
            continue
        if response.status_code >= 500:   # Server lỗi
            time.sleep(2 ** attempt)      # Chờ: 2s, 4s, 8s, 16s, 32s
            continue
        ...
    except (ConnectionError, Timeout):
        time.sleep(2 ** attempt)          # Thử lại sau vài giây
```

**Exponential backoff** nghĩa là mỗi lần thất bại, thời gian chờ tăng gấp đôi
(2 → 4 → 8 → 16 giây). Điều này giúp không "tấn công" server khi nó đang quá tải.

### Với lỗi khi lưu file:

```python
def save_article(article, output_dir):
    try:
        ...
        output_path.write_text(content, encoding="utf-8")
        return True
    except Exception as exc:
        logger.error("Failed: %s", exc)   # Ghi log lỗi
        return False                       # Tiếp tục bài tiếp theo
```

Nếu một bài lỗi, script log lỗi và chuyển sang bài tiếp theo, thay vì crash.

---

## 7. Thiết kế sẵn cho xác thực trong tương lai

Hiện tại, endpoint này là công khai — không cần API key. Nhưng nếu sau này cần
dùng Zendesk có trả phí (private articles), bạn chỉ cần:

1. Sao chép `.env.sample` thành `.env`
2. Điền `ZENDESK_TOKEN=your_token_here`
3. Chạy lại script — nó tự động đọc và gắn header `Authorization: Bearer <token>`

Không cần sửa code, chỉ cần thêm biến môi trường.

---

## 8. Slug là gì và được tạo ra như thế nào?

**Slug** là phần chữ trong URL được dùng làm tên file, ví dụ:

```
URL:  https://support.optisigns.com/hc/en-us/articles/12345678-how-to-set-up-digital-signage
Slug: how-to-set-up-digital-signage
File: docs/how-to-set-up-digital-signage.md
```

Regex được dùng để tách phần slug ra:

```python
match = re.search(r"/articles/\d+-(.+)$", path)
# \d+ → khớp với số ID (12345678)
# (.+) → bắt toàn bộ phần sau dấu gạch ngang đầu tiên
```

Nếu URL không có slug (hiếm gặp), fallback về ID bài viết: `docs/12345678.md`.

---

## 9. Kết quả kiểm tra & sửa lỗi (Code Review — 2026-07-07)

Sau khi review kỹ `scraper.py` và chạy kiểm tra trên toàn bộ 405 bài viết thực,
dưới đây là kết quả:

### 9.1 Chuyển link tương đối → tuyệt đối

**Kết quả: ✅ Hoạt động đúng** (sau khi vá thêm một trường hợp bị thiếu)

Hàm `make_absolute_links()` ban đầu chỉ xử lý attribute dùng **nháy kép**:

```python
# Phiên bản cũ -- chỉ bắt href="..." và src="..."
html = re.sub(r'(href|src)="(/[^"]*)"', ...)
```

Tuy nhiên HTML hợp lệ cũng cho phép dùng **nháy đơn**:

```html
<a href='/hc/en-us/articles/123'>...</a>
```

Phiên bản mới xử lý cả hai dạng:

```python
# Nháy kép
html = re.sub(r'(href|src)="(/[^"]*)"', ...)
# Nháy đơn
html = re.sub(r"(href|src)='(/[^']*)'", ...)
```

**Kiểm chứng:** Sau khi chạy scraper lại, quét toàn bộ 405 file .md trong thư
mục `docs/` — không tìm thấy bất kỳ link Markdown nào dạng `](/...)` (link
tương đối). Tất cả đều đã là URL tuyệt đối.

---

### 9.2 Chuyển `<pre><code>` → Markdown fenced code block

**Kết quả: ✅ Hoạt động đúng** (markdownify xử lý tốt, không cần can thiệp)

Trong 405 bài viết, có **37 bài** chứa thẻ `<pre>`. Có hai dạng thường gặp:

| Dạng HTML                        | Ví dụ                                   | Kết quả                |
|----------------------------------|-----------------------------------------|------------------------|
| `<pre><code>...</code></pre>`    | Đoạn code có highlight ngôn ngữ         | Fenced block ` ``` `  |
| `<pre>...<br>...</pre>`          | Code thuần, xuống dòng bằng `<br>`      | Fenced block ` ``` `  |

Cả hai dạng đều ra đúng Markdown fenced block. Ví dụ thực tế từ bài viết OAuth:

```markdown
``` 
const body = {
  "grant_type": "client_credentials",
  "client_id": "<CLIENT_ID>",
  "client_secret": "<CLIENT_SECRET>"
};
const params = Object.keys(body || {}).map((key) => {
  return key + '=' + body[key];
}).join('&');
...
```
```

---

### 9.3 Lỗi phát hiện: Key không hợp lệ trong `MD_OPTIONS`

**Vấn đề:** `MD_OPTIONS` ban đầu có key `"convert_links": True` nhưng đây
**không phải** option hợp lệ của thư viện `markdownify`. Library này chuyển
đổi `<a href>` thành link Markdown theo mặc định — không cần cấu hình thêm.

Tham số không hợp lệ bị **im lặng bỏ qua** (không lỗi, không cảnh báo), nhưng
nếu để lại sẽ gây hiểu nhầm khi đọc code.

**Sửa:** Xóa `"convert_links": True` và `"convert_images": True` khỏi
`MD_OPTIONS`. Thêm vào `"newline_style": "backslash"` để xuống dòng trong
`<pre>` được giữ nguyên đúng hơn.

```python
# Phiên bản đã sửa
MD_OPTIONS: dict = {
    "heading_style": "ATX",
    "bullets": "-",
    "strip": ["script", "style"],
    "newline_style": "backslash",  # giữ nguyên line breaks trong <pre>
}
```

---

*Tài liệu này được tạo tự động song song với code để phục vụ mục đích học tập
và phỏng vấn. Cập nhật tài liệu này mỗi khi code thay đổi đáng kể.*
