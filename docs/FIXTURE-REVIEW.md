# FIXTURE-REVIEW — Rà soát fixtures vs. đặc tả DOMAIN-RULES

> Phạm vi: `fixtures/reviews_page1.json`, `fixtures/reviews_page2.json` (không sửa fixture,
> chỉ báo cáo). Đối chiếu với `docs/DOMAIN-RULES.md`. Các file liên quan đã đọc:
> `fixtures/accounts.json`, `fixtures/locations.json`, `src/gbp/client/mock_client.py`,
> `src/gbp/fixtures.py`, `src/gbp/repo/sqlite_repo.py`.

## 1. Case đã có trong fixtures

`reviews_page1.json` — location `accounts/1001/locations/loc-001`, `totalSize = 6`,
`averageRating = 4.3`, có `nextPageToken = "reviews-page-2"`.

| # | review | Sao | Nội dung | Case được bao phủ |
|---|---|---|---|---|
| 1 | `r-1001` | FIVE | EN, có nhắc nhân viên "Kim" + dịch vụ "gel manicure" | 5 sao có comment; có tên nhân viên; có dịch vụ xác minh được |
| 2 | `r-1002` | FIVE | `comment = null` | Review **chỉ có số sao** (mức 5) |
| 3 | `r-1003` | FOUR | EN, "great job ... though I waited about 20 minutes" | 4 sao có comment lẫn khen + than phiền nhẹ |

`reviews_page2.json` — cùng location, `totalSize = 6`, `averageRating = 4.3`, **không** có `nextPageToken` (trang cuối).

| # | review | Sao | Nội dung | Case được bao phủ |
|---|---|---|---|---|
| 4 | `r-1004` | ONE | EN, phàn nàn nặng ("chipped my nails same day", "staff was rude") | Review 1 sao tiêu cực (biên dưới của nhóm 1–2 sao) |
| 5 | `r-1005` | FIVE | **Tiếng Việt**, "nhân viên thân thiện ... Sẽ quay lại" | 5 sao ngôn ngữ không phải EN |
| 6 | `r-1006` | FIVE | EN, nhắc nhân viên "Anna" | 5 sao có tên nhân viên (biến thể thứ hai) |
| 7 | `r-1001` (lặp) | FIVE | Comment đã sửa `"(updated)"`, `updateTime` mới hơn | **Chống trùng xuyên trang** + review bị chỉnh sửa |

Tóm tắt độ phủ: phân trang 2 trang, trang cuối không có token; 5 sao có/không comment; 4 sao; 1 sao;
tên nhân viên (2 biến thể); dịch vụ (gel manicure / pedicure); review EN + VI; review lặp giữa 2 trang.
2 file error (`error_401.json`, `error_429.json`) đã tồn tại — ngoài phạm vi đặc tả này.

## 2. Gap list — case của đặc tả CHƯA được fixtures bao phủ

### 2.1 Khoảng trống theo số sao (§1)
- **G1 — 3 sao**: không có case nào `rating == 3`.
- **G2 — 2 sao**: nhóm 1–2 sao chỉ có biên 1 sao; thiếu biên 2 sao.
- **G3 — Chỉ có số sao ở mức thấp**: `r-1002` là FIVE + `comment = null`. Thiếu case
  rating-only cho 1/2/3/4 sao (bắt buộc phê duyệt thủ công, không được bịa chi tiết).

### 2.2 Khoảng trống nhạy cảm (§1.1) — hoàn toàn chưa bao phủ
- **G4 — Sức khoẻ/an toàn**: comment cáo buộc nhiễm trùng / chảy máu / dụng cụ bẩn.
- **G5 — Phân biệt đối xử**: comment mang tính chủng tộc/giới tính/ngôn ngữ.
- **G6 — Đe doạ/xúc phạm/thù địch**: comment chửi thề, doạ làm hại.
- **G7 — Pháp lý/tài chính**: doạ kiện, khiếu nại cơ quan quản lý, đòi hoàn tiền, doạ đăng báo.
- **G8 — PII trong review**: comment chứa số điện thoại/địa chỉ/thông tin y tế.
- **G9 — Spam/quảng cáo/đối thủ**: comment chèn link, quảng cáo tiệm khác, không liên quan tiệm.

### 2.3 Khoảng trống Local SEO & cách ly dữ liệu tiệm (§2, §6)
- **G10 — Đa tiệm**: mọi review đều thuộc `loc-001`. `loc-002..loc-006` (San Jose, Houston, Dallas,
  Westminster, Atlanta) **không có review nào** → không test được luật "KHÔNG dùng thông tin tiệm A
  cho tiệm B".
- **G11 — Lệch ngôn ngữ location vs review**: `loc-003`/`loc-005` có `language = "vi"` nhưng không có
  review; ngược lại `r-1005` (VI) nằm ở `loc-001` (EN) — chỉ bao phủ 1 chiều.
- **G12 — Bịa từ khoá**: không có case để test FAIL của Local SEO (nhồi keyword, tên tiệm/thành phố lặp).
- **G13 — Brand Profile**: không có fixture Brand Profile (`business_name`, `services_json`, `tone`,
  `seo_keywords_json`, `rules_json`, `auto_publish_allowed`) → không test được §6 và luật `rules_json`.
- **G14 — Do-not-mention**: tên nhân viên trong fixture ("Kim", "Anna") không gắn với `rules_json`
  cấm nhắc → không test được nhánh chặn tên nhân viên.

### 2.4 Khoảng trống trạng thái/đường dữ liệu (§5, §3)
- **G15 — Đã có phản hồi**: payload review không có trường phản hồi (không có `reviewReply`/`reply_text`)
  → không test được nhánh "không ghi đè phản hồi đã có" (`COALESCE` trong `upsert_review`) và
  ngưỡng chống trùng 90 ngày ở §3.
- **G16 — Đổi số sao**: `r-1001` chỉ sửa **comment** (giữ FIVE); thiếu case khách **hạ sao**
  (ví dụ FIVE → TWO) để test chuyển `needs_review → draft` khi `update_time` đổi.
- **G17 — Trang rỗng / 0 review**: không có fixture `reviews: []` (kể cả trên `loc-002`) để test biên.
- **G18 — Trùng trong cùng một trang**: dedup hiện chỉ test xuyên trang.

### 2.5 Bất nhất dữ liệu fixture (báo cáo, không sửa)
- **D1**: `reviews_page2.json` khai `totalSize = 6` nhưng chỉ chứa 4 phần tử, trong đó 1 phần tử là
  bản lặp của `r-1001` → 6 review duy nhất khớp `totalSize`, nhưng *số phần tử* (3 + 4 = 7) lớn hơn.
- **D2**: `MockGBPClient.list_reviews` giữ **lần xuất hiện đầu tiên** theo `name`
  (`seen.setdefault`) → bản cập nhật `r-1001 "(updated)"` ở trang 2 **bị bỏ**. Luồng thật cần áp
  bản mới hơn theo `updateTime`; hiện fixture chưa kiểm chứng được điều này.
- **D3**: `averageRating = 4.3` trong cả hai trang; không có fixture cho `totalSize`/`averageRating`
  thay đổi sau khi review bị sửa (watermark `updateTime`).

## 3. Đề xuất bổ sung fixture (không thực hiện trong task này)

1. Thêm review vào **`reviews_page1.json`** (hoặc trang mới) cho `loc-001`:
   3 sao, 2 sao, và rating-only 2 sao (`comment = null`) → đóng G1, G2, G3.
2. Thêm 5 case nhạy cảm (mỗi loại 1 review, có thể ở `loc-005` để kiểm tra cả ngôn ngữ VI):
   sức khoẻ, phân biệt đối xử, đe doạ/chửi thề, pháp lý/hoàn tiền, PII → đóng G4–G8.
3. Thêm 1 review spam/đối thủ có link quảng cáo → đóng G9.
4. Thêm **trang review cho `loc-002`** (tiệm khác, thành phố khác) với 1–2 review để test cách ly
   dữ liệu tiệm A/B; thêm 1 review VI ở `loc-005` (vi) → đóng G10, G11.
5. Tạo `fixtures/brand_profiles.json` (2 tiệm: `loc-001` EN auto_publish=0, `loc-005` VI
   auto_publish=1 có `rules_json.do_not_mention`) → đóng G12, G13, G14.
6. Thêm trường phản hồi (`reviewReply: {"comment", "updateTime"}`) cho 1 review cũ → đóng G15.
7. Thêm case "hạ sao" (cùng `name`, `starRating` thấp hơn, `updateTime` mới) → đóng G16.
8. Thêm fixture `reviews_empty.json` (`reviews: []`, không `nextPageToken`) → đóng G17.
9. Thêm 1 trang có review lặp **trong cùng trang** → đóng G18.

Ghi chú kỹ thuật khi bổ sung: giữ field name đúng như mock client đang đọc
(`name`, `reviewId`, `starRating`, `comment`, `createTime`, `updateTime`, `reviewer.displayName`);
token trang 1 phải là `reviews-page-2` để `MockGBPClient._page_by_token` phân giải được
(`fixtures.list_reviews_pages` chỉ nạp 2 trang cố định).
