# Giải thích kỹ thuật: GitHub Actions Deployment

> Tài liệu viết bằng Tiếng Việt, dành cho lập trình viên junior chuẩn bị cho buổi phỏng vấn kỹ thuật.

---

## 1. Vì sao chọn GitHub Actions thay vì Render / Railway / Fly.io?

### Tình trạng các nền tảng cloud miễn phí (tính đến giữa năm 2025):

| Nền tảng | Vấn đề với Cron Job miễn phí |
|---|---|
| **Render** | Free tier chỉ cho phép web service ngủ/thức (Spin-down). Cron Job bị giới hạn 15 phút/lần → pipeline scrape+upload có thể mất 30-45 phút → bị kill giữa chừng. |
| **Railway** | Free tier hết hạn hoặc yêu cầu thẻ tín dụng từ cuối 2024. Không còn thực sự miễn phí cho workload scheduled. |
| **Fly.io** | Cần cấu hình phức tạp, tài nguyên miễn phí rất hạn chế, dễ bị charge không ngờ. |
| **GitHub Actions** | ✅ **Hoàn toàn miễn phí** cho repo public. Repo private có 2,000 phút/tháng miễn phí — đủ để chạy job ~30 phút/ngày (900 phút/tháng). |

**Kết luận:** GitHub Actions là lựa chọn duy nhất đảm bảo zero-cost cho workload cron job này.

---

## 2. GitHub Actions Runner là gì và vì sao nó "ephemeral"?

### GitHub Actions Runner là gì?
Khi bạn trigger một workflow (tự động hoặc thủ công), GitHub sẽ khởi động một **máy ảo (Virtual Machine)** mới trên đám mây của họ (Ubuntu Linux) để thực thi các bước trong workflow. Máy ảo này gọi là **Runner**.

### Ephemeral có nghĩa là gì?
**Ephemeral** = "Tạm thời", "không tồn tại lâu dài".

Sau khi workflow chạy xong, Runner bị **xoá hoàn toàn**. Toàn bộ file trên filesystem của nó biến mất. Lần chạy tiếp theo sẽ bắt đầu trên một máy ảo **brand new** không có gì cả.

### Tại sao đây là vấn đề với state.json?

```
Lần chạy 1 (Runner A):
  - Scrape 405 bài → upload tất cả → ghi state.json
  - Runner A tắt → state.json biến mất

Lần chạy 2 (Runner B — máy mới):
  - Không có state.json → coi tất cả là bài MỚI
  - Upload lại cả 405 bài → lãng phí, tạo bản trùng!
```

**Giải pháp:** Commit `state.json` ngược lại vào GitHub repo sau mỗi lần chạy.

---

## 3. Cơ chế "commit state.json ngược lại repo" — từng bước

### Sơ đồ luồng:

```
┌─────────────────────────────────────────────────────────────────┐
│                   GitHub Actions Runner                         │
│                                                                 │
│  1. checkout repo → lấy state.json từ commit trước            │
│       ↓                                                         │
│  2. docker build -t optibot .                                  │
│       ↓                                                         │
│  3. docker run (volume mount workspace → /app/state)           │
│       └─ container chạy main.py, ghi state.json vào mount     │
│       ↓                                                         │
│  4. (if: always()) git add state.json                          │
│  5. git diff --cached → có thay đổi?                           │
│       ├─ CÓ → git commit + git push → state.json lên GitHub   │
│       └─ KHÔNG → echo "nothing to commit" → skip              │
│       ↓                                                         │
│  6. (if: always()) kiểm tra outcome pipeline → fail nếu cần  │
│       ↓                                                         │
│  7. Runner bị xoá — nhưng state.json đã an toàn trên GitHub!  │
└─────────────────────────────────────────────────────────────────┘

Lần chạy tiếp theo:
  Step 1 → git checkout → lấy state.json từ commit step 5 của lần trước
```

### Code thực hiện điều này:

**Bước 3 — Chạy Docker với volume mount:**
```yaml
- name: Run sync pipeline (Docker)
  id: pipeline
  env:
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    GEMINI_STORE_NAME: ${{ secrets.GEMINI_STORE_NAME }}
  run: |
    docker run --rm \
      -e GEMINI_API_KEY="${GEMINI_API_KEY}" \
      -e GEMINI_STORE_NAME="${GEMINI_STORE_NAME}" \
      -e STATE_FILE=/app/state/state.json \
      -v "${{ github.workspace }}:/app/state" \
      optibot
```

**Tại sao cần volume mount (`-v`)?**
- Container có filesystem riêng, tách biệt với Runner.
- Nếu không mount, `state.json` do container ghi sẽ **mất ngay khi container kết thúc**.
- Với `-v "${{ github.workspace }}:/app/state"`, thư mục workspace của Runner được ánh xạ vào `/app/state` bên trong container.
- `STATE_FILE=/app/state/state.json` hướng `main.py` ghi vào đường dẫn đã mount → file xuất hiện trên Runner sau khi container exit.

**Bước 4 — Commit state.json (với `if: always()`):**
```yaml
- name: Commit updated state.json
  if: always()    # ← Chạy kể cả khi pipeline bị lỗi!
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

### Tại sao cần `[skip ci]` trong commit message?
Nếu không có `[skip ci]`, mỗi khi bot commit `state.json` lên GitHub, GitHub sẽ thấy có commit mới → trigger workflow chạy lại → tạo vòng lặp vô tận (infinite loop).

---

## 4. Cách GitHub Secrets bảo vệ API Key

### Vấn đề khi không có Secrets:
Nếu bạn hardcode API key trực tiếp trong file workflow:
```yaml
# ❌ TUYỆT ĐỐI KHÔNG LÀM VẬY:
env:
  GEMINI_API_KEY: "AIza..."
```
→ Key sẽ hiện trong code history, ai clone repo cũng thấy.

### Giải pháp: GitHub Secrets
```yaml
# ✅ Đúng cách — lấy từ Secrets:
env:
  GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
```

**Cách GitHub Secrets hoạt động:**
1. Bạn nhập key trong Settings → Secrets → Actions (giao diện web GitHub).
2. GitHub **mã hóa** key bằng thuật toán bất đối xứng và lưu vào hệ thống của họ.
3. Khi workflow chạy, GitHub **giải mã** và inject vào môi trường của Runner dưới dạng biến môi trường.
4. **Nếu key vô tình in ra log**, GitHub sẽ tự động **thay thế bằng `***`** để không lộ ra ngoài.
5. Key không bao giờ xuất hiện trong code, không trong commit history, không trong log.

### Cách thêm Secret:
1. GitHub repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **"New repository secret"**
3. Name: `GEMINI_API_KEY`, Value: dán API key thực của bạn
4. Click **"Add secret"**

---

## 5. Sự khác biệt giữa `schedule` và `workflow_dispatch`

### `schedule` — Trigger tự động theo lịch:
```yaml
on:
  schedule:
    - cron: "0 2 * * *"  # Chạy lúc 02:00 UTC mỗi ngày
```

Cú pháp cron: `phút giờ ngày tháng thứ`
- `0 2 * * *` = Phút 0, giờ 2, mọi ngày, mọi tháng, mọi thứ = 02:00 UTC hàng ngày

**Lưu ý quan trọng:** GitHub có thể delay schedule trigger lên đến 15-30 phút nếu server bận. Đây là hành vi bình thường, không phải lỗi.

### `workflow_dispatch` — Trigger thủ công:
```yaml
on:
  workflow_dispatch:
```

Cho phép bạn chạy workflow bất cứ lúc nào từ giao diện GitHub:
- Actions tab → Chọn workflow → Click **"Run workflow"** button

**Dùng khi nào?**
- Kiểm tra lần đầu (sanity check) trước khi đợi schedule tự chạy
- Demo cho nhà tuyển dụng / người review
- Debug khi có lỗi cần chạy lại ngay

### Cả hai cùng tồn tại:
```yaml
on:
  schedule:
    - cron: "0 2 * * *"
  workflow_dispatch:
```
→ Workflow vừa chạy tự động hàng ngày, vừa cho phép trigger thủ công.

---

## 5b. Vì sao cần `if: always()` trên bước commit?

### Vấn đề:
Theo mặc định, GitHub Actions **bỏ qua (skip)** tất cả các bước tiếp theo nếu một bước nào đó bị lỗi.

**Kịch bản nguy hiểm:**
```
main.py chạy, upload được 200/405 bài → crash ở bài 201
→ state.json đã ghi 200 bài thành công
→ GitHub Actions skip bước "Commit state.json" (vì pipeline failed)
→ state.json với 200 bài bị mất!
→ Lần chạy tiếp theo: upload lại toàn bộ 405 bài từ đầu
```

### Giải pháp: `if: always()`
```yaml
- name: Commit updated state.json
  if: always()    # Chạy kể cả khi bước trước bị lỗi
  run: |
    git add state.json
    ...
```

Với `if: always()`, bước commit **luôn chạy** dù pipeline thành công hay thất bại.
→ 200 bài đã upload thành công được lưu vào state.json trên GitHub.
→ Lần chạy tiếp theo chỉ cần xử lý 205 bài còn lại.

### Vấn đề phụ: Job status bị lệch
Nếu commit step (với `if: always()`) là bước cuối cùng và nó thành công, GitHub sẽ báo **toàn bộ job "Passed"** — dù pipeline thực sự bị lỗi!

**Giải pháp:** Thêm bước cuối để propagate (truyền) trạng thái lỗi:
```yaml
- name: Fail job if pipeline failed
  if: always()
  run: |
    if [ "${{ steps.pipeline.outcome }}" = "failure" ]; then
      echo "Pipeline step failed — propagating failure to job status."
      exit 1    # ← Làm job fail đúng nghĩa
    fi
```

**Kết quả cuối cùng:**
- Pipeline crash giữa chừng → state.json được commit (bảo toàn progress)
- Job status vẫn hiển thị **Failed** (đỏ) → monitoring/email alerts hoạt động đúng
- Lần chạy tiếp theo resume từ đúng điểm đã dừng

---

## 6. Cách đọc logs và lấy link chia sẻ cho README

### Các bước xem logs:

1. Vào GitHub repo → **Actions** tab
2. Ở sidebar trái, click **"Daily OptiBot Sync"** (tên workflow)
3. Click vào một lần chạy trong danh sách (ví dụ: lần chạy gần nhất)
4. Click vào job **"Scrape → Delta → Upload"** (hình tròn xanh/đỏ)
5. Expand step **"Run sync pipeline"** để xem toàn bộ output của `main.py`
6. Tìm dòng cuối: `SUMMARY: Added=X Updated=Y Skipped=Z Deleted=W`

### Lấy link chia sẻ:
- URL trong browser lúc bạn đang xem log chính là link share.
- Định dạng: `https://github.com/<user>/<repo>/actions/runs/<run-id>`
- Dán link này vào README để nhà tuyển dụng kiểm tra logs trực tiếp.

### Ví dụ output khi job chạy thành công:
```
2026-07-07 02:01:05 [INFO] ============================================================
2026-07-07 02:01:05 [INFO]   OptiBot Daily Sync — starting pipeline
2026-07-07 02:01:05 [INFO] ============================================================
2026-07-07 02:01:06 [INFO] Using File Search Store: fileSearchStores/optisignssupportdocs-xxx
2026-07-07 02:01:08 [INFO] Fetched 405 article(s) from Zendesk.
2026-07-07 02:01:08 [INFO] Loaded state for 405 article(s) from state.json.
2026-07-07 02:01:08 [INFO] Delta: 0 added, 3 updated, 402 skipped, 0 deleted.
...
SUMMARY: Added=0 Updated=3 Skipped=402 Deleted=0
```

---

## 7. Tóm tắt kiến trúc GitHub Actions (phiên bản Docker)

```
GitHub Repo
    ├── .github/workflows/daily-sync.yml  ← Định nghĩa workflow
    ├── Dockerfile                         ← Container được build trong CI
    ├── state.json                         ← Được commit bởi bot sau mỗi run
    ├── main.py                            ← Pipeline chính (chạy bên trong container)
    ├── scraper.py                         ← Scrape Zendesk
    └── upload_file_search.py             ← Upload to Gemini

Mỗi ngày lúc 02:00 UTC:
    GitHub khởi động Runner (ubuntu-latest)
        → checkout repo (lấy state.json từ lần trước)
        → docker build -t optibot .
        → docker run optibot
              -v workspace:/app/state  ← mount để state.json persist
              -e STATE_FILE=/app/state/state.json
              (main.py chạy bên trong container → ghi state.json vào mount)
        → (if: always()) commit state.json → push
        → (if: always()) check pipeline outcome → fail nếu cần
        → Runner bị xoá

Local testing:
    docker build -t optibot .
    docker run --rm \
      -e GEMINI_API_KEY=... \
      -e STATE_FILE=/app/state/state.json \
      -v ${PWD}:/app/state \
      optibot
```
