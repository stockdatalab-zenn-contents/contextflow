"""pipeline/p10_sessionizer.py

RawEvent（5秒間隔などの前面ウィンドウ生ログ）を Session（連続区間）へ圧縮する。
非公開の検討資料にある

    11:00-11:18 VS Code work-context 18分
    11:18-11:24 Chrome GitHub 6分

のような後処理圧縮に対応する。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Sequence

from contextflow.config import AppConfig
from contextflow.contracts.models import RawEvent, Session
from contextflow.storage.db import Database
from contextflow.storage.repositories import RawEventRepository, SessionRepository
from contextflow.timeutil import day_range, ensure_aware


def build_sessions(events: Sequence[RawEvent], config: AppConfig) -> list[Session]:
    """RawEvent 列を Session 列へ圧縮する（純粋関数。DB に触らない）。

    連結条件:
      - 同一 process（`merge_by_process_only` が false ならタイトルも同一）
      - 直前イベントとの間隔が `sessionizer.merge_gap_sec` 以内
    `collector.idle_threshold_sec` を超える idle_sec を持つイベントが来たら
    そこで session を切る。そのイベント自身は idle 区間なので session に含めない
    （PC 離席を勝手に作業時間へ含めないため）。
    """
    if not events:
        return []

    merge_gap_sec = int(config.get("sessionizer.merge_gap_sec", 60))
    min_duration_sec = int(config.get("sessionizer.min_duration_sec", 30))
    merge_by_process_only = bool(config.get("sessionizer.merge_by_process_only", False))
    idle_threshold_sec = int(config.get("collector.idle_threshold_sec", 180))
    interval_sec = int(config.get("collector.interval_sec", 5))

    # 時刻順にソートしてから連結する
    sorted_events = sorted(events, key=lambda e: ensure_aware(e.ts))

    sessions: list[Session] = []
    current: list[RawEvent] = []

    def flush() -> None:
        """current に溜めたイベントを1つの Session にまとめて sessions へ追加する。"""
        if not current:
            return
        session = _events_to_session(current, interval_sec)
        # min_duration_sec 未満の session は捨てる
        if session.duration_sec >= min_duration_sec:
            sessions.append(session)

    for event in sorted_events:
        # idle しきい値を超えるイベントは PC 離席とみなし、そこで session を切る。
        # このイベント自体は idle 区間なので、どの session にも含めない。
        if event.idle_sec > idle_threshold_sec:
            flush()
            current = []
            continue

        if not current:
            current = [event]
            continue

        prev = current[-1]
        same_process = event.process == prev.process
        same_title = merge_by_process_only or (event.window_title == prev.window_title)
        gap_sec = (ensure_aware(event.ts) - ensure_aware(prev.ts)).total_seconds()

        if same_process and same_title and gap_sec <= merge_gap_sec:
            current.append(event)
        else:
            # 連結条件を満たさない → 直前までを session として確定し、新しい session を開始
            flush()
            current = [event]

    flush()
    return sessions


def _events_to_session(events: list[RawEvent], interval_sec: int) -> Session:
    """同一 session に属する RawEvent 列（時刻順）から Session を組み立てる。"""
    start_at = ensure_aware(events[0].ts)
    last_ts = ensure_aware(events[-1].ts)
    # 最後のイベントの1サンプルぶん（採取間隔）を含めて終了時刻とする
    end_at = last_ts + timedelta(seconds=interval_sec)
    duration_sec = int(round((end_at - start_at).total_seconds()))
    idle_sec = max(e.idle_sec for e in events)
    return Session(
        start_at=start_at,
        end_at=end_at,
        process=events[0].process,
        window_title=events[0].window_title,
        duration_sec=duration_sec,
        idle_sec=idle_sec,
        sample_count=len(events),
    )


def sessionize_day(db: Database, target: date, config: AppConfig) -> list[Session]:
    """target 日の RawEvent を読み、session化して保存する。保存した Session を返す。"""
    start, end = day_range(target)
    events = RawEventRepository(db).list_between(start, end)
    sessions = build_sessions(events, config)
    SessionRepository(db).replace_between(start, end, sessions)
    return sessions
