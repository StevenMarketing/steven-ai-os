# Steven AI OS — GBP Platform (gbp.adzdigital.com)

Tài liệu này ghi lại quyết định kiến trúc **2026-10-16 (GMT+7)**: domain chính thức của
nền tảng là **`gbp.adzdigital.com`** (thay cho `greply.adzdigital.com`). Đây là nền tảng
quản lý **Google Business Profile + marketing đa chức năng**; **GReply là một module bên
trong**, không phải toàn bộ hệ thống.

> Trạng thái: **kế hoạch + chuẩn bị**. Không có thay đổi DNS, không có reverse proxy, không
> có kết nối Google thật, không có production deploy ở bước này.

## 1. Mô hình

```
                       https://gbp.adzdigital.com
                                  │  (HTTPS, Let's Encrypt)
                       ┌──────────▼──────────┐
                       │  Reverse proxy      │  Caddy/Nginx trên VPS
                       │  + phiên đăng nhập  │  (auth của app, không public trần)
                       └──────────┬──────────┘
                                  │  proxy tới loopback
                       ┌──────────▼──────────┐
                       │  App server         │  127.0.0.1:8000 (chỉ loopback)
                       │  src/gbp/api/…      │  web/ (UI)
                       └──────────┬──────────┘
                                  │
        ┌─────────────┬───────────┼───────────┬──────────────┐
        │             │           │           │              │
   ┌────▼────┐  ┌─────▼─────┐ ┌───▼────┐ ┌────▼─────┐  ┌─────▼──────┐
   │ GReply  │  │ Locations │ │Reviews │ │Local SEO │  │Analytics…  │
   │ (đã có) │  │ (kế hoạch)│ │(kế hoạch)│ │(kế hoạch)│  │(kế hoạch)  │
   └─────────┘  └───────────┘ └────────┘ └──────────┘  └────────────┘
                       Content Studio (kế hoạch)
```

**Quy ước đặt chỗ module:** mỗi module có tiền tố route riêng dưới cùng host để không phải
đổi domain khi mở rộng:

| Module | Route đề xuất | Trạng thái |
|---|---|---|
| **GReply** (soạn/duyệt trả lời review, DRAFT MODE) | `/greply/…` + API `/api/…` hiện có | **đã có** (v0.22.0 UI, `web/`, DRAFT MODE) |
| Locations | `/locations/…` | kế hoạch — chưa xây |
| Reviews | `/reviews/…` | kế hoạch — chưa xây |
| Local SEO | `/seo/…` | kế hoạch — chưa xây |
| Content Studio | `/content/…` | kế hoạch — chưa xây |
| Analytics | `/analytics/…` | kế hoạch — chưa xây |

Không module nào trong nhóm "kế hoạch" được xây ở bước này; bảng chỉ để chốt chỗ đặt và
tránh phải đổi domain/route về sau.

## 2. Google OAuth — redirect URI dự kiến

```
redirect_uri = https://gbp.adzdigital.com/api/google/oauth/callback
```

* Scope: `https://www.googleapis.com/auth/business.manage` — **RESTRICTED** (cần Google
  phê duyệt/allowlist; xem `docs/GOOGLE-READINESS.md`).
* Bắt buộc: `state` (CSRF, dùng một lần) + PKCE `S256`, cổng live opt-in
  `GBP_ALLOW_LIVE_GOOGLE=1` (mặc định **fail-closed**).
* Token lưu cục bộ, mode `0600`, không bao giờ ghi log/giá trị (xem
  `src/gbp/auth/token_manager.py` và `scripts/google_flow.py`).
* Đăng ký thêm `http://127.0.0.1:8000/api/google/oauth/callback` (chỉ để thử cục bộ) nếu
  cần; **không** dùng tunnel tạm cho OAuth thật.

## 3. Kế hoạch triển khai (chưa thực hiện bước nào)

1. **DNS (owner):** bản ghi `A` cho `gbp.adzdigital.com` → IP của VPS.
2. **TLS + proxy (owner phê duyệt):** Caddy/Nginx + Let's Encrypt, chỉ mở 80/443; app vẫn
   bind `127.0.0.1:8000`, **không** mở cổng app ra ngoài.
3. **Auth:** giữ nguyên tường đăng nhập của app (`--auth`, session + CSRF); mọi route
   `/api/...` đều yêu cầu phiên hợp lệ (đã kiểm chứng: 401 khi chưa đăng nhập).
4. **Google:** bật `GBP_ALLOW_LIVE_GOOGLE=1` **chỉ** trong phiên kiểm thử có phê duyệt,
   sau khi scope `business.manage` được Google chấp thuận.
5. **Kiểm thử giới hạn:** liệt kê **tối đa 5 location** (business name, address, ZIP,
   location ID) → owner chọn **một** location → chỉ **đọc** reviews. Không reply, không
   edit, không publish, không sync hàng loạt 500 tiệm.
6. **Rollback:** tắt proxy/DNS là về trạng thái cũ; xoá token qua `disconnect()`; không có
   bước nào chạm dữ liệu khách hàng trên Google.

## 4. Ranh giới an toàn đang giữ

* Không kết nối/thao tác Google Business Profile thật, không đăng/chỉnh sửa review.
* Không thay đổi DNS / reverse proxy / firewall / production ở bước này.
* Không tạo hoặc thay đổi API key; không in token/client secret/refresh token.
* GitHub worker cron: **PAUSED**; lịch kiểm tra OpenRouter billing: **giữ nguyên**.
