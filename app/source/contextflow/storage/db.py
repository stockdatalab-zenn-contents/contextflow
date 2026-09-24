"""SQLite への接続とスキーマ初期化。

コネクションは1本を保持して使い回す（`connect()` は常に同じ接続を返す）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from contextflow.config import AppConfig

# schema.sql は同じディレクトリに置く
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


class Database:
    """SQLite コネクションを1本だけ保持するラッパ。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> sqlite3.Connection:
        """既存の接続を返す。無ければ新規作成して保持する。"""
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            self._conn = conn
        return self._conn

    def initialize(self) -> None:
        """schema.sql を適用する。CREATE TABLE IF NOT EXISTS のみなので冪等。"""
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        conn = self.connect()
        conn.executescript(sql)
        conn.commit()

    def close(self) -> None:
        """接続を閉じる。閉じた後に connect() すれば新しい接続が作られる。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Database:
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def open_database(config: AppConfig) -> Database:
    """config の paths.database を使い、initialize 済みの Database を返す。"""
    db = Database(config.path("database"))
    db.initialize()
    return db
