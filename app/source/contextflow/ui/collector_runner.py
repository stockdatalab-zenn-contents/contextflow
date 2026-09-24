"""ui/collector_runner.py

ブラウザの「開始」「停止」ボタンから、UI プロセス内のバックグラウンドスレッドで
`python app/cf.py collect` と同等の収集を行う層。

別プロセスは起こさない（プロセス管理・孤児プロセスを避けるため）。
そのぶん UI を終了すると収集も止まる（仕様。`server.py` の終了処理で `stop()` を呼ぶ）。

モジュールレベルで1つだけのスレッドを保持し、同時に2つ走らせない。
操作（開始・停止）は `threading.Lock` で直列化する。
収集スレッドの SQLite 接続は、接続がスレッド固有であるため、必ずスレッドの中で開く。

標準ライブラリのみ使用。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional

from contextflow import timeutil
from contextflow.collector.collector import HEARTBEAT_KEY, Collector
from contextflow.config import AppConfig
from contextflow.storage.db import open_database
from contextflow.storage.repositories import MetaRepository


@dataclass
class RunnerState:
    """収集スレッドの現在状態（画面表示用）。"""

    running: bool
    started_at: datetime | None
    collected: int  # 直近の実行で収集した件数
    message: str  # 人間向けの日本語1行


# --- モジュールレベルの状態（1つだけ）。操作は _lock で直列化する ---
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_state = RunnerState(running=False, started_at=None, collected=0, message="収集していない")


def _copy_state() -> RunnerState:
    """呼び出し側が書き換えても内部状態に影響しないよう複製を返す。"""
    return replace(_state)


def _within(iso_text: str, limit_sec: float) -> bool:
    """ISO8601 の時刻が、今から limit_sec 以内かどうか（api.py の同名関数と同じ考え方）。"""
    try:
        moment = timeutil.ensure_aware(datetime.fromisoformat(iso_text))
    except (TypeError, ValueError):
        return False
    elapsed = (timeutil.now() - moment).total_seconds()
    return 0 <= elapsed <= limit_sec


def _check_other_process(config: AppConfig) -> None:
    """別プロセス（CLI の collect や別の UI）が収集中なら例外を投げる。

    判断は心拍（HEARTBEAT_KEY）が collector.interval_sec × 3 秒以内に
    更新されているかどうか。二重起動で生ログが重複するのを防ぐのが目的。

    この確認のためだけに短命の DB 接続を開いて閉じる
    （収集スレッド用の接続とは別。呼び出し元スレッドの中で完結するので問題ない）。
    """
    db = open_database(config)
    try:
        heartbeat = MetaRepository(db).get(HEARTBEAT_KEY)
    finally:
        db.close()

    interval = float(config.get("collector.interval_sec", 5) or 5)
    if heartbeat and _within(heartbeat, interval * 3):
        raise ValueError("別のウィンドウで収集中。そちらを止めてから開始する")


def _run_collector(config: AppConfig, stop_event: threading.Event) -> None:
    """スレッド本体。

    Database はここで新規に開く（sqlite3 接続はスレッド固有で、作成した
    スレッドでしか使えないため）。例外はここで捕まえ、状態へ残してスレッドを
    落とさない。終了時は必ず db.close() する。
    """
    global _state

    collected = 0
    message = "収集を停止した"
    db = None
    try:
        db = open_database(config)
        collected = Collector(config, db).run(stop_event=stop_event)
    except Exception as exc:  # noqa: BLE001 - スレッドを落とさず状態へ残す
        message = f"収集中にエラーが発生した: {exc}"
    finally:
        if db is not None:
            db.close()
        with _lock:
            _state = replace(_state, running=False, collected=collected, message=message)


def start(config: AppConfig) -> RunnerState:
    """収集スレッドを起こす。

    既にこの UI が動かしていれば、そのまま running=True で「既に収集中」を返す。
    別プロセス（CLI の collect や別の UI）が収集中なら ValueError を投げる
    （呼び出し側の api.py で 409 にする）。
    """
    global _thread, _stop_event, _state

    with _lock:
        if _thread is not None and _thread.is_alive():
            _state = replace(_state, message="既に収集中")
            return _copy_state()

    # 心拍の確認は DB I/O を伴うため、ロックの外で行う
    _check_other_process(config)

    with _lock:
        # ロックを外していた間に別の呼び出しが開始していないか、念のため再確認する
        if _thread is not None and _thread.is_alive():
            _state = replace(_state, message="既に収集中")
            return _copy_state()

        stop_event = threading.Event()
        thread = threading.Thread(
            target=_run_collector,
            args=(config, stop_event),
            daemon=True,
            name="cf-collector",
        )
        _thread = thread
        _stop_event = stop_event
        _state = RunnerState(
            running=True, started_at=timeutil.now(), collected=0, message="収集を開始した"
        )
        thread.start()
        return _copy_state()


def stop(timeout_sec: float = 10.0) -> RunnerState:
    """収集スレッドを止める。動いていなければ何もしない。"""
    global _thread, _stop_event, _state

    with _lock:
        thread = _thread
        stop_event = _stop_event
        if thread is None or not thread.is_alive():
            _state = replace(_state, running=False, message="収集していない")
            return _copy_state()

    # スレッド側の終了処理（_run_collector の finally）も _lock を取るため、
    # ロックを持ったまま join するとデッドロックする。join はロックの外で行う。
    stop_event.set()
    thread.join(timeout_sec)

    with _lock:
        if thread.is_alive():
            _state = replace(
                _state,
                running=True,
                message=f"停止処理が {timeout_sec:.0f} 秒以内に終わらなかった（裏で継続中）",
            )
        else:
            _thread = None
            _stop_event = None
            # running / collected / message は _run_collector が既に更新済み
        return _copy_state()


def state() -> RunnerState:
    """現在の保持状態を返す（DB は見ない）。"""
    with _lock:
        return _copy_state()


def is_running() -> bool:
    """この UI が収集スレッドを動かしているか。"""
    with _lock:
        return _thread is not None and _thread.is_alive()
