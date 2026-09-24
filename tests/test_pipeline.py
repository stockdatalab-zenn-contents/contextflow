"""tests/test_pipeline.py

pipeline 配下（p10_sessionizer / p20_classifier / p30_activity_builder）のテスト。
標準ライブラリの unittest のみを使う。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tests.conftest_path import add_source_path

add_source_path()

from contextflow.config import AppConfig  # noqa: E402
from contextflow.contracts.models import (  # noqa: E402
    Activity,
    ActivityLayer,
    ActivityType,
    RawEvent,
    Source,
)
from contextflow.pipeline.p10_sessionizer import build_sessions  # noqa: E402
from contextflow.pipeline.p20_classifier import Classifier  # noqa: E402
from contextflow.pipeline.p30_activity_builder import (  # noqa: E402
    DEFAULT_PLANNED_CONFIDENCE,
    find_gaps,
    merge_layers,
)

# 全テスト共通の基準時刻（tz付き。マシンのローカルタイムゾーンに依存させない）
BASE = datetime(2026, 9, 23, 9, 0, 0, tzinfo=timezone.utc)


def _ev(offset_sec: int, process: str, title: str, idle_sec: int = 0) -> RawEvent:
    """BASE からの経過秒で RawEvent を作る小さなヘルパ。"""
    return RawEvent(
        ts=BASE + timedelta(seconds=offset_sec),
        process=process,
        window_title=title,
        idle_sec=idle_sec,
    )


# ---------------------------------------------------------------------------
# build_sessions
# ---------------------------------------------------------------------------


class TestBuildSessions(unittest.TestCase):
    """p10_sessionizer.build_sessions のテスト。"""

    def setUp(self) -> None:
        self.config = AppConfig(
            data={
                "sessionizer": {
                    "merge_gap_sec": 60,
                    "min_duration_sec": 30,
                    "merge_by_process_only": False,
                },
                "collector": {"idle_threshold_sec": 180, "interval_sec": 5},
            }
        )

    def test_empty_events_returns_empty_list(self) -> None:
        self.assertEqual(build_sessions([], self.config), [])

    def test_merges_same_process_and_title_and_splits_on_title_change(self) -> None:
        """同一 process・title で間隔が merge_gap_sec 以内なら1 session に連結され、
        title が変わると別 session に分かれることを確認する。"""
        events = [
            _ev(0, "Code.exe", "a.py - VS Code"),
            _ev(5, "Code.exe", "a.py - VS Code"),
            _ev(10, "Code.exe", "a.py - VS Code"),
            _ev(15, "Code.exe", "a.py - VS Code"),
            _ev(20, "Code.exe", "a.py - VS Code"),
            _ev(25, "Code.exe", "a.py - VS Code"),
            # タイトルが変わる -> 新しい session
            _ev(30, "Code.exe", "b.py - VS Code"),
            _ev(35, "Code.exe", "b.py - VS Code"),
            _ev(40, "Code.exe", "b.py - VS Code"),
            _ev(45, "Code.exe", "b.py - VS Code"),
            _ev(50, "Code.exe", "b.py - VS Code"),
            _ev(55, "Code.exe", "b.py - VS Code"),
        ]
        sessions = build_sessions(events, self.config)
        self.assertEqual(len(sessions), 2)

        s1, s2 = sessions
        self.assertEqual(s1.process, "Code.exe")
        self.assertEqual(s1.window_title, "a.py - VS Code")
        self.assertEqual(s1.start_at, BASE)
        # 終了時刻は最後のイベント時刻 + interval_sec(5)
        self.assertEqual(s1.end_at, BASE + timedelta(seconds=30))
        self.assertEqual(s1.duration_sec, 30)
        self.assertEqual(s1.sample_count, 6)

        self.assertEqual(s2.window_title, "b.py - VS Code")
        self.assertEqual(s2.start_at, BASE + timedelta(seconds=30))
        self.assertEqual(s2.end_at, BASE + timedelta(seconds=60))

    def test_splits_session_on_idle_exceeding_threshold(self) -> None:
        """idle_threshold_sec を超える idle イベントで session が切れ、
        そのイベント自身はどちらの session にも含まれないことを確認する。"""
        events = [
            _ev(0, "Code.exe", "a.py"),
            _ev(5, "Code.exe", "a.py"),
            _ev(10, "Code.exe", "a.py"),
            _ev(15, "Code.exe", "a.py"),
            _ev(20, "Code.exe", "a.py"),
            _ev(25, "Code.exe", "a.py"),
            # 直前との間隔は5秒（merge_gap_sec内）だが、idle_sec(181) > idle_threshold_sec(180)
            # なのでここで session を切り、このイベント自体は除外される
            _ev(30, "Code.exe", "a.py", idle_sec=181),
            _ev(40, "Code.exe", "a.py"),
            _ev(45, "Code.exe", "a.py"),
            _ev(50, "Code.exe", "a.py"),
            _ev(55, "Code.exe", "a.py"),
            _ev(60, "Code.exe", "a.py"),
            _ev(65, "Code.exe", "a.py"),
        ]
        sessions = build_sessions(events, self.config)
        self.assertEqual(len(sessions), 2)
        # idle イベント(t=30)の時刻はどちらの session にも含まれない
        self.assertEqual(sessions[0].end_at, BASE + timedelta(seconds=30))
        self.assertEqual(sessions[1].start_at, BASE + timedelta(seconds=40))
        self.assertEqual(sessions[1].end_at, BASE + timedelta(seconds=70))

    def test_discards_sessions_below_min_duration(self) -> None:
        """min_duration_sec 未満の session は破棄されることを確認する。"""
        events = [
            # 2件・間隔5秒 -> duration = (5+interval5) - 0 = 10秒 < min_duration_sec(30) -> 破棄
            _ev(0, "notepad.exe", "memo.txt"),
            _ev(5, "notepad.exe", "memo.txt"),
            # 別プロセスへ切り替え。こちらは十分な長さがあるので残る
            _ev(100, "Code.exe", "a.py"),
            _ev(105, "Code.exe", "a.py"),
            _ev(110, "Code.exe", "a.py"),
            _ev(115, "Code.exe", "a.py"),
            _ev(120, "Code.exe", "a.py"),
            _ev(125, "Code.exe", "a.py"),
        ]
        sessions = build_sessions(events, self.config)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].process, "Code.exe")


# ---------------------------------------------------------------------------
# Classifier.classify
# ---------------------------------------------------------------------------


class TestClassifier(unittest.TestCase):
    """p20_classifier.Classifier のテスト。"""

    def setUp(self) -> None:
        categories = {
            "default_activity_type": "other",
            "title_rules": [
                {"contains": ["Teams 会議", "Zoom Meeting"], "activity_type": "meeting"},
            ],
            "process_rules": [
                {"match": ["Code.exe"], "activity_type": "coding"},
                {"match": ["Teams.exe"], "activity_type": "communication"},
            ],
            "project_rules": [
                {"contains": ["contextflow"], "project": "contextflow"},
            ],
        }
        self.classifier = Classifier(categories)

    def test_title_rules_take_priority_over_process_rules(self) -> None:
        # process としては communication(Teams.exe) と判定されるはずの組だが、
        # タイトルに "Teams 会議" が含まれるため title_rules が優先され meeting になる
        activity_type, _ = self.classifier.classify("Teams.exe", "Teams 会議 - 定例MTG")
        self.assertEqual(activity_type, ActivityType.MEETING)

    def test_process_rules_used_when_title_has_no_match(self) -> None:
        activity_type, _ = self.classifier.classify("Code.exe", "main.py - Visual Studio Code")
        self.assertEqual(activity_type, ActivityType.CODING)

    def test_default_activity_type_when_nothing_matches(self) -> None:
        activity_type, project = self.classifier.classify("unknown.exe", "何かの画面")
        self.assertEqual(activity_type, ActivityType.OTHER)
        self.assertIsNone(project)

    def test_project_is_inferred_from_title(self) -> None:
        _, project = self.classifier.classify("Code.exe", "contextflow - main.py - VS Code")
        self.assertEqual(project, "contextflow")

    def test_project_is_none_when_no_rule_matches(self) -> None:
        _, project = self.classifier.classify("Code.exe", "other-repo - main.py")
        self.assertIsNone(project)


# ---------------------------------------------------------------------------
# merge_layers
# ---------------------------------------------------------------------------


class TestMergeLayers(unittest.TestCase):
    """p30_activity_builder.merge_layers のテスト。"""

    def setUp(self) -> None:
        # config は既定値(DEFAULT_MIN_FRAGMENT_SEC / DEFAULT_PLANNED_CONFIDENCE)を使う
        self.config = AppConfig(data={})
        self.day = datetime(2026, 9, 23, 13, 0, 0, tzinfo=timezone.utc)

    def test_reported_and_observed_split_planned_meeting(self) -> None:
        """参照資料の例:
        予定 13:00-14:00 会議 / 観測 13:40-14:00 PowerPoint / 手入力 13:00-13:40 会議
        -> 13:00-13:40 (手入力/会議) と 13:40-14:00 (PCログ/資料作成) に分かれる。
        """
        day = self.day
        planned = [
            Activity(
                start_at=day,
                end_at=day + timedelta(hours=1),
                activity_type=ActivityType.MEETING,
                layer=ActivityLayer.PLANNED,
                source=Source.CALENDAR,
                confidence=1.0,
                summary="定例会議",
            )
        ]
        observed = [
            Activity(
                start_at=day + timedelta(minutes=40),
                end_at=day + timedelta(hours=1),
                activity_type=ActivityType.DOCUMENT,
                layer=ActivityLayer.OBSERVED,
                source=Source.WINDOWS,
                confidence=0.8,
                summary="POWERPNT.EXE / スライド作成",
            )
        ]
        reported = [
            Activity(
                start_at=day,
                end_at=day + timedelta(minutes=40),
                activity_type=ActivityType.MEETING,
                layer=ActivityLayer.REPORTED,
                source=Source.MANUAL,
                confidence=1.0,
                summary="会議",
            )
        ]

        confirmed = merge_layers(planned, observed, reported, self.config)

        self.assertEqual(len(confirmed), 2)
        first, second = confirmed

        # 13:00-13:40 は手入力(manual)が勝つ
        self.assertEqual(first.start_at, day)
        self.assertEqual(first.end_at, day + timedelta(minutes=40))
        self.assertEqual(first.source, Source.MANUAL)
        self.assertEqual(first.activity_type, ActivityType.MEETING)

        # 13:40-14:00 は観測(windows)が残る
        self.assertEqual(second.start_at, day + timedelta(minutes=40))
        self.assertEqual(second.end_at, day + timedelta(hours=1))
        self.assertEqual(second.source, Source.WINDOWS)
        self.assertEqual(second.activity_type, ActivityType.DOCUMENT)

        # 区間が重ならないこと
        self.assertLessEqual(first.end_at, second.start_at)

        # 予定は手入力・観測で完全に埋まるので、予定由来の断片は残らない
        self.assertTrue(all(a.layer == ActivityLayer.CONFIRMED for a in confirmed))
        self.assertFalse(any(a.summary == "定例会議" for a in confirmed))

    def test_intervals_never_overlap_across_layers(self) -> None:
        """3層が複雑に重なっても、確定後の区間は重複しないことを確認する。"""
        day = self.day
        planned = [
            Activity(
                start_at=day,
                end_at=day + timedelta(hours=2),
                activity_type=ActivityType.MEETING,
                layer=ActivityLayer.PLANNED,
                source=Source.CALENDAR,
                confidence=1.0,
            )
        ]
        observed = [
            Activity(
                start_at=day + timedelta(minutes=10),
                end_at=day + timedelta(minutes=90),
                activity_type=ActivityType.CODING,
                layer=ActivityLayer.OBSERVED,
                source=Source.WINDOWS,
                confidence=0.8,
            )
        ]
        reported = [
            Activity(
                start_at=day + timedelta(minutes=30),
                end_at=day + timedelta(minutes=50),
                activity_type=ActivityType.MEETING,
                layer=ActivityLayer.REPORTED,
                source=Source.MANUAL,
                confidence=1.0,
            )
        ]

        confirmed = merge_layers(planned, observed, reported, self.config)
        for prev, cur in zip(confirmed, confirmed[1:]):
            self.assertLessEqual(prev.end_at, cur.start_at)

    def test_planned_only_period_has_lowered_confidence(self) -> None:
        """観測・手入力が無く予定だけの時間帯は confidence が下がることを確認する。"""
        day = self.day
        planned = [
            Activity(
                start_at=day,
                end_at=day + timedelta(hours=1),
                activity_type=ActivityType.MEETING,
                layer=ActivityLayer.PLANNED,
                source=Source.CALENDAR,
                confidence=1.0,
            )
        ]
        confirmed = merge_layers(planned, [], [], self.config)
        self.assertEqual(len(confirmed), 1)
        self.assertLess(confirmed[0].confidence, 1.0)
        self.assertEqual(confirmed[0].confidence, DEFAULT_PLANNED_CONFIDENCE)


# ---------------------------------------------------------------------------
# find_gaps
# ---------------------------------------------------------------------------


class TestFindGaps(unittest.TestCase):
    """p30_activity_builder.find_gaps のテスト。"""

    def test_returns_only_gaps_at_or_above_threshold(self) -> None:
        day_start = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
        day_end = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        activities = [
            # 9:00-9:30
            Activity(start_at=day_start, end_at=day_start + timedelta(minutes=30)),
            # 9:30-9:35 の隙間(5分=300秒) -> min_gap_sec(600) 未満なので対象外
            Activity(
                start_at=day_start + timedelta(minutes=35),
                end_at=day_start + timedelta(minutes=60),
            ),
            # 10:00-10:20 の隙間(20分=1200秒) -> 対象
            Activity(
                start_at=day_start + timedelta(minutes=80),
                end_at=day_start + timedelta(minutes=120),
            ),
            # 11:00-12:00 は活動なし(60分) -> 対象
        ]
        gaps = find_gaps(activities, day_start, day_end, min_gap_sec=600)
        self.assertEqual(len(gaps), 2)
        self.assertEqual(
            gaps[0],
            (day_start + timedelta(minutes=60), day_start + timedelta(minutes=80)),
        )
        self.assertEqual(gaps[1], (day_start + timedelta(minutes=120), day_end))

    def test_no_activities_returns_whole_window_as_gap(self) -> None:
        start = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        gaps = find_gaps([], start, end, min_gap_sec=600)
        self.assertEqual(gaps, [(start, end)])

    def test_empty_window_returns_no_gaps(self) -> None:
        start = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(find_gaps([], start, start, min_gap_sec=1), [])


if __name__ == "__main__":
    unittest.main()
