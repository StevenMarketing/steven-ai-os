"""In ra tên branch chuẩn cho một issue (thay cho `python3 -c "..."` trong runbook).

Vì sao có file này: runbook của worker tính tên branch bằng `python3 -c "import ..."`.
Lệnh `-c` nằm trong mẫu allowlist khá rộng (`script execution via -e/-c flag`), mà
mẫu đó cho phép chạy **mọi** đoạn code inline. Dùng script đã kiểm vào repo thì
runbook không cần mẫu đó nữa, và phần tính tên branch trở thành code có test.

Dùng::

    python3 scripts/branch_for.py 42 "docs: add a Quickstart section to README"
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import issue_worker_lib as lib  # noqa: E402


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Tên branch cho một issue.")
    parser.add_argument("issue")
    parser.add_argument("title", nargs="?", default="")
    args = parser.parse_args(argv)
    print(lib.branch_for(args.issue, args.title))
    return 0


if __name__ == "__main__":
    sys.exit(main())
