"""sources/manual.py

会議・思考・相談・紙作業・移動中など、PCログに残らない作業を
1行の操作（start/stop）または後からの一括追加（add）で記録する入力口。

実行中の作業は meta テーブル（MetaRepository, key=`manual_running`）へ
JSON で保持し、stop するまで activities テーブルへは書き込まない。
本人申告なので layer=REPORTED / source=MANUAL / confidence=1.0 で保存する。

日をまたぐ記録（stop / add）は、ローカル日付の 00:00 を境に複数の Activity へ
分割して保存する（calendar_ics.py の日またぎ分割と同じ考え方）。
`ActivityRepository.list_between` が `start_at` だけで絞り込むため、1件のまま
保存すると日をまたいだ分が開始日側にしか出てこず、終了日側の一覧・集計から
抜け落ちてしまうことの対策。標準ライブラリのみ使用。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from contextflow.contracts.models import Activity, ActivityLayer, ActivityType, Source
from contextflow.storage.db import Database
from contextflow.storage.repositories import ActivityRepository, MetaRepository
from contextflow.timeutil import day_range, ensure_aware, now

# meta テーブルに実行中の作業を保持する際のキー
_META_KEY = "manual_running"

# これ未満で終了した作業は押し間違いとみなし、記録せず取り消す。
# 数ミリ秒〜1秒の「開始してすぐ終了」で、長さ0分の活動が残るのを防ぐ。
MIN_DURATION_SEC = 1.0


def _split_range_by_day(start_at: datetime, end_at: datetime) -> list[tuple[datetime, datetime]]:
    """日をまたぐ区間を日境界（ローカル日付の 00:00）で分割する。

    calendar_ics.py の `_split_event_by_day` と同じ考え方。
    各区間が単一日の [00:00, 翌00:00) に収まるようにする。
    日をまたがなければ1件のタプルのみを返す。
    """
    if end_at <= start_at:
        # 不正・ゼロ長区間はそのまま1件として扱う（分割不要）
        return [(start_at, end_at)]

    pieces: list[tuple[datetime, datetime]] = []
    cur = start_at
    while cur < end_at:
        _, day_end = day_range(cur.date())
        piece_end = min(end_at, day_end)
        pieces.append((cur, piece_end))
        cur = piece_end
    return pieces


class ManualInput:
    """本人申告（手入力）による活動の開始・終了・追加を扱う。"""

    def __init__(self, db: Database) -> None:
        self._activities = ActivityRepository(db)
        self._meta = MetaRepository(db)

    def start(
        self,
        activity_type: ActivityType,
        *,
        project: Optional[str] = None,
        task: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> None:
        """作業を開始する。

        すでに実行中の作業があれば、その作業をこの開始時刻で自動的に stop
        してから開始する（前後の作業を隙間なく区切るため）。
        """
        started_at = ensure_aware(at) if at is not None else now()

        if self.running() is not None:
            self.stop(at=started_at)

        payload = {
            "activity_type": activity_type.value,
            "project": project,
            "task": task,
            "started_at": started_at.isoformat(),
        }
        self._meta.set(_META_KEY, json.dumps(payload, ensure_ascii=False))

    def stop(self, *, at: Optional[datetime] = None, summary: str = "") -> list[Activity]:
        """実行中の作業を終了し、Activity として保存する。

        日をまたぐ場合（ローカル日付の 00:00 をまたぐ場合）は日境界ごとに分割し、
        複数の Activity として保存する（例: 23:50 開始 → 翌 00:10 終了 は2件）。
        日をまたがない通常ケースでは、今までどおり1件だけの list を返す。

        実行中の作業が無ければ空リストを返す（例外にしない）。

        開始直後（`MIN_DURATION_SEC` 未満）に止めた場合は、押し間違いとみなして
        実行中の状態だけを消し、Activity を作らずに空リストを返す。
        長さ0分の活動をタイムラインへ残さないため。
        この判定は分割の前に、区間全体の長さで行う。
        """
        running = self.running()
        if running is None:
            return []

        ended_at = ensure_aware(at) if at is not None else now()
        started_at = running["started_at"]
        if (ended_at - started_at).total_seconds() < MIN_DURATION_SEC:
            self._meta.delete(_META_KEY)
            return []

        activities = [
            self._save(
                piece_start,
                piece_end,
                running["activity_type"],
                project=running["project"],
                task=running["task"],
                summary=summary,
            )
            for piece_start, piece_end in _split_range_by_day(started_at, ended_at)
        ]
        self._meta.delete(_META_KEY)
        return activities

    def running(self) -> dict | None:
        """実行中の作業を dict で返す。実行中が無ければ None。"""
        raw = self._meta.get(_META_KEY)
        if raw is None:
            return None
        payload = json.loads(raw)
        return {
            "activity_type": ActivityType(payload["activity_type"]),
            "project": payload.get("project"),
            "task": payload.get("task"),
            "started_at": ensure_aware(datetime.fromisoformat(payload["started_at"])),
        }

    def add(
        self,
        start: datetime,
        end: datetime,
        activity_type: ActivityType,
        *,
        project: Optional[str] = None,
        task: Optional[str] = None,
        summary: str = "",
    ) -> list[Activity]:
        """過去の時間帯を後からまとめて Activity として追加する。

        日をまたぐ場合は日境界ごとに分割し、複数の Activity として保存する。
        日をまたがない通常ケースでは、今までどおり1件だけの list を返す。
        """
        start_at = ensure_aware(start)
        end_at = ensure_aware(end)
        if start_at >= end_at:
            raise ValueError("start は end より前である必要がある")

        return [
            self._save(piece_start, piece_end, activity_type, project=project, task=task, summary=summary)
            for piece_start, piece_end in _split_range_by_day(start_at, end_at)
        ]

    def _save(
        self,
        start_at: datetime,
        end_at: datetime,
        activity_type: ActivityType,
        *,
        project: Optional[str],
        task: Optional[str],
        summary: str,
    ) -> Activity:
        """1区間を Activity として保存する（stop / add 共通）。"""
        activity = Activity(
            start_at=start_at,
            end_at=end_at,
            activity_type=activity_type,
            layer=ActivityLayer.REPORTED,
            source=Source.MANUAL,
            project=project,
            task=task,
            confidence=1.0,
            summary=summary,
        )
        activity.id = self._activities.add(activity)
        return activity
