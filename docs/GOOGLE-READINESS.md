# Google Business Profile — Integration Readiness (Nhóm 8B / T26)

Trạng thái: **sẵn sàng ở mức kiến trúc + mã nguồn, CHƯA kết nối Google thật.**
Mọi thứ dưới đây đã được kiểm thử **offline** bằng mock transport + fixtures.

## 1. Nguồn tài liệu đã đối chiếu (đọc ngày 2026-10-15)

| Nội dung | Nguồn chính thức |
|---|---|
| Danh mục API + lịch ngừng hỗ trợ | https://developers.google.com/my-business/content/sunset-dates |
| Dữ liệu review (list / get / nhiều location / reply / delete) | https://developers.google.com/my-business/content/review-data |
| `accounts.locations.batchGetReviews` (v4) | https://developers.google.com/my-business/reference/rest/v4/accounts.locations/batchGetReviews |
| REST reference tổng | https://developers.google.com/my-business/reference/rest |

**Xác nhận được:**
- Capability review vẫn nằm trên **host legacy `mybusiness.googleapis.com/v4`**
  (`GET /v4/{account}/locations/{location}/reviews`, `…/reviews/{reviewId}`,
  `…/reply`, và `POST /v4/{account}/locations:batchGetReviews`) — theo đúng
  trang “Work with review data”.
- Các API **v1 (2021+)** phủ: Account Management, Business Information,
  Business Calls, Lodging, Media, Notifications, Performance, Q&A.

**CHƯA xác minh được (không suy đoán):**
- **Không** đọc được bảng ngày sunset cho các method review v4 trên trang
  sunset-dates (bảng không render khi thu thập). ⇒ Coi rủi ro “Google ngừng v4”
  là **CHƯA XÁC MINH**, không phải “an toàn”.
- Hạn mức (quota) thật theo project **chỉ xem được trong Google Cloud Console**,
  không kiểm chứng được offline.
- Q&A API đã được ghi nhận ngừng từ 2025-11-03 ở giai đoạn trước; tài liệu này
  không dùng Q&A.

## 2. Bản đồ endpoint → module

| Endpoint | Module | Ghi chú |
|---|---|---|
| `GET /v1/accounts` | `client/gbp_client.py::list_accounts` | Account Management v1 |
| `GET /v1/{account}/locations` | `gbp_client.py::list_locations` | đi hết `nextPageToken` |
| `GET /v4/{location}/reviews` | `gbp_client.py::list_reviews` | `pageSize=50`, token phân trang |
| `GET /v4/{review_name}` | `gbp_client.py::get_review` | |
| `POST /v4/{account}/locations:batchGetReviews` | `gbp_client.py::batch_get_reviews` | gộp nhiều location |
| `PUT /v4/{review_name}/reply` | **KHÔNG có đường gọi** (DRAFT MODE) | chỉ tồn tại như hợp đồng API |

## 3. OAuth (chuẩn bị, chưa chạy thật) — `src/gbp/auth/oauth.py`

- Scope: **`https://www.googleapis.com/auth/business.manage`** — scope **RESTRICTED**,
  phải được Google phê duyệt cho Cloud project trước khi token thật hoạt động.
- `build_authorization_url(client_id, redirect_uri, state=…)` sinh URL consent với
  `access_type=offline` + `prompt=consent` (bắt buộc để có refresh token cho đồng bộ tự động).
- `exchange_code(transport, …)` đổi authorization code lấy token **qua transport được tiêm vào**
  (mock trong test) — module **không tự mở socket**.
- `google_connection_status()` trả `connected=False` khi chưa có token thật
  (và **không bao giờ** trả giá trị token).
- **Chốt an toàn:** một transport **live** bị **từ chối** trừ khi đặt
  `GBP_ALLOW_LIVE_GOOGLE=1`. `MockTransport` luôn được phép.
- Đã sửa bug: `has_live_credentials()` trước đây coi **OpenRouter API key** là
  “có credentials Google” ⇒ nay chỉ tính **token Google** thật.

## 4. Đồng bộ nhiều location — `src/gbp/sync/queue.py`

`LocationSyncQueue` (mới; trước đây hàng đợi chỉ tồn tại trong script benchmark):

| Thuộc tính | Cách làm |
|---|---|
| **Concurrency cap** | pool giới hạn `concurrency` (mặc định 2), báo cáo `peak_in_flight` để assert |
| **Pagination** | `ReviewSync` đi `nextPageToken`; có trần số trang chống vòng lặp token |
| **Retry + backoff** | tối đa `max_attempts`, backoff mũ có trần |
| **`Retry-After`** | nếu lỗi mang `retry_after` thì **tôn trọng giá trị server** (giới hạn bởi trần) |
| **Idempotency** | upsert theo **review resource name**; chạy lần 2 ⇒ 0 upsert mới |
| **Resume sau lỗi** | checkpoint `nextPageToken` được **giữ nguyên** khi lỗi ⇒ lần chạy sau tiếp tục từ checkpoint |
| **Cô lập lỗi** | 1 location lỗi **không** làm hỏng cả mẻ; ghi `sync_run_log` (status `failed`) |
| **Định danh** | chỉ dùng **`location_resource_id`** (`accounts/…/locations/…`); tên tiệm và ZIP **không** là khóa |

## 5. Kế hoạch 500 location (đã mô phỏng offline)

1. Liệt kê account → locations (v1, phân trang).
2. Đưa danh sách `location_resource_id` vào `LocationSyncQueue` (cap 2–4 tuỳ quota).
3. Mỗi location: watermark `updateTime` + checkpoint trang trong `sync_state`.
4. Lỗi từng location → ledger + đếm `consecutive_failures` → cờ `needs_alert` trên Dashboard.
5. Chạy lại an toàn: idempotent, resume từ checkpoint.
6. Cảnh báo location lỗi liên tục; **không** tự publish bất cứ thứ gì.

## 6. Việc còn phải làm trước khi kết nối thật (cần bạn phê duyệt)

1. Google Cloud project + **OAuth client id/secret** (không tự tạo key).
2. **Phê duyệt scope `business.manage`** (restricted) — ngoài tầm kiểm soát.
3. Xác nhận **quota** thật trong Cloud Console + kế hoạch xin tăng cho 500 location.
4. **Token lưu ngoài repo**, mode `600`, có `GBP_ALLOW_LIVE_GOOGLE=1` khi vận hành.
5. **TLS + `GBP_COOKIE_SECURE=1`** trước khi chạy trên hạ tầng thật.
6. Xác nhận lại **lịch sunset của reviews v4** (chưa xác minh được ở bước này).

## 8. Phát hiện & sửa trong T26 (số đo thật)

| Phát hiện | Bằng chứng | Xử lý |
|---|---|---|
| **Repo KHÔNG có transport thật** | chỉ tồn tại `MockTransport`; `_live_client()` gọi `HttpClient(timeout=…)` thiếu tham số bắt buộc ⇒ `TypeError`, đường live chưa từng chạy được | thêm `client/urllib_transport.py` (stdlib `urllib`, chốt an toàn kép theo host Google) |
| **`has_live_credentials()` nhận nhầm API key AI** | chỉ cần export `OPENROUTER_API_KEY` là bị coi như "có credential Google" | nay chỉ tính **token Google** thật |
| **SQLite không an toàn đa luồng** | 200 location: `concurrency=2` → **13% location lỗi** (`SystemError`/`InterfaceError`); `concurrency=8` → **35%** | hàng đợi **tuần tự hoá mọi lời gọi DB** bằng một lock; `concurrency` chỉ giới hạn pool worker |
| **Resume làm MẤT dữ liệu** | checkpoint tiến lên trước khi review được ghi; resume từ trang 2 ⇒ **mất 2/4 review** | **ghi theo từng trang** (streaming upsert) ⇒ checkpoint luôn mô tả dữ liệu đã lưu thật |
| **`sync_location` không dùng checkpoint** | docstring nói "resume" nhưng code không truyền `start_token`/`on_page` (chỉ `incremental_sync` có) | đã nối checkpoint + trả `resumed_from` |
| **Hai ledger khác nhau** | `ReviewSync` ghi `sync_runs`; Dashboard đọc `sync_run_log` (chỉ `SyncState` ghi) ⇒ sync qua queue **vô hình** trên Dashboard | ✅ **ĐÃ SỬA ở T27**: mirror mỗi lần chạy vào `sync_run_log` (nguồn chuẩn), bỏ ghi trùng, migration cộng thêm `sync_runs.location_id`, bộ đọc hợp nhất `SyncState.history()` — xem `docs/SCALE-REPORT.md` §5 |
| **Queue báo sai số lần thử** | lỗi không-retry vẫn báo `attempts = max_attempts` | đã sửa |

### Bằng chứng đo được (500 location, offline, dữ liệu tổng hợp)
`ok=500 · failed=0 · fetched=2000 · upserted=2000 · errors=0 · peak_in_flight=4 (đúng cap) ·
0.133s · rows=2000 distinct=2000 (không mất, không trùng) · ledger 500 · 0 checkpoint tồn · published=0`

## 7. Không được làm (đang giữ đúng)

- ❌ Không kết nối Google thật, không OAuth consent thật.
- ❌ Không publish / không gọi `PUT …/reply`.
- ❌ Không deploy, không đổi VPS/Hermes/model/provider.
- ❌ Không hardcode credential; không ghi token vào log.
