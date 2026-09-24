"""前面ウィンドウを一定間隔で採取し、RawEvent として SQLite へ書き込む。

標準ライブラリのみ使用。win32.py から生情報を取得し、privacy 設定でマスクした上で
RawEventRepository を通して保存する。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Optional

from contextflow import timeutil
from contextflow.collector.win32 import get_foreground_info, get_host, get_idle_sec
from contextflow.config import AppConfig
from contextflow.contracts.models import RawEvent
from contextflow.storage.db import Database
from contextflow.storage.repositories import MetaRepository, RawEventRepository

# 収集が動いているかを示す心拍。まとめ書きを待たずに状態が分かるようにする
HEARTBEAT_KEY = "collector_heartbeat"

# 正規表現のコンパイル結果をキャッシュ（pattern 文字列 -> コンパイル済み）
_mask_pattern_cache: dict[str, "re.Pattern[str]"] = {}


def _compiled_pattern(pattern: str) -> "re.Pattern[str]":
    compiled = _mask_pattern_cache.get(pattern)
    if compiled is None:
        compiled = re.compile(pattern)
        _mask_pattern_cache[pattern] = compiled
    return compiled


def mask_title(title: str, config: AppConfig) -> str:
    """privacy 設定に従ってウィンドウタイトルをマスクする。

    drop_window_title が true なら空文字を返す。
    そうでなければ mask_patterns（正規表現 -> 置換文字列）を順に適用する。
    """
    if config.get("privacy.drop_window_title", False):
        return ""

    masked = title
    for rule in config.get("privacy.mask_patterns", []) or []:
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "")
        if not pattern:
            continue
        masked = _compiled_pattern(pattern).sub(replacement, masked)
    return masked


class Collector:
    """前面ウィンドウのサンプリングと SQLite への保存を担う。"""

    def __init__(self, config: AppConfig, db: Database) -> None:
        self._config = config
        self._db = db
        self._repo = RawEventRepository(db)

    def sample(self) -> RawEvent:
        """1回ぶん採取する（マスク適用済み。保存はしない）。"""
        info = get_foreground_info()
        idle_sec = get_idle_sec()
        title = mask_title(info.window_title, self._config)
        return RawEvent(
            ts=timeutil.now(),
            process=info.process,
            window_title=title,
            idle_sec=idle_sec,
            host=get_host(),
        )

    def run(
        self,
        duration_sec: Optional[int] = None,
        stop_event: Optional["threading.Event"] = None,
    ) -> int:
        """interval_sec 間隔でサンプリングし、flush_every 件ごとにまとめて書き込む。

        duration_sec 経過で終了。KeyboardInterrupt を受けても残りを必ず flush してから
        書き込み件数を返す。処理時間ぶんドリフトしないよう、実際の経過時間を見て
        次回サンプリングまでの待ち時間を都度調整する。

        stop_event を渡すと、それが立った時点で待機を打ち切って終了する
        （GUI から停止するため。待機中でも即座に止まる）。
        """
        interval = float(self._config.get("collector.interval_sec", 5))
        flush_every = int(self._config.get("collector.flush_every", 12))
        # 件数がたまらなくても、この秒数を超えたら書き出す。
        # 画面を再読み込みしたときに、収集中でも直近の生ログを参照できるようにするため。
        flush_max_wait = float(self._config.get("collector.flush_max_wait_sec", 10))

        buffer: list[RawEvent] = []
        total = 0
        start = time.monotonic()
        next_at = start
        last_flush = start
        meta = MetaRepository(self._db)

        try:
            while duration_sec is None or (time.monotonic() - start) < duration_sec:
                if stop_event is not None and stop_event.is_set():
                    break
                buffer.append(self.sample())
                # 生ログは flush_every 件ごとのまとめ書きなので、最後の1件だけでは
                # 「動いているか」が最大 flush_every × interval 秒ぶん古く見える。
                # 画面の状態表示用に、毎回 心拍だけ更新する（1行の更新なので軽い）。
                meta.set(HEARTBEAT_KEY, timeutil.now().isoformat())
                # 件数がたまるか、一定時間が過ぎたら書き出す
                waited = time.monotonic() - last_flush
                if buffer and (len(buffer) >= flush_every or waited >= flush_max_wait):
                    total += self._repo.add_many(buffer)
                    buffer.clear()
                    last_flush = time.monotonic()

                # 固定スケジュール（start からの積算）で次回時刻を決めるため、
                # サンプリング・書き込みにかかった時間ぶんはドリフトしない。
                next_at += interval
                wait = next_at - time.monotonic()
                if duration_sec is not None:
                    remaining = (start + duration_sec) - time.monotonic()
                    wait = min(wait, remaining)
                if wait > 0:
                    if stop_event is not None:
                        # 待機中でも停止できるよう sleep ではなく wait を使う
                        if stop_event.wait(wait):
                            break
                    else:
                        time.sleep(wait)
        except KeyboardInterrupt:
            pass
        finally:
            if buffer:
                total += self._repo.add_many(buffer)
            # 終了したら心拍を消す。停止が即座に画面へ反映される
            meta.delete(HEARTBEAT_KEY)

        return total
