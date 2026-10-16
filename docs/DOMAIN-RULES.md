# DOMAIN-RULES — Nghiệp vụ AI Review Reply (GBP, tiệm nail Mỹ)

> Trạng thái: ĐẶC TẢ (offline). Tài liệu này định nghĩa luật nghiệp vụ cho module soạn
> phản hồi review. Hệ thống **CHỈ TẠO NHÁP** — không publish, không kết nối Google.
> Tham chiếu schema: `sql/schema.sql` (`reviews`, `reply_drafts`, `approval_records`,
> `brand_profiles`, `audit_logs`); tham chiếu model: `src/gbp/models.py`.

## 0. Nguyên tắc nền
- Mọi phản hồi do AI soạn đều là **draft**. Không có ngoại lệ tự publish trong giai đoạn hiện tại.
- Mọi phản hồi phải bám **nội dung thật** của review. Không có dữ kiện → không khẳng định.
- Thiếu thông tin (thiếu Brand Profile, thiếu service, thiếu ngữ cảnh) → chọn **phản hồi trung tính**, an toàn.
- Không bao giờ trộn dữ liệu giữa các tiệm (xem §6).

## 1. Phân loại review theo số sao và cách xử lý

Thang sao: GBP trả `starRating` dạng chuỗi (`ONE`..`FIVE`); lưu DB dạng số `rating` 1..5.

| Nhóm | Điều kiện | Hành vi bắt buộc | Trạng thái draft khởi tạo |
|---|---|---|---|
| 5 sao | `rating == 5` | Soạn nháp cảm ơn, gắn 1 chi tiết thật trong review. Được phép auto-approve **chỉ khi** `auto_publish_allowed = 1` VÀ không thuộc nhóm nhạy cảm. | `needs_review` (mặc định) |
| 4 sao | `rating == 4` | Soạn nháp cảm ơn + ghi nhận điểm chưa hài lòng nếu có. Không hứa hẹn bù đắp. Luôn cần người duyệt. | `needs_review` |
| 3 sao | `rating == 3` | Trung tính: cảm ơn, ghi nhận, mời liên hệ trực tiếp. Không giải thích/hợp lý hoá. Luôn cần người duyệt. | `needs_review` |
| 1–2 sao | `rating in (1, 2)` | **BẮT BUỘC phê duyệt thủ công.** Không auto-approve kể cả khi `auto_publish_allowed = 1`. Nháp: xin lỗi ngắn, không đổ lỗi, không tranh luận, không hứa hoàn tiền/giảm giá, đưa kênh liên hệ riêng. | `needs_review` |
| Chỉ có số sao | `comment` rỗng/`null` | Không bịa chi tiết. Nháp ngắn, dùng đúng mức sao (5: cảm ơn; ≤3: mời chia sẻ thêm qua kênh riêng). | `needs_review` |
| Nhạy cảm | xem §1.1 | **BẮT BUỘC phê duyệt thủ công + cờ cảnh báo.** Nháp tối thiểu, trung tính, hoặc đề xuất "không phản hồi tự động". | `needs_review` + `flag` |

### 1.1 Tiêu chí review nhạy cảm (bất kỳ dấu hiệu nào → cờ `sensitive`)
- Cáo buộc liên quan **sức khoẻ/an toàn** (nhiễm trùng, chảy máu, bỏng, dụng cụ bẩn).
- Cáo buộc **phân biệt đối xử** (chủng tộc, giới tính, ngôn ngữ, ngoại hình).
- **Đe doạ**, quấy rối, xúc phạm, thù địch.
- **Pháp lý/tài chính**: doạ kiện, khiếu nại cơ quan quản lý, đòi hoàn tiền, doạ đăng báo/review xấu.
- **Thông tin cá nhân** (PII) của nhân viên/khách: số điện thoại, địa chỉ, tên đầy đủ ngoài tên hiển thị, thông tin y tế.
- **Spam/quảng cáo đối thủ**, nội dung không liên quan tiệm, nghi review giả/đối thủ.
- Đề cập **tên nhân viên** — không nhạy cảm nhưng bắt buộc kiểm tra §2/§7 trước khi nhắc lại tên.

### 1.2 Quy tắc chung theo mức sao
- Không tranh luận, không phủ nhận cảm nhận của khách ("Bạn đã sai", "Chúng tôi chưa từng...").
- Không hứa hành động cụ thể chưa được chủ tiệm xác nhận (bù, miễn phí, sửa lại).
- Độ dài mục tiêu: 2–4 câu (5 sao), 3–5 câu (≤3 sao). Không vượt 600 ký tự.
- Ngôn ngữ trả lời = ngôn ngữ của review (xem §6 language).

## 2. Quy tắc Local SEO

### 2.1 Được phép dùng (CHỈ khi khớp nội dung review)
- **Business Name** (`brand_profiles.business_name`).
- **City / Local Area** — đúng thành phố của tiệm đang được review.
- Nhóm từ dịch vụ: `Nail Salon`, `Manicure`, `Pedicure`, `Gel Nails`, `Acrylic Nails`, `Dip Powder`, `Nail Art`.
- **Dịch vụ đã xác minh** (nằm trong `services_json` của tiệm đó).

Điều kiện dùng: từ khoá phải **xuất hiện trong hoặc suy ra trực tiếp từ** review (khách nhắc dịch vụ đó),
hoặc là **nhóm ngành trung tính** dùng tối đa 1 lần. Không nhét từ khoá khách không hề nhắc.

### 2.2 Cấm
- **Nhồi từ khoá**: ≥2 từ khoá SEO trong một câu; tổng >3 keyword/lần phản hồi.
- **Lặp tên tiệm** quá 1 lần, hoặc **lặp tên thành phố** quá 1 lần trong cùng phản hồi.
- **Bịa**: nhân viên (không có trong review và không có trong dữ liệu xác minh), dịch vụ chưa xác minh,
  địa điểm/chi nhánh khác, trải nghiệm của khách, thời gian/phòng ốc/quy trình không được nêu.
- **Hứa ưu đãi**: giảm giá, hoàn tiền, voucher, miễn phí, "lần sau em giảm cho anh/chị" khi chưa được
  chủ tiệm cho phép rõ ràng (không có cơ chế cho phép → coi như chưa được phép).
- Cam kết về **thời gian chờ**, **giá**, **kết quả tuyệt đối** ("không bao giờ hỏng", "đẹp mãi").
- Dịch máy thô: câu SEO phải đọc tự nhiên như người viết.

### 2.3 Nguồn dữ liệu
- Chỉ dùng `brand_profiles` của **đúng location** đang review (`location_id` khớp).
- Không suy diễn từ tiệm khác cùng chuỗi. Không suy diễn từ tên tiệm nếu profile trống.

## 3. Chống trùng lặp phản hồi

Áp dụng khi soạn một phản hồi mới: so với các phản hồi **trước đó của cùng location** trong cửa sổ
**90 ngày** (dùng `reviews.reply_text` đã lưu).

### 3.1 Chỉ số (metrics)
- **N-gram overlap**: tách từ đã chuẩn hoá (lowercase, bỏ dấu câu), lấy **4-gram**; đo **Jaccard**
  giữa tập 4-gram của phản hồi mới và tập 4-gram của từng phản hồi cũ → lấy **giá trị lớn nhất**.
- **Prefix lặp**: 8 từ đầu giống hệt một phản hồi cũ → đếm là trùng nặng.
- **Câu mở/kết trùng**: câu đầu hoặc câu cuối giống hệt một phản hồi cũ.
- **Boilerplate redundancy**: các câu chỉ khác tên người/thành phố được coi là trùng nhau.

### 3.2 Ngưỡng đề xuất
| Jaccard 4-gram (max) | Kết luận | Hành động |
|---|---|---|
| ≤ 0.35 | PASS | Chấp nhận |
| 0.35 < x ≤ 0.50 | WARN | Cho duyệt, nhưng bắt buộc viết lại ít nhất 1 câu |
| > 0.50 | FAIL | Viết lại; nếu vẫn > 0.50 sau 2 lần → chuyển người duyệt viết tay |

Bổ sung: nếu **prefix 8 từ trùng** HOẶC **câu mở trùng hệt** → tự động FAIL bất kể Jaccard.
Không lặp nguyên văn câu cảm ơn chuẩn; dùng biến thể dựa trên chi tiết thật của review.

## 4. Checklist chất lượng 7 mục

Mỗi mục gồm câu hỏi kiểm tra và tiêu chí PASS/FAIL. Draft chỉ được `approved` khi **cả 7 mục PASS**
(mục 7 Safety là điều kiện chặn cứng).

| # | Mục | Câu hỏi kiểm tra | PASS | FAIL |
|---|---|---|---|---|
| 1 | Relevance | Phản hồi có trả lời đúng điều khách nêu không? | Mọi ý chính của review được đề cập hoặc ghi nhận. | Bỏ qua ý chính; trả lời lạc đề; chào chung chung. |
| 2 | Personalization | Có ít nhất 1 chi tiết riêng của review (dịch vụ, tên nhân viên khách nêu, tình huống) không? | Có ≥1 chi tiết thật, đúng. | Không có chi tiết nào; hoặc chi tiết không có trong review. |
| 3 | Local SEO | Từ khoá dùng có khớp review và đúng tiệm không? | ≤3 keyword, đều khớp; tên tiệm/thành phố ≤1 lần mỗi loại. | Nhồi keyword; sai thành phố; keyword không liên quan. |
| 4 | Accuracy | Có khẳng định nào không có cơ sở không? | Không có; mọi khẳng định truy được về review/brand profile. | Bịa nhân viên/dịch vụ/địa điểm/trải nghiệm; hứa ưu đãi. |
| 5 | Tone | Giọng có đúng brand voice và phù hợp mức sao không? | Đúng `tone`; ≤3 sao: đồng cảm, không phòng thủ. | Thân mật quá trớn với review 1 sao; tranh luận; máy móc. |
| 6 | Duplication | Jaccard 4-gram max có ≤0.35 và không trùng prefix/câu mở? | ≤0.35 và không trùng prefix/câu mở. | >0.50, hoặc trùng prefix 8 từ, hoặc câu mở trùng hệt. |
| 7 | Safety | Có PII, cáo buộc nhạy cảm, hoặc tuyên bố về thứ hạng Google không? | Không PII; không cam kết SEO/thứ hạng; case nhạy cảm có cờ + người duyệt. | Lộ PII; cam kết tăng thứ hạng; case nhạy cảm bị auto. |

Ghi chú chấm điểm: một mục WARN ở #3 hoặc #6 không tự động FAIL, nhưng nếu có ≥2 WARN → coi như FAIL
và yêu cầu viết lại.

## 5. State machine của draft

Bảng `reply_drafts.status` (CHECK constraint): `draft | needs_review | approved | rejected`.
`published` là trạng thái của **giai đoạn sau** (khi có uỷ quyền publish) — hiện **không** dùng.

```
draft ──(AI soạn xong)──► needs_review ──(người duyệt: approve)──► approved
                              │                                        │
                              │                                        └─(giai đoạn sau)──► published
                              ├──(người duyệt: reject)──► rejected
                              └──(hết hạn / review bị sửa)──► draft (soạn lại)
```

| Chuyển trạng thái | Điều kiện | Ai được đổi | Bản ghi audit |
|---|---|---|---|
| `draft → needs_review` | Đã soạn xong text, đã chạy checklist §4 (kết quả lưu kèm). | Hệ thống (worker AI) | `action = "draft_created"`, `actor = "system"` |
| `needs_review → approved` | Checklist 7/7 PASS; case 1–2 sao & nhạy cảm chỉ được duyệt thủ công; `note` lý do. | Người có thẩm quyền (chủ tiệm/ops) | `action = "approved"`, `actor = <user>` |
| `needs_review → rejected` | Lý do sai nội dung/PII/tone/trùng lặp hoặc chủ tiệm từ chối. Bắt buộc có `note`. | Người có thẩm quyền (chủ tiệm/ops) | `action = "rejected"`, `actor = <user>` |
| `approved → published` (giai đoạn sau) | Chỉ khi `auto_publish_allowed = 1` HOẶC có uỷ quyền publish rõ ràng; **không** áp dụng cho 1–2 sao/nhạy cảm. | Hệ thống publish (tương lai) | `action = "published"`, `actor = "system"` |
| `needs_review → draft` | Review bị sửa (`update_time` thay đổi) hoặc hết hạn duyệt. | Hệ thống | `action = "requeued"` |
| Bất kỳ → `draft` (soạn lại) | Người duyệt yêu cầu viết lại. | Người có thẩm quyền | `action = "changes_requested"` |

Quy tắc bổ sung:
- **Không** có đường tắt `draft → approved`; phải qua `needs_review`.
- `approved`/`rejected` là trạng thái cuối của một vòng; đổi lại phải tạo hành động mới có audit.
- Mỗi lần chuyển trạng thái ghi 1 dòng `approval_records(draft_id, action, actor, decided_at, note)`
  và 1 dòng `audit_logs(ts, actor, action, entity='reply_draft', entity_id, detail_json)`.
- Không xoá cứng draft đã `approved`/`rejected`; giữ lịch sử.

## 6. Đặc tả trường Brand Profile (mỗi tiệm)

Một bản ghi `brand_profiles` cho mỗi `location_id` (UNIQUE). Trường lưu JSON dạng TEXT.

| Trường | Kiểu | Bắt buộc | Ý nghĩa / ràng buộc |
|---|---|---|---|
| `location_id` | INT (FK, UNIQUE) | Có | Khoá nối tới `business_locations.id`. **Nguồn chân lý duy nhất** để tách dữ liệu tiệm. |
| `business_name` | TEXT | Khuyến nghị | Tên dùng trong phản hồi. Rỗng → không nhắc tên tiệm. |
| `city` | TEXT | Khuyến nghị | Thành phố của tiệm; phải khớp `business_locations.city`. Rỗng → không nhắc địa danh. |
| `services_json` | JSON array | Khuyến nghị | Dịch vụ **đã xác minh**. Chỉ được nhắc dịch vụ có trong list. Rỗng → không nhắc dịch vụ. |
| `language` | TEXT (`en`/`vi`) | Có | Ngôn ngữ ưu tiên của tiệm (mặc định). Vẫn ưu tiên ngôn ngữ của review khi lệch. |
| `tone` | TEXT | Có | Brand voice, ví dụ `friendly`, `warm-professional`, `concise`. |
| `seo_keywords_json` | JSON array | Tuỳ chọn | Keyword được duyệt riêng cho tiệm; tập con của §2.1. |
| `rules_json` | JSON object | Tuỳ chọn | Luật riêng: ví dụ `{"do_not_mention": ["Kim"], "escalation_email": "..."}`. Luật riêng **thắng** luật chung. |
| `auto_publish_allowed` | INT (0/1) | Có | Mặc định 0. Nếu 1: chỉ áp dụng cho 5 sao không nhạy cảm; **không** áp dụng cho 1–4 sao, review nhạy cảm, hay review chỉ có số sao ≤3. |

Ràng buộc liên quan:
- Ngoài ra hệ thống cần `business/location id` để tra cứu: `business_locations.location_name`
  (ví dụ `accounts/1001/locations/loc-001`) + `account_id` + `title`, `city`, `state` (địa chỉ/thành phố/tiểu bang).

Nguyên tắc cách ly dữ liệu (BẮT BUỘC):
- **KHÔNG dùng thông tin của tiệm A cho review của tiệm B** — kể cả cùng chuỗi, cùng thành phố,
  cùng chủ sở hữu. Mọi tra cứu phải khoá theo `location_id` của review đang xử lý.
- Tên nhân viên chỉ được nhắc nếu: xuất hiện trong review VÀ không nằm trong `rules_json.do_not_mention`.
- **Thiếu thông tin → phản hồi trung tính**: không tên tiệm, không địa danh, không dịch vụ cụ thể,
  không tên nhân viên; cảm ơn ngắn + mời liên hệ trực tiếp. Không suy diễn để lấp chỗ trống.

## 7. Quy tắc an toàn

- **Không tuyên bố** phản hồi sẽ/có thể **tăng thứ hạng trên Google Maps** hay cải thiện xếp hạng tìm kiếm.
- **Không** đưa thông tin cá nhân (PII): số điện thoại, email cá nhân, địa chỉ nhà, thông tin y tế, dữ liệu
  khách hàng khác. Chỉ nêu kênh liên hệ **chính thức của tiệm** (điện thoại/website công khai) khi cần.
- Không xác nhận/tiết lộ chi tiết đặt hẹn, giá, hay hồ sơ của khách khác.
- Không chỉ trích, không gán nhãn khách (không gọi "khách hàng giả", "đối thủ", "spammer") trong phản hồi công khai.
- Không cam kết kết quả y tế/mỹ phẩm ("trị", "chữa", "đảm bảo không nhiễm trùng").
- Case nhạy cảm: không phản hồi tự động; chuyển người duyệt; cân nhắc **không phản hồi** và theo dõi.
- Mọi tuyên bố phải truy được về review hoặc Brand Profile. Không có nguồn → bỏ tuyên bố.
