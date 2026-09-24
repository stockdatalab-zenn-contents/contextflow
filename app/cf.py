"""contextflow の入口スクリプト。

使い方: プロジェクトルートで `python app/cf.py <command>`
source/ をパスへ追加してから CLI を呼ぶだけの薄いラッパ。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "source"))

from contextflow.cli import main  # noqa: E402  パス追加後に import する

if __name__ == "__main__":
    raise SystemExit(main())
