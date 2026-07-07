# Giải thích kỹ thuật: main.py, Docker & Deploy

> Tài liệu này viết cho lập trình viên junior đọc trước buổi phỏng vấn kỹ thuật.
> Mục tiêu: hiểu rõ tại sao hệ thống được thiết kế như vậy, không chỉ là "code chạy được".

---

## 1. Luồng tổng thể của main.py

```
┌─────────────────────────────────────────────────────────┐
│                      main.py                            │
│                                                         │
│  1. Kết nối Gemini API → lấy File Search Store         │
│       ↓                                                 │
│  2. Gọi Zendesk API → lấy danh sách bài viết hiện tại  │
│       ↓                                                 │
│  3. Đọc state.json → biết trạng thái lần chạy trước   │
│       ↓                                                 │
│  4. So sánh → phân loại từng bài:                      │
│       ├─ ADDED   → scrape + upload                     │
│       ├─ UPDATED → scrape + upload mới + xóa cũ        │
│       ├─ SKIPPED → bỏ qua (không thay đổi)             │
│       └─ DELETED → xóa khỏi File Search Store          │
│       ↓                                                 │
│  5. In tóm tắt: Added: X, Updated: Y, Skipped: Z, …   │
│       ↓                                                 │
│  6. exit(0) thành công / exit(1) nếu lỗi              │
└─────────────────────────────────────────────────────────┘
```

### Giải thích từng bước

**Bước 1 — Kết nối Gemini:**
Script tái sử dụng hàm `build_client()` và `get_or_create_file_search_store()` từ `upload_file_search.py`. Nếu biến `GEMINI_STORE_NAME` đã được đặt trong `.env`, nó sẽ dùng thẳng store đó mà không cần gọi API list/create (nhanh hơn và tránh lỗi timeout).

**Bước 2 — Fetch Zendesk:**
Tái sử dụng `fetch_all_articles()` từ `scraper.py`. Hàm này đã có retry + backoff, phân trang tự động. Kết quả là list tất cả bài viết hiện tại trên Zendesk (có `id`, `updated_at`, `html_url`, `body`).

**Bước 3 — Đọc state.json:**
Hàm `load_state()` đọc file JSON. Nếu file không tồn tại (lần đầu chạy), trả về dict rỗng → tất cả bài viết được coi là MỚI.

**Bước 4 — So sánh và phân loại:**
Hàm `categorise_articles()` so sánh `updated_at` của mỗi bài với giá trị đã lưu trong state.

---

## 2. Logic Delta Detection hoạt động ra sao

### Các trường hợp có thể xảy ra:

| Tình huống | Điều kiện | Hành động | Đếm vào |
|---|---|---|---|
| **Bài mới** | `article_id` không có trong state.json | Scrape → upload → thêm vào state | `ADDED` |
| **Bài đã cập nhật** | `article_id` có trong state nhưng `updated_at` khác | Re-scrape → upload mới → xóa doc cũ → cập nhật state | `UPDATED` |
| **Bài không đổi** | `article_id` trong state và `updated_at` giống hệt | Không làm gì | `SKIPPED` |
| **Bài đã xóa trên Zendesk** | `article_id` trong state nhưng không còn trong API | Xóa khỏi File Search Store → xóa khỏi state | `DELETED` |

### Ví dụ thực tế:

```
Ngày 1 (chạy lần đầu): state.json trống
→ 405 bài → tất cả ADDED
→ state.json ghi lại 405 bài với updated_at của chúng

Ngày 2 (chạy lần 2):
→ Zendesk có 405 bài, nhưng 3 bài có updated_at mới hơn
→ 402 SKIPPED, 3 UPDATED
→ state.json được cập nhật cho 3 bài đó

Ngày 3: OptiSigns xóa 1 bài cũ, thêm 2 bài mới
→ 404 SKIPPED, 2 ADDED, 1 DELETED
```

---

## 3. Vì sao cần state.json và cách nó tránh upload trùng lặp

### Vấn đề nếu không có state.json:
Mỗi lần cron job chạy, nó sẽ không biết những gì đã được upload trước đó.
→ Kết quả: Upload lại toàn bộ 405 bài mỗi ngày → lãng phí API quota, tốn thời gian (~40 phút/lần), tạo bản trùng trong File Search Store.

### Giải pháp: state.json
File JSON đóng vai trò là "bộ nhớ ngắn hạn" của pipeline. Nó lưu trữ:

```json
{
  "360051014713": {
    "updated_at": "2026-06-18T05:00:56Z",
    "slug": "how-to-use-youtube-with-optisigns",
    "document_name": "fileSearchStores/optisignssupportdocs-xxx/documents/yyy"
  }
}
```

- `updated_at`: Dùng để so sánh với Zendesk API → biết bài có thay đổi không.
- `slug`: Biết tên file `.md` tương ứng trên disk.
- `document_name`: ID của document trong Gemini Store → cần để xóa khi bài được cập nhật hoặc xóa.

### Crash-safe design (Thiết kế an toàn khi crash):
`save_state()` được gọi **ngay sau mỗi thao tác thành công** (không đợi đến cuối).

```python
# Sau khi upload thành công 1 bài:
state[aid] = {"updated_at": ..., "document_name": ...}
save_state(state)   # ← ghi ngay vào disk
```

**Kịch bản crash:** Script đang upload bài số 150/405, máy chủ bị mất điện đột ngột.
- Khởi động lại: state.json đã ghi bài 1-149
- 149 bài đầu: `updated_at` khớp → **SKIPPED**
- Bài 150+: tiếp tục từ đó → không bị upload trùng

---

## 4. Vì sao exit(0) / exit(1) quan trọng với Cron Job

### Quy ước Exit Code trong Unix/Linux:
- **`exit(0)`** = Thành công (success) — cron job coi là OK.
- **`exit(1)`** (hoặc bất kỳ số khác 0) = Thất bại — cron job báo lỗi.

### Tại sao điều này quan trọng?

```
# Cron job kiểm tra exit code để quyết định hành động tiếp theo:
python main.py && echo "OK — cron thành công"
python main.py || send_alert_email("Pipeline bị lỗi!")
```

- Các hệ thống monitoring (Railway, Render, PagerDuty...) đọc exit code để gửi cảnh báo.
- Nếu `main.py` luôn exit(0) dù có lỗi → giám sát không bao giờ phát hiện vấn đề.
- Nếu bỏ `sys.exit()` hoàn toàn → Python mặc định trả về exit(0) → tương tự vấn đề trên.

### Thiết kế trong main.py:
```python
# Thành công:
sys.exit(0)

# Lỗi không phục hồi (không có article, không kết nối được API...):
sys.exit(1)

# Bắt exception không mong đợi ở lớp ngoài cùng:
except Exception as exc:
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    sys.exit(1)
```

---

## 5. Dockerfile hoạt động ra sao — Giải thích từng dòng

```dockerfile
# Dòng 1: Dùng Python 3.11 phiên bản "slim"
# "slim" = ít thư viện hệ thống hơn → image nhỏ hơn (~50MB vs ~900MB full)
FROM python:3.11-slim

# Dòng 2-3: Cấu hình môi trường Python
# PYTHONDONTWRITEBYTECODE=1 → không tạo file .pyc (không cần trong container)
# PYTHONUNBUFFERED=1        → log hiện ngay lập tức, không bị buffer
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dòng 4: Thư mục làm việc trong container
WORKDIR /app

# Dòng 5-6: Copy và cài dependencies TRƯỚC khi copy code
# Trick quan trọng: Docker cache layer này nếu requirements.txt không đổi
# → tái build nhanh hơn rất nhiều khi chỉ sửa code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Dòng 7: Copy toàn bộ source code vào container
COPY . .

# Dòng 8: Tạo thư mục docs trước
RUN mkdir -p /app/docs

# Dòng 9: Lệnh chạy khi container khởi động
# Pipeline chạy một lần rồi thoát (exit 0 hoặc exit 1)
CMD ["python", "main.py"]
```

### Cách inject secret khi chạy:
```bash
# Chạy local với secret từ biến môi trường:
docker run -e GEMINI_API_KEY=your-key-here optibot

# Chạy với file .env:
docker run --env-file .env optibot
```

**Quan trọng:** Secret KHÔNG được hard-code trong Dockerfile hay code. Luôn dùng `-e` hoặc `--env-file`.

### Về state.json và volume mount:
Mỗi lần container khởi động mới, filesystem bên trong sẽ **reset về trạng thái ban đầu** (không có state.json).
Để giữ state giữa các lần chạy, cần mount một volume:

```bash
# Tạo thư mục lưu state trên máy host:
mkdir -p ./data

# Mount vào /app/state và trỏ STATE_FILE vào đó:
docker run \
  -e GEMINI_API_KEY=your-key \
  -e STATE_FILE=/app/state/state.json \
  -v $(pwd)/data:/app/state \
  optibot
```

---

## 6. Cách deploy lên GitHub Actions (Hướng dẫn từng bước)

> **Vì sao dùng GitHub Actions thay vì Railway / Render / Fly.io?**
> Các nền tảng trên không còn free tier thật cho cron/scheduled job kể từ giữa 2025.
> GitHub Actions miễn phí hoàn toàn cho repo public, và 2,000 phút/tháng cho repo private —
> đủ để chạy job ~30 phút mỗi ngày.

### Bước 1: Chuẩn bị repo GitHub
1. Push code lên GitHub (đảm bảo `.env` trong `.gitignore`, chỉ có `.env.sample`).
2. Kiểm tra `Dockerfile` và `.github/workflows/daily-sync.yml` đã có trong repo.

### Bước 2: Thêm GitHub Actions Secrets
1. Vào GitHub repo → tab **"Settings"**.
2. Ở sidebar trái: **"Secrets and variables"** → **"Actions"**.
3. Click **"New repository secret"** cho từng biến:

| Secret Name | Giá trị |
|---|---|
| `GEMINI_API_KEY` | API key thực của bạn (lấy từ aistudio.google.com) |
| `GEMINI_STORE_NAME` | Tên store, ví dụ: `fileSearchStores/optisignssupportdocs-xxxx` |

**Quan trọng:** Các Secret được mã hóa và **không bao giờ hiện trong logs** — GitHub tự động che chúng.

### Bước 3: Kích hoạt workflow thủ công (lần đầu)
1. Vào GitHub repo → tab **"Actions"**.
2. Ở sidebar trái, click **"Daily OptiBot Sync"**.
3. Click nút **"Run workflow"** (góc phải trên) → chọn branch `main` → **"Run workflow"**.
4. Lần chạy đầu sẽ upload toàn bộ 405 bài (mất khoảng 40 phút), sau đó `state.json` được commit ngược về repo.

### Bước 4: Xem Logs
1. Vào tab **"Actions"** → click vào lần chạy gần nhất.
2. Click vào job **"Scrape → Delta → Upload"**.
3. Mở rộng step **"Run sync pipeline"** để xem toàn bộ output của `main.py`.
4. Tìm dòng cuối: `SUMMARY: Added=X Updated=Y Skipped=Z Deleted=W`

**Link logs chia sẻ:** URL trên browser chính là link share — ví dụ:
`https://github.com/<user>/<repo>/actions/runs/<run-id>`

### Bước 5: Lịch chạy tự động hàng ngày
Sau khi workflow được push lên, GitHub sẽ tự động chạy vào **02:00 UTC mỗi ngày**.
Không cần cấu hình thêm gì — workflow file `.github/workflows/daily-sync.yml` đã định nghĩa sẵn:
```yaml
schedule:
  - cron: "0 2 * * *"
```

---

## 7. Cơ chế state.json trong GitHub Actions

### Vấn đề: GitHub Actions Runner là "ephemeral" (tạm thời)
Mỗi lần workflow chạy, GitHub khởi động một **máy ảo mới hoàn toàn** và xoá nó sau khi xong.
Nếu không xử lý đặc biệt, `state.json` sẽ biến mất sau mỗi run → mỗi ngày upload lại 405 bài.

### Giải pháp: Commit state.json ngược về repo
Sau khi `main.py` chạy xong, workflow thực hiện:

```yaml
- name: Commit updated state.json
  run: |
    git config user.name  "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    git add state.json
    if git diff --cached --quiet; then
      echo "state.json unchanged — nothing to commit."
    else
      git commit -m "chore: update state.json [skip ci]"
      git push
    fi
```

- **`[skip ci]`**: Ngăn commit này trigger workflow chạy lại (tránh vòng lặp vô tận).
- **`git diff --cached --quiet`**: Chỉ commit nếu `state.json` thực sự thay đổi.

---

## 8. Tóm tắt kiến trúc tổng thể sau khi hoàn thiện

```
GitHub Repo
    │
    ├── scraper.py                        ← Fetch Zendesk API, convert HTML → Markdown
    ├── upload_file_search.py             ← Upload docs lên Gemini File Search Store
    ├── main.py                           ← Pipeline tổng (scrape + delta + upload)
    ├── Dockerfile                        ← Đóng gói thành container (chạy local)
    ├── state.json                        ← Được commit bởi GitHub Actions bot sau mỗi run
    ├── .github/
    │   └── workflows/
    │       └── daily-sync.yml            ← Định nghĩa cron job tự động
    └── docs/                             ← File .md tạm thời trong Runner (không commit)
         ├── how-to-use-youtube-with-optisigns.md
         └── ...

GitHub Actions (Cron: 0 2 * * * = 02:00 UTC mỗi ngày):
    1. Checkout repo → lấy state.json từ commit trước
    2. pip install -r requirements.txt
    3. python main.py (GEMINI_API_KEY từ Secrets)
       ├── Fetch 405 bài từ Zendesk
       ├── So sánh vs state.json → delta
       ├── Upload chỉ bài ADDED/UPDATED lên Gemini
       └── In SUMMARY: Added=X Updated=Y Skipped=Z Deleted=W
    4. git commit state.json → git push
    5. Runner bị xoá — state.json đã an toàn trên GitHub
```


