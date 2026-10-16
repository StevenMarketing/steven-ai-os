# GBP Integration — Kiến trúc & Kế hoạch (Giai đoạn 4)

> Trạng thái: **KẾ HOẠCH** — chưa triển khai. Không kết nối Google thật, không OAuth consent.
> Nguồn: tài liệu chính thức `developers.google.com/my-business` (tra ngày 2026-10-15).

## 0. Vì sao chưa chạy qua đêm
Không có cơ chế **dừng theo ngân sách USD** trong Hermes v0.22.0, và không có đồng hồ
tổng cho một phiên 6 giờ. Vì vậy **không khởi chạy phiên tự động** (theo yêu cầu chủ dự án).

## 1. API chính thức cần dùng

| Nhu cầu | API | Endpoint chính |
|---|---|---|
| Liệt kê account | My Business Account Management API v1 | `GET /v1/accounts` |
| Liệt kê location | My Business Business Information API v1 | `GET /v1/accounts/{a}/locations` |
| **Reviews** (list/get/reply) | Google My Business **v4 (legacy)** | `GET /v4/accounts/{a}/locations/{l}/reviews` · `.../reviews/{id}` · `PUT .../reviews/{id}/reply` · `DELETE .../reviews/{id}/reply` |
| Reviews nhiều location | v4 | `POST /v4/accounts/{a}/locations:batchGetReviews` |
| Metrics | Business Profile Performance API v1 | `locations.fetchMultiDailyMetricsTimeSeries` |

**Cảnh báo vòng đời (từ trang Deprecation schedule):**
- Performance legacy (`reportInsights`) đã ngừng 2023-03-30 → dùng `fetchMultiDailyMetricsTimeSeries`.
- **Q&A API ngừng 2025-11-03.** Calls API, Health/Insurance đã ngừng.
- Reviews hiện **vẫn ở v4 legacy** (không có sunset được công bố) → rủi ro dài hạn cần theo dõi `latest-updates`.

## 2. Điều kiện truy cập (CHẶN — cần bạn làm)
1. Google Cloud project hợp lệ.
2. **Đăng ký/được Google duyệt** quyền dùng Business Profile APIs (form đăng ký) — không tự động có.
3. Website doanh nghiệp hợp lệ (điều kiện eligibility).
4. OAuth client + scope `https://www.googleapis.com/auth/business.manage` (restricted scope → cần OAuth verification nếu dùng cho bên thứ ba).
5. Tuân thủ policy: chỉ quản lý listing **mình sở hữu/được uỷ quyền**; **phản hồi review phải có uỷ quyền của chủ tiệm**.

## 3. Kiến trúc đề xuất

```
gbp-integration/
├── src/gbp/
│   ├── auth/token_manager.py     # OAuth2 auth-code + offline refresh; token lưu file quyền 600 (KHÔNG commit)
│   ├── client/http_client.py     # httpx; timeout; backoff+jitter; Retry-After; phân loại lỗi
│   ├── client/quota.py           # rate limiter (QPM) — tránh vượt quota
│   ├── models.py                 # Account, Location, Review, Reply (dataclass/pydantic)
│   ├── repo/sqlite_repo.py       # upsert idempotent; UNIQUE(review_name)
│   ├── sync/reviews_sync.py      # phân trang pageToken; batchGetReviews; chống trùng; watermark updateTime
│   ├── sync/state.py             # sync_runs ledger (bắt đầu/kết thúc/lỗi/đếm)
│   └── cli.py                    # lệnh: sync-reviews, list-locations, draft-reply (chỉ NHÁP)
├── sql/schema.sql
├── tests/                        # unit + MOCK (không gọi mạng thật)
│   ├── fixtures/*.json           # trang review, 429, partial error
│   └── test_*.py
└── docs/ARCHITECTURE.md, docs/API-NOTES.md
```

### 3.1 OAuth flow (thiết kế, chưa chạy)
Authorization Code + `access_type=offline` + `prompt=consent` → refresh token.
Token lưu ngoài repo, quyền `600`, không log. Mọi thao tác hiện tại dùng **mock token** trong test.

### 3.2 Đồng bộ reviews + phân trang
```
for account in accounts:
  for location in locations(account):
    pageToken = None
    while True:
      r = GET /v4/accounts/{a}/locations/{l}/reviews?pageSize=50&pageToken={pageToken}
      upsert(reviews)                 # UNIQUE(review_name) → idempotent
      pageToken = r.nextPageToken
      if not pageToken or pages > MAX_PAGES: break
```
- `batchGetReviews` khi quét nhiều location (giảm số request).
- Watermark theo `review.updateTime` để chỉ xử lý thay đổi.

### 3.3 Chống trùng & idempotency
- `reviews.name` (resource id) làm **UNIQUE** → `INSERT ... ON CONFLICT DO UPDATE`.
- `sync_runs(id, started_at, ended_at, status, fetched, upserted, errors)` — ledger kiểm toán.
- Mỗi lần sync có `run_key` (ngày + account) → không chạy trùng.

### 3.4 Xử lý lỗi / retry / quota
- Retry bounded + backoff+jitter cho 429/5xx; tôn trọng `Retry-After`.
- 401 → refresh token 1 lần; 403 quota → dừng + báo.
- Circuit breaker theo location; lỗi từng phần không làm hỏng cả run.

### 3.5 Trả lời review (giới hạn)
- Hệ thống **chỉ tạo bản nháp**. Không publish.
- Publish (khi được duyệt) = `PUT .../reviews/{id}/reply` — **cần phê duyệt rõ ràng của bạn + uỷ quyền của chủ tiệm**.
- Review tiêu cực: luôn chờ duyệt (đúng nguyên tắc Steven AI OS).

## 4. Schema đề xuất (SQLite)
`accounts`, `locations`, `reviews`(name UNIQUE, rating, comment, create_time, update_time, reviewer, raw_json),
`review_replies`(review_name, draft_text, status[draft/approved/published], published_at),
`sync_runs`, `oauth_tokens`, `idempotency_keys`.

## 5. Test
- **Mock toàn bộ HTTP** (không gọi Google). Fixtures: phân trang 2 trang, 429 rồi thành công, 401 rồi refresh, partial error.
- Test chống trùng: chạy sync 2 lần → số review không đổi.
- Test pagination cap, backoff, upsert.

## 6. Phân chia công việc qua Kanban (khi được duyệt)
| Task | Assignee | Nội dung |
|---|---|---|
| T1 | coding-agent | client + retry/backoff + quota + tests |
| T2 | coding-agent | models + SQLite schema + repo upsert/dedup + tests |
| T3 | gbp-agent | reviews_sync + pagination + fixtures/tests + docs API notes |
| T4 | coding-agent | token_manager (mock) + cli draft-reply + tests |
Chạy tuần tự/bounded, tối đa 2 worker đồng thời, 1/agent, `--max-runtime 300`, `--max-retries 1`.

## 7. Ước tính chi phí (mềm, KHÔNG đảm bảo cứng)
Mỗi task worker ~$0.001–0.005 → 4 task ≈ **$0.004–0.02**; cộng lượt orchestrator. Nằm dưới $0.20
nếu giới hạn ~4–6 task, nhưng **không có cơ chế chặn cứng**.

## 8. Việc CHẶN — cần chủ dự án phê duyệt/cung cấp
1. Google Cloud project + đăng ký Business Profile APIs (Google duyệt).
2. OAuth client id/secret + quyết định scope `business.manage`.
3. Xác nhận có/không cơ chế dừng ngân sách (hiện KHÔNG có) và mức chấp nhận rủi ro.
4. Project runner.

## 9. Web management UI — V2 app shell (Group 7 / T18)
`web/index.html`, `web/styles.css`, `web/app.js` — plain HTML/CSS/JS, no framework, no CDN, no build step.
Served read-only by `src/gbp/api/server.py` from the top-level `web/` dir (CSP `'self'` only, no inline script/style).

- Shell: fixed left sidebar (desktop >=1280px) with the 8 nav items
  Overview, Locations, Review Inbox, AI Drafts, Needs Approval, Approved, Rejected, Audit History,
  plus System Status, Settings and the dashboard version string (`v2.0.0`) in the bottom group.
- Responsive: below 1280px the sidebar becomes an off-canvas drawer (`#nav-toggle`, `#nav-overlay`,
  Escape to close); <=700px data tables stack into cards (they never overflow horizontally).
- Routing: `?area=<value>`. Old values keep working; nav labels map onto the existing 8 areas via
  `AREA_ALIASES` in `web/app.js` (Overview->dashboard, Review Inbox->inbox, AI Drafts->drafts,
  Needs Approval->needs_review, Audit History->audit, plus System Status/Settings shell panels).
- Every panel has loading (skeleton/spinner), empty, error (with the API message) and
  "Not tracked"/"No data available" states. No invented numbers/placeholder data.
- i18n: a single `STRINGS = { en: {...} }` map with `DEFAULT_LOCALE = "en"`; add a `vi` map later,
  no call-site changes. The DRAFT MODE banner text is pinned and must not change.

### What T19 (next) must build on
- Extend the shell, do not replace it: keep `class="tab"` on exactly the 8 primary nav buttons and the
  exact banner string "DRAFT MODE — nothing is published to Google" (both are test-pinned).
- Add new user-facing text through `STRINGS.en` / `t()`; keep `?area=` back-compat (add aliases to
  `AREA_ALIASES`, canonical areas stay the 8 in `AREAS`).
- Reuse `renderLoading`/`renderEmpty`/`renderErrorState` for any new panel; never hardcode numbers.

## Worker safety summary

Before a worker run may open a pull request it passes through these controls, in order:

- **Budget gate** — runs before the run starts: `scripts/worker_gate.py` decides the provider-aware daily budget and the issue gate and refuses the run outright if either layer says no.
- **Atomic claim with TTL** — the issue is claimed atomically with a short time-to-live, so overlapping ticks cannot work the same issue twice and a crashed run frees its claim.
- **Mandatory diff gate** — `scripts/issue_guard.py verify-diff` must pass against `origin/main` before anything is committed or pushed; the pre-commit and pre-push hooks run the same gate.
- **Branch-only push** — the worker pushes its own branch and nothing else: never a force-push, never a merge, never a deploy, and `main` stays untouched.
- **Telegram notification** — once the outcome is known, `worker_gate.py finish` releases the budget hold, records the real cost and sends the outcome notification.

Labels are the only way a task enters this pipeline, and only the repository owner can add hermes-ready.

