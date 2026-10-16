# UNATTENDED MODE — Hermes tự chạy khi chủ sở hữu ngủ

Tài liệu này ghi lại **cơ chế thật** của Hermes về phê duyệt lệnh, **quyết định**
đã chọn cho Steven AI OS, bằng chứng, và những gì **vẫn luôn cần người**.

## 1. Cơ chế Command Approval của Hermes (thực tế, không suy đoán)

| Sự thật | Chi tiết |
|---|---|
| Chỉ **shell command** mới qua approval | Ghi/sửa file (`write_file`, `patch`) **không bao giờ** hỏi ⇒ worker đã có thể sửa code khi không có người |
| Chế độ | `approvals.mode = smart` (đang dùng). An LLM phụ đánh giá mức rủi ro; chế độ `off` là YOLO (bỏ hết bảo vệ) — **không dùng** |
| Ghi nhớ quyết định | Khi chủ sở hữu bấm "always allow", **mẫu lý do** được thêm vào `command_allowlist` trong `config.yaml` |
| Cách sửa chính thức | `hermes config set command_allowlist '[...]'` (không sửa tay YAML) |
| Hạn chờ | `approvals.timeout = 600s` |
| Ghi memory/skill | `memory.write_approval: true`, `skills.write_approval: true` ⇒ vẫn cần người |

Allowlist hiện tại (3 mục) — đúng những mẫu worker cần:

```
- command parser limit or malformed executable payload
- execute_code
- script execution via -e/-c flag        ← mọi `python3 -c "..."` của worker
```

## 2. Quyết định: **giữ nguyên** `approvals.mode: smart`, không nới allowlist

Yêu cầu của chủ sở hữu là *"ưu tiên allowlist giới hạn theo lệnh, đường dẫn và phạm
vi thay vì Always Allow toàn bộ"* và *"không tự ý vô hiệu hoá Command Approval"*.

**Bằng chứng cho thấy chưa cần nới gì:** lần chạy thật của worker cho Issue #2 là một
phiên do **cron** sinh ra (không có người ngồi trả lời prompt). Runbook dùng
`git` + `python3 -m unittest` + `python3 -c` + `python3 scripts/...` — **không lệnh
nào bị hỏi**; phiên chạy xong trong 93 giây (claim 05:13:50 → finish 05:15:23), tạo
branch, commit, draft PR và comment kết quả.

⇒ **Unattended mode đã hoạt động trong đúng phạm vi worker, không cần thay đổi cấu
hình bảo mật nào.** Giữ nguyên `smart` + 3 mục allowlist hiện có.

## 3. Được tự chạy khi không có người

- `git status/log/diff/show/branch/rev-parse/ls-remote`, `git add/commit`, `git push` **branch riêng**
- `python3 -m unittest`, `python3 -c`, `python3 scripts/*.py` (không phải script phá hoại)
- `gh`/API GitHub chỉ đọc + tạo draft PR + comment (qua PAT phạm vi hẹp)
- `write_file` / `patch` trên file **không** nằm trong danh sách protected
- `python3 scripts/notify.py` (thông báo Telegram), `python3 scripts/budget.py can-spend`

## 4. LUÔN cần người (bị chặn hoặc phải là chủ sở hữu)

| Việc | Vì sao |
|---|---|
| Merge PR, push `main`, `--force` | pre-push hook chặn; chủ sở hữu là người merge |
| Deploy / đổi VPS / Hermes / model / provider | ngoài phạm vi |
| `rm -rf`, xoá dữ liệu, `git reset --hard` | phá huỷ |
| Đọc/sửa `.env`, credentials, `.ssh/`, `config.yaml` | bí mật & bảo mật |
| Gắn nhãn `hermes-ready` | cổng người: chỉ tài khoản chủ repo, kiểm chứng bằng replay timeline |
| Việc Google Business Profile thật (đăng/trả lời review) | cấm tuyệt đối |
| Sửa file protected (`scripts/issue_worker_lib.py`, hooks, workflows, `docs/GITHUB-WORKER.md`, …) | diff gate từ chối, cần `HERMES_ALLOW_PROTECTED=1` của chủ sở hữu |

Khi một lệnh **bị approval từ chối** trong phiên không người: worker phải **dừng
ngay**, gắn `hermes-blocked`, ghi lý do vào issue và gửi Telegram
(`--event task_blocked`). Không thử cú pháp khác để lách.

## 5. Rollback (kiểm tra trước khi đổi bất cứ thứ gì về bảo mật)

```bash
hermes config get command_allowlist                       # xem trạng thái
cp -a /opt/data/config.yaml /opt/data/config.yaml.bak-$(date -u +%Y%m%dT%H%M%SZ)
hermes config set command_allowlist '["<mục cũ 1>","<mục cũ 2>"]'   # khôi phục
hermes config get approvals.mode                          # phải vẫn là "smart"
```

Module này **không** đổi `approvals.mode`, không tạo khoá, không mở cổng, không
cài dịch vụ hệ thống.

## 6. Nếu sau này cần nới rộng hơn (kiến trúc thay thế)

Nếu một loại lệnh mới thật sự cần cho unattended mà Hermes không có allowlist đủ hẹp
để cấp an toàn, **không** hạ `approvals.mode`; thay vào đó tách một **worker hạn chế
quyền**:

1. Container/người dùng riêng, chỉ thấy thư mục làm việc (không thấy `.env`, `.ssh`).
2. Chỉ có network ra `api.github.com` (allowlist egress) + không có credential Google.
3. Token GitHub riêng, chỉ repo này, quyền Contents/Issues/PR.
4. Mọi thứ khác vẫn đi qua Hermes với `approvals.mode: smart`.

Cách này giữ nguyên "không vô hiệu hoá kiểm soát bảo mật" mà vẫn chạy 24/7.

## 7. Review phạm vi `command_allowlist`

| Mục | Cho phép gì | Worker có cần? | Rủi ro | Đề xuất |
|---|---|---|---|---|
| `command parser limit or malformed executable payload` | lệnh dài/bị parser cắt | không trực tiếp | thấp (hệ quả cú pháp, không phải quyền) | giữ |
| `execute_code` | công cụ `execute_code` của Hermes — **chạy Python tuỳ ý** | **không** (runbook chỉ dùng terminal + ghi file) | **cao** | có thể bỏ để siết; đổi lại mọi phiên sẽ bị hỏi lại khi dùng công cụ này |
| `script execution via -e/-c flag` | `python3 -c`, `sh -c`… (**code tuỳ ý inline**) | **trước đây có** — bước 3 tính tên branch | **cao** | **đã loại nhu cầu**: bước 3 nay dùng `python3 scripts/branch_for.py N "<title>"` (script đã kiểm vào repo, có test) ⇒ có thể bỏ mục này |
| `script execution via heredoc` | `python3 - <<'PY' … PY` (**code tuỳ ý qua heredoc**) | **không** — runbook không dùng heredoc | **cao** | có thể bỏ. Mục này **tự xuất hiện trong phiên này** khi chủ sở hữu bấm "allow" cho vài lệnh heredoc tôi chạy để kiểm chứng; đó là cách allowlist vốn hoạt động (mỗi lần duyệt sẽ ghi lại mẫu lý do) |

**Lượt này KHÔNG đổi gì** (đúng yêu cầu "không nới quyền hoặc tắt smart approval"):
không hạ `approvals.mode`, không thêm, không xoá mục nào.

**Đề xuất (chờ chủ sở hữu duyệt — đây là thay đổi cấu hình bảo mật):**

```bash
cp -a /opt/data/config.yaml /opt/data/config.yaml.bak-$(date -u +%Y%m%dT%H%M%SZ)
hermes config set command_allowlist '["command parser limit or malformed executable payload"]'
hermes config get command_allowlist      # xác minh
```

Hệ quả khi siết: mọi lần dùng `execute_code` hoặc `python3 -c` trong **bất kỳ** phiên
nào sẽ hỏi lại. Worker **không** bị ảnh hưởng vì runbook không còn cần `-c`.

## 8. Cổng hợp nhất `scripts/worker_gate.py`

`StateStore.can_start()` chỉ đếm sổ cái **riêng của worker**; nó không thấy chi tiêu của
các phiên Hermes khác trên cùng khoá, và trong code **không có trần retry**
(`MAX_ATTEMPTS` không tồn tại ở đâu trước lượt này). `budget.py` có cả hai nhưng
**không được worker gọi**. `worker_gate.py` nối hai lớp, worker chỉ cần gọi một lệnh
mỗi bước:

| Lệnh | Việc |
|---|---|
| `worker_gate.py can-start --issue N --estimate 0.05` | lớp ngân sách (`max(provider usage_daily, sổ cái)`, hạn mức khoá, **fail-closed**, trần retry) **và** lớp issue (idempotency, claim sống, kill switch). Chỉ cho qua khi **cả hai** đồng ý; từ chối thì in rõ lớp nào chặn |
| `worker_gate.py claim --issue N --estimate 0.05` | **giữ chỗ ngân sách trước** (nguyên tử), rồi claim issue; claim hỏng ⇒ **nhả chỗ ngay** |
| `worker_gate.py finish --issue N --outcome done\|failed\|blocked\|needs-info --cost C [--pr URL] [--tests "..."]` | nhả chỗ, ghi **chi phí thật** (tính vào trần retry), đóng issue trong sổ cái, gửi Telegram theo kết cục |
| `worker_gate.py attempts --issue N` | số lần đã thử / trần retry |
| `worker_gate.py report` | số của cả hai lớp |

`worker_gate.py` là **file mới** (không nằm trong protected path), nên nó được review
như code thường; `scripts/issue_*.py` vẫn protected nên worker không thể tự sửa cổng.

