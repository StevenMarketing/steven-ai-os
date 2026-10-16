# Ngân sách & định tuyến model — Steven AI OS

Ngân sách được chủ sở hữu phê duyệt: **$3/ngày** cho toàn bộ Steven AI OS (trần,
không phải chỉ tiêu mục tiêu).

Công cụ: `scripts/budget.py` (theo dõi + cưỡng chế) và `scripts/budget_watchdog.py`
(dừng worker khi chạm trần). Cả hai **thuần script, không gọi LLM**, không bao giờ
in hoặc ghi API key.

## 1. Hai nguồn số liệu — luôn tách bạch

| Nguồn | Lấy ở đâu | Ý nghĩa |
|---|---|---|
| **Ước tính (Hermes)** | `sessions.estimated_cost_usd` trong `state.db`, `cost_source=provider_models_api` | ước tính theo bảng giá model; dùng để theo dõi nội bộ |
| **Provider xác nhận (OpenRouter)** | `GET /api/v1/key` → `usage`, `limit`, `usage_daily`, `usage_weekly`, `usage_monthly`, `limit_remaining`, `budgets[]` | **số dùng cho hoá đơn**; có cả số **theo ngày** do provider tính |

`budget.py provider-today` ưu tiên **`usage_daily` của provider** (chính xác tuyệt
đối cho hôm nay); chỉ khi provider không trả trường đó mới dùng **mốc nối tiếp theo
ngày** (`ops/budget/provider-usage.json`) — và mốc này nối tiếp giá trị cuối của
ngày trước nên không "quên" phần đã tiêu.

Số thật lúc kiểm tra: `usage_daily = $1.440867` · `weekly = $4.6183` ·
`monthly = $4.6183` · `limit = $10` · còn `$5.381699` · khoá hết hạn 2027-04-12.

## 2. Số đo thật (đã kiểm chứng trên hệ thống này)

| Việc | Chi phí đo được | Nguồn |
|---|---|---|
| Worker xử lý 1 issue tài liệu (28 API call, 67 message) | **$0.008553** | `sessions.estimated_cost_usd` |
| Một tick cron "thức nhưng không có việc" (1 API call) | **$0.000138** | nt |
| Session Telegram (81 message) | **$0.019663** | nt |
| Session desktop nặng (654 API call, 434 message) | **$2.171618** | nt |
| 30 ngày gần nhất (toàn hệ thống) | **~$2.22** | `hermes insights` |
| Khoá OpenRouter | **usage $4.6183 / limit $10 · còn $5.3817** | `hermes usage` + `/api/v1/key` |

⇒ Một issue tài liệu ≈ **$0.009–0.03**; một issue code ≈ **$0.03–0.06**; một tick
rảnh ≈ **$0.0001**. Với trần $3/ngày, tiền **không phải** là ràng buộc chặn —
ràng buộc thật là số issue/ngày và thời gian chạy.

## 3. Cách dùng

```bash
python3 scripts/budget.py report --days 2          # theo ngày / model / task
python3 scripts/budget.py status                   # trần + số provider xác nhận
python3 scripts/budget.py can-spend --estimate 0.05 --task issue-42   # exit 1 = từ chối
python3 scripts/budget.py record --task issue-42 --cost 0.0086 --model deepseek-v4.1-flash
python3 scripts/budget.py route --task-class docs  # hạng model đề xuất
python3 scripts/budget.py pause --reason "stop"    # kill switch ngân sách
python3 scripts/budget_watchdog.py                 # chỉ báo cáo
python3 scripts/budget_watchdog.py --enforce       # pause worker khi chạm trần
```

## 4. Trần và cách cưỡng chế

| Tham số | Mặc định | Biến môi trường |
|---|---|---|
| Trần ngày | **$3.00** | `STEVEN_DAILY_BUDGET_USD` |
| Trần một task | $0.60 | `STEVEN_TASK_BUDGET_USD` |
| Trần retry mỗi task | 3 | `STEVEN_MAX_ATTEMPTS` |
| Fail-open khi không đọc được provider | **tắt** | `STEVEN_BUDGET_FAIL_OPEN=1` (không khuyến nghị) |

`can_spend()` từ chối khi: đang PAUSED · ước tính > trần task · đã thử đủ số retry ·
đã tiêu + ước tính > trần ngày · **provider báo hết hạn mức** · **không đọc được số
provider (fail-closed)**.

### ⚠️ Sự thật về "hard cap"
Hermes **không** có cơ chế chặn chi tiêu ở cấp provider. Nó *có*:
`hermes insights` (analytics 30 ngày), `hermes usage` (hạn mức + đã dùng theo
ngày/tuần/tháng), `--usage-file` (báo cáo JSON mỗi lần chạy one-shot), và các cột
`estimated_cost_usd` / `actual_cost_usd` / `cost_status` trong `state.db`.
Nhưng **không có** tham số nào bắt provider từ chối khi vượt một ngân sách ngày.

⇒ Chỉ có **hai** lớp chặn thật:
1. **Hạn mức khoá OpenRouter** — trần cứng do provider ép. Hiện tại khoá này có
   `limit = $10` với **`reset_interval = null`** (hạn mức *tổng*, không tự reset).
   🎯 **Đề xuất cụ thể (đã kiểm chứng trường tồn tại trong API):** đặt hạn mức khoá
   thành `limit_usd = 3` với **`reset_interval = "daily"`** trong dashboard
   OpenRouter → khi đó trần **$3/ngày được provider cưỡng chế thật**, không phụ
   thuộc vào code của repo này.
2. **Code của repo này** (`budget.py` + `budget_watchdog.py`) — cưỡng chế trần ngày,
   fail-closed, và là lớp duy nhất kiểm tra được *trước khi* gọi model.

Không được tuyên bố "$3/ngày là bảo đảm tuyệt đối" nếu chỉ dựa vào lớp 2: nó chỉ
chặn những đường đi **có gọi `can_spend()`**. Đề xuất: bật watchdog theo lịch
(một cron riêng chạy script thuần, $0 chi phí AI) hoặc gọi `can-spend` ở đầu mỗi
tác vụ tốn tiền. **Bật lớp 1 thì mới có bảo đảm cứng.**

## 5. Định tuyến model

| Hạng | Model | Dùng cho |
|---|---|---|
| **economy** | `deepseek/deepseek-v4.1-flash` (đang chạy) | tài liệu, polling, triage, fix nhỏ, thêm test, **issue-worker**, báo cáo |
| **strong** | *(chưa cấu hình)* | kiến trúc, review bảo mật, refactor lớn, thiết kế migration |

`route()` **chỉ đề xuất** — module không có đường nào tự đổi model/provider đang
chạy hay tự tăng ngân sách. Muốn nâng hạng phải do chủ sở hữu chọn model/provider
rồi cấu hình (và ghi lại lý do).

## 6. Tham số đề xuất (theo số đo thật)

| Tham số | Đề xuất | Lý do |
|---|---|---|
| Issue/ngày | **3** (giữ nguyên) | trần hiện tại; chưa cần tăng vì $ không phải nút thắt |
| Polling | **1 phút** (đã bật) | phát hiện issue trong ~1–2 phút; 1.440 request/ngày so với 5.000/giờ ⇒ an toàn |
| Timeout worker | **300s** (kanban) / cron ngắt 3 phút | full suite ~167s; runbook có ngân sách thời gian |
| Retry | kanban `failure_limit=2` + `MAX_ATTEMPTS=3` | chặn vòng lặp đốt token |
| Worker đồng thời | **1** | đã cấu hình |
| Dự phòng ngân sách | giữ ~20% trần ngày chưa dùng | tránh chạm trần giữa việc quan trọng |

## 7. Điều module này KHÔNG làm
- Không tự tăng ngân sách, không tự đổi model/provider.
- Không in, không ghi token/key vào log hay sổ cái (có test khẳng định).
- Không tuyên bố bảo đảm tuyệt đối $3/ngày khi chưa có cưỡng chế ở cấp provider.
