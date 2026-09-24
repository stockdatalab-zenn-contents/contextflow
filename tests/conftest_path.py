"""tests/conftest_path.py

`app/source` を sys.path へ追加するだけの小さなヘルパ。
`python -m unittest discover -s tests -t .` はプロジェクトルートを
トップレベルとして実行されるため、`contextflow` パッケージが置かれている
`app/source` を明示的に sys.path へ加えないと import できない。
"""

from __future__ import annotations

import sys
from pathlib import Path

# このファイルは tests/conftest_path.py にある
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_DIR = _PROJECT_ROOT / "app" / "source"


def add_source_path() -> None:
    """app/source を sys.path の先頭へ追加する（複数回呼んでも安全）。"""
    source_str = str(_SOURCE_DIR)
    if source_str not in sys.path:
        sys.path.insert(0, source_str)
