# Báo cáo Quy mô & Độ tin cậy — Nhóm 8B / T27

Trạng thái: **đo lường offline trên dữ liệu mô phỏng**. Không kết nối Google, không deploy,
không đụng dữ liệu thật. Ngày đo: 2026-10-16.

## 1. Môi trường đo

| Thành phần | Giá trị |
|---|---|
| CPU / RAM | 2 vCPU (AMD EPYC 9354P) / 7.9 GB (dùng ~1.7 GB) |
| Kernel | Linux 6.8.0-142-generic (container) |
| Python | 3.14.7 |
| SQLite | 3.53.1 · `sqlite3.threadsafety = 3` |
| Tiến trình | 1 process, server loopback `127.0.0.1`, 1 kết nối SQLite dùng chung |
| DB | **file SQLite tạm** (không phải `:memory:`), có index sẵn trong `sql/schema.sql` |

## 2. Dữ liệu mô phỏng

- **500 location** (`accounts/1001/locations/loc-0001…0500`), 5 bang, có `postal_code`, brand profile.
- **2.000 review** (4/location, chia 2 trang để kiểm tra phân trang + resume), có `createTime`/`updateTime`.
- Dung lượng DB: **0,27 MB** (chỉ location) → **1,57 MB** (+2.000 review).

## 3. Hiệu năng API (20 lần đo/endpoint)

| Endpoint | p50 | p95 | p99 | max | bytes |
|---|---:|---:|---:|---:|---:|
| `/api/locations?page=1&page_size=25` | 28,35 | 34,27 | 34,59 | 34,59 | 13.512 |
| `/api/locations?page=20&page_size=25` | 29,55 | 32,30 | 34,09 | 34,09 | 13.425 |
| `/api/locations?q=Bench Nails 250` (tên) | 28,90 | 32,24 | 32,70 | 32,70 | 641 |
| `/api/locations?q=90250` (ZIP) | 29,94 | 35,58 | 36,81 | 36,81 | 641 |
| `/api/locations?state=CA` (lọc) | 28,35 | 32,18 | 34,67 | 34,67 | 13.519 |
| `/api/locations?sort=unanswered&order=desc` | 28,61 | 31,13 | 31,43 | 31,43 | 13.394 |
| `/api/reviews?page=1&page_size=25` | 0,74 | 1,06 | 1,31 | 1,31 | 105 |
| `/api/dashboard` | 13,00 | 14,19 | 14,28 | 14,28 | 758 |
| `/api/locations/1/sync-runs` | 28,64 | 30,26 | 31,05 | 31,05 | 102 |

### Tối ưu đã thực hiện trong T27 (đo trước/sau)

| Endpoint | Trước | Sau | Cách sửa |
|---|---:|---:|---|
| `/api/locations…` | ~70 ms | **~28 ms** | `_locations_status()` chạy **~5 truy vấn/location** (500 location ⇒ ~2.500 truy vấn/request). Nay gộp thành **vài truy vấn GROUP BY** + **một lượt quét ledger**. |
| `/api/dashboard` | ~52 ms | **~13 ms** | dùng chính hàm trên |

**Chi phí cố định còn lại (~28 ms)**: endpoint vẫn dựng đủ 500 dòng rồi mới lọc/phân trang trong Python.
Tối ưu tiếp (đẩy `q`/`state`/phân trang xuống SQL) là việc của giai đoạn sau — hiện p95 < 35 ms nên **chưa cần**.

## 4. Đồng bộ 500 location (hàng đợi có giới hạn đồng thời)

```
locations=500 · ok=500 · failed=0 · fetched=2000 · upserted=2000 · errors=0
peak_in_flight=4/4 (đúng cap) · 15,6 s ⇒ ~128 review/s (offline, 2 vCPU)
rows=2000 · distinct=2000 ⇒ KHÔNG mất dữ liệu · KHÔNG trùng
ledger: sync_runs=500 · sync_run_log=500
checkpoint tồn đọng=0 · published drafts=0
```

## 5. Hợp nhất `sync_runs` ↔ `sync_run_log` (yêu cầu trọng tâm)

**Vấn đề đo được:** `ReviewSync` ghi **`sync_runs`** (1 dòng/run_key, **không có** `location_id`);
Dashboard đọc **`sync_run_log`** (append-only, có `location_id`) — chỉ `SyncState.record_run` ghi.
⇒ Đồng bộ do hàng đợi chạy **vô hình trên Dashboard**, và `consecutive_failures` (nguồn của cờ
`needs_alert`) bị **đếm trùng** khi cả hai phía cùng ghi.

**Cách thống nhất (không mất dữ liệu, không phá vỡ tương thích):**

1. `sync_run_log` là **nguồn chuẩn** (canonical): `ReviewSync` **mirror mỗi lần chạy vào đó**
   (kể cả trường hợp lỗi trước khi bắt đầu walk), ghi theo **`location_resource_id`**.
2. **Bỏ ghi trùng** ở các caller (`sync_all`, hàng đợi) ⇒ mỗi lần chạy **đúng 1 dòng** ⇒ chuỗi lỗi
   liên tiếp đếm chính xác (cảnh báo đúng lúc).
3. `sync_runs` **vẫn được ghi** như cũ (tương thích ngược; test cũ vẫn PASS).
4. **Migration cộng thêm**: `ALTER TABLE sync_runs ADD COLUMN location_id TEXT` (chỉ khi thiếu) +
   **back-fill best-effort** từ tiền tố `run_key`; dòng không suy ra được **giữ NULL, không xoá**.
5. **Bộ đọc hợp nhất** `SyncState.history()`: trả cả dòng `sync_run_log` **và** dòng `sync_runs` chưa từng
   được mirror (⇒ không mất lịch sử cũ), khử trùng theo `run_key`; chấp nhận cả khoá **số** do code cũ ghi.
   API `/api/locations/<id>/sync-runs` dùng bộ đọc này.
6. Dashboard (`/api/locations/status`) tra ledger theo **cả** resource name **và** id số.

**Kiểm chứng:** test `test_sync_is_mirrored_into_the_dashboard_ledger`,
`test_unified_history_keeps_legacy_sync_runs_only_rows`,
`test_history_accepts_legacy_numeric_location_rows`, `test_queue_sync_is_visible_to_the_dashboard`,
`MigrationTests` (legacy table → thêm cột, dữ liệu còn nguyên, back-fill đúng, chạy lại migration không mất dòng).

## 6. SQLite có đáp ứng 500 location?

**Kết luận: CÓ, ở mô hình hiện tại — chưa cần PostgreSQL.** Bằng chứng:

| Tiêu chí | Số đo | Đánh giá |
|---|---|---|
| Dung lượng | 1,57 MB cho 500 location + 2.000 review | rất nhỏ (Postgres sẽ là overkill) |
| Đọc (API) | p95 13–36 ms với 500 dòng | tốt cho dashboard nội bộ |
| Phân trang/tìm/lọc/sắp xếp | đúng ở `total=500`, `pages=20` | đạt |
| Ghi tuần tự | ~128 review/s (1 process, 2 vCPU) | đủ cho 500 location × vài chục review |
| Ghi song song | **không dùng được cùng 1 connection** | xem cảnh báo dưới |

⚠️ **Giới hạn phải nhớ:** Python `sqlite3` **không an toàn khi nhiều luồng dùng chung một connection**
(đo được: `concurrency=2` ⇒ **13% location lỗi** `SystemError`/`InterfaceError`; `concurrency=8` ⇒ **35%**).
Hàng đợi vì vậy **tuần tự hoá mọi lời gọi DB**; `concurrency` chỉ giới hạn pool worker. Muốn ghi song song
thật: mỗi worker một connection riêng, hoặc WAL + một writer. **Không cần đổi sang PostgreSQL.**

## 7. Độ tin cậy (đã kiểm thử)

| Kịch bản | Kết quả |
|---|---|
| Gián đoạn giữa chừng (crash ở trang 2) | Trang 1 **đã được ghi**; checkpoint còn nguyên |
| Resume | Cả 6 location resume từ checkpoint, **24/24 review, 0 trùng, 0 checkpoint tồn** |
| Chạy lại toàn bộ 500 location | **không thêm dòng nào** (idempotent theo khoá) |
| Lỗi 1 location giữa mẻ | Không ảnh hưởng các location khác; ledger ghi `failed` **đúng 1 dòng** |
| RBAC ở quy mô 500 | Reviewer chỉ thấy **3** location được cấp; `GET /api/locations/5` ⇒ **403**; ledger location khác ⇒ **403** |
| Publish | **Mọi trạng thái draft đều bị `publish_guard` chặn**; `published = 0` |

## 8. Rủi ro & việc còn lại

1. **Ghi DB tuần tự** (SQLite 1 connection) — nút thắt nếu cần nhiều writer; chưa cần xử lý ở quy mô này.
2. **Chi phí cố định ~28 ms** cho endpoint location (dựng 500 dòng rồi lọc trong Python) — tối ưu SQL ở giai đoạn sau.
3. **Chưa đo với hạ tầng thật**: mọi số liệu là offline, 2 vCPU, DB tạm; **chưa kiểm thử 500 tài khoản Google thật** và **chưa gọi Google API thật** lần nào.
4. **Quota Google thật chưa xác minh** (chỉ xem được trong Cloud Console).
5. **Lịch sunset của reviews v4 chưa xác thực được** (trang chính thức không render bảng khi thu thập).
6. Tồn đọng từ T25: MFA stub, chưa TLS, lockout policy chưa có rotation.
7. `sync_runs` vẫn tồn tại song song như **bản tóm tắt** (giữ để tương thích); về lâu dài nên coi `sync_run_log` là nguồn duy nhất và có thể ngừng ghi `sync_runs` khi mọi caller đã chuyển.

## 9. Cách tái lập

```bash
cd /opt/data/gbp-integration
PYTHONPATH=src python3 scripts/bench_scale.py              # bảng p50/p95 + sync 500 location
PYTHONPATH=src python3 scripts/bench_scale.py --json       # dạng máy đọc
PYTHONPATH=src python3 -m unittest tests.test_scale_reliability -v
```

Tất cả đều **offline**, dùng DB tạm tự xoá; không kết nối Google, không đụng dữ liệu hiện có.
