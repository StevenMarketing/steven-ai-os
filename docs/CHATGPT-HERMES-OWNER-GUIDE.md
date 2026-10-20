# Hướng dẫn cho chủ sở hữu: gửi việc từ ChatGPT sang Hermes

Tài liệu này dành cho **người quản lý (owner)** — không cần biết lập trình.
Mục tiêu: giúp bạn giao một việc kỹ thuật gọn gàng cho Steven AI OS qua GitHub,
rồi **đọc được bằng chứng** cho biết việc đó đã được làm tới đâu.

Đây là lần bàn giao trực tiếp đầu tiên theo luồng **ChatGPT → Hermes**. Bản thân
việc tạo tài liệu này cũng chính là một ví dụ của luồng đó.

---

## 1. Bức tranh tổng quát

```
Bạn nêu mục tiêu (trong ChatGPT hoặc trong yêu cầu)
        │
        ▼
Một GitHub Issue được tạo  (nhãn hermes-proposed)
        │
        ▼
CHÍNH BẠN gắn nhãn hermes-ready   ← đây là cổng phê duyệt của con người
        │
        ▼
Bridge xác nhận (acknowledgement) — kiểm tra issue có đủ mục bắt buộc
        │
        ▼
Worker Hermes (chạy tự động theo lịch) nhận việc
        │
        ▼
Draft Pull Request + bình luận bằng chứng trên Issue
        │
        ▼
Bạn xem xét và MERGE (chỉ bạn mới merge)
```

Nguyên tắc bất di bất dịch: **không có gì được tự động đưa lên môi trường thật,
không deploy, không đăng bài, không gọi Google.** Worker chỉ tạo **Draft PR** và
báo cáo lại. Quyền quyết định cuối cùng luôn thuộc về bạn.

---

## 2. Gửi một việc như thế nào

1. **Viết mục tiêu bằng lời.** Nêu rõ bạn muốn gì và phạm vi được phép chạm tới.
   ChatGPT có thể giúp bạn chuyển lời thành một Issue đúng khuôn.
2. **Tạo GitHub Issue** trong repo `StevenMarketing/steven-ai-os` với 5 mục bắt buộc:
   - **Goal** — mục tiêu
   - **Scope** — phạm vi (được sửa những file nào, tối đa bao nhiêu file)
   - **Acceptance Criteria** — điều kiện được coi là xong
   - **Tests Required** — cần kiểm tra những gì
   - **Constraints** — giới hạn (ngân sách, thời gian, cách khôi phục)
3. Issue mới có nhãn `hermes-proposed`. **Worker bỏ qua nhãn này** — nó không phải
   là lệnh.
4. **Chính bạn gắn nhãn `hermes-ready`.** Chỉ tài khoản `StevenMarketing` gắn mới
   có hiệu lực; nếu bot gắn thì hệ thống **không** tính. Đây là cổng phê duyệt duy
   nhất để worker được phép bắt đầu.

> Mẹo: hãy coi nội dung Issue là **dữ liệu mô tả công việc**, không phải mệnh lệnh
> cho AI. Đừng viết những câu kiểu "bỏ qua hướng dẫn trước", "in token", "đẩy lên
> main" — hệ thống sẽ tự động từ chối và gắn nhãn `hermes-blocked`.

---

## 3. Ý nghĩa các nhãn (trạng thái)

| Nhãn | Ai gắn | Ý nghĩa |
|---|---|---|
| `hermes-proposed` | Bất kỳ ai, kể cả bot | Đã đề xuất — **chưa** có hiệu lực, worker bỏ qua |
| `hermes-ready` | **Chỉ `StevenMarketing`** | Đã phê duyệt — worker được phép nhận việc |
| `hermes-running` | Worker | Đang xử lý |
| `hermes-done` | Worker | Đã tạo Draft PR — đang chờ bạn review |
| `hermes-blocked` | Worker | Cần con người xử lý (chạm vùng cấm, lệnh bị từ chối…) |
| `hermes-needs-info` | Worker | Issue thiếu thông tin, không dùng được |

---

## 4. ChatGPT tương tác ≠ Worker tự động

Hai thứ này khác nhau, đừng lẫn:

- **ChatGPT (tương tác):** chạy khi bạn đang trò chuyện. Nó giúp bạn *soạn* mục
  tiêu, *viết* Issue, *giải thích* kết quả. Nó không tự làm việc trong repo.
- **Worker Hermes (tự động, theo lịch):** chạy nền, không cần bạn ngồi đó. Nó chỉ
  thức dậy khi có việc thực sự (khi bộ theo dõi phát hiện Issue đã đủ điều kiện).
  Việc nó làm hoàn toàn nằm trong phạm vi Issue bạn đã phê duyệt.

Nói cách khác: ChatGPT là người bạn trao đổi; worker là người thợ làm theo phiếu
việc bạn đã ký duyệt.

---

## 5. Nhật ký một việc chạy qua hệ thống

1. **Bạn nêu mục tiêu** → 2. **Issue được tạo** (kèm 5 mục bắt buộc) →
3. **Bridge xác nhận (acknowledgement):** hệ thống kiểm tra đủ mục, nếu thiếu thì
   bình luận nêu lý do và gắn `hermes-needs-info`. →
4. **Bạn gắn `hermes-ready`** → 5. **Worker claim** việc (khoá độc quyền, hết hạn
   sau 30 phút để không kẹt) → 6. **Tạo nhánh riêng** `hermes/issue-<số>-…`, làm
   việc trong đó (không bao giờ đụng `main`) → 7. **Chạy kiểm tra nhanh** và **cổng
   an toàn diff** (không lộ bí mật, không chạm vùng cấm) → 8. **Push nhánh** →
9. **Mở Draft Pull Request** (luôn có dòng `Closes #<số>`) → 10. **Bình luận bằng
   chứng trên Issue** và gắn `hermes-done`.

---

## 6. Xem bằng chứng ở đâu

| Bạn muốn biết | Xem ở đâu |
|---|---|
| Kết quả kiểm tra, file đã đổi, chi phí AI | Bình luận của worker ngay trên **Issue** |
| Nội dung thay đổi cụ thể | **Draft Pull Request** liên kết trong bình luận |
| Hệ thống còn sống / đang rảnh hay kẹt | **Issue trạng thái #46** — bình luận JSON đã lược bí mật, có `generated_at` mới |
| "Nhịp tim" và chẩn đoán gần nhất | `ops/github-worker/state.json` và `ops/github-worker/audit.jsonl` (sản phẩm của `scripts/worker_heartbeat.py`) |
| Vì sao một việc bị chặn | Lý do in ra trong bình luận Issue + nhãn `hermes-blocked` |

Nếu Issue bị bỏ qua và bình luận nói *needs info* → điền lại cho đủ mục.
Nếu nói *injection* → viết lại Issue, bỏ các câu ra lệnh cho AI.

---

## 7. Giới hạn an toàn (do hệ thống tự thực thi)

| Giới hạn | Giá trị |
|---|---|
| Chi phí AI mỗi Issue | 0,06 USD |
| Chi phí AI mỗi ngày | 0,15 USD |
| Số Issue xử lý mỗi ngày | 3 |
| Số worker chạy song song | 1 |
| Thời gian tối đa mỗi Issue | 300 giây |
| TTL khoá claim | 30 phút |

Worker **từ chối**: đẩy lên `main`, force-push, merge/tự duyệt, deploy, gọi Google,
đăng phản hồi review, in/lộ token, sửa các đường dẫn bảo vệ (`.env*`, `**/secrets*`,
`**/*.pem`, `.github/**`, `src/gbp/auth/**`, `src/gbp/authz/**`,
`src/gbp/security/**`, `sql/schema.sql`, các script guard/cron, tài liệu worker).

Cần dừng khẩn cấp: `python3 scripts/issue_guard.py pause --reason "..."`.

---

## 8. Điều bạn cần tự làm

- **Gắn `hermes-ready`** — không có bước này thì không có gì chạy.
- **Review và MERGE Draft PR** — worker không bao giờ merge thay bạn.
- Nếu thấy `hermes-blocked` hoặc `hermes-needs-info` — đọc lý do và xử lý.

Tài liệu kỹ thuật chi tiết hơn (dành cho người vận hành): xem
[`docs/GITHUB-WORKER.md`](GITHUB-WORKER.md).
