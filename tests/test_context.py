"""context/features.py・context/builder.py のユニットテスト。

標準ライブラリの unittest のみを使用する。DB・ファイルは tempfile で
一時領域に作り、プロジェクト内（特に app/data）は汚さない。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from datetime import time as dtime
from datetime import timedelta, timezone
from pathlib import Path

from tests.conftest_path import add_source_path

add_source_path()

from contextflow import timeutil  # noqa: E402
from contextflow.config import AppConfig  # noqa: E402
from contextflow.context.builder import ContextBuilder, state_to_flat_dict  # noqa: E402
from contextflow.context.features import compute_features, summarize_time  # noqa: E402
from contextflow.contracts.models import (  # noqa: E402
    Activity,
    ActivityLayer,
    ActivityType,
    Change,
    CurrentState,
    Decision,
    PlannedItem,
    PastSummary,
    Source,
    Task,
    TaskStatus,
    TimeSummary,
    UpcomingSummary,
    WorkFeatures,
)
from contextflow.planner.planner import _safe_state_payload  # noqa: E402
from contextflow.storage.db import Database  # noqa: E402
from contextflow.storage.repositories import ActivityRepository, TaskRepository  # noqa: E402


def _activity(
    start,
    end,
    activity_type,
    project=None,
    source=Source.WINDOWS,
    layer=ActivityLayer.CONFIRMED,
    task=None,
    summary="",
    detail=None,
):
    """テスト用の Activity を組み立てる小さなヘルパ。"""
    return Activity(
        start_at=start,
        end_at=end,
        activity_type=activity_type,
        layer=layer,
        source=source,
        project=project,
        task=task,
        summary=summary,
        detail=detail or {},
    )


class SummarizeTimeTests(unittest.TestCase):
    """summarize_time: activity_type別・project別の集計、projectがNoneのまとめ方。"""

    def test_type_and_project_aggregation(self):
        tz = timezone.utc
        base = datetime(2026, 9, 23, 9, 0, tzinfo=tz)
        activities = [
            _activity(base, base + timedelta(minutes=20), ActivityType.CODING, project="p1"),
            _activity(
                base + timedelta(minutes=20),
                base + timedelta(minutes=30),
                ActivityType.CODING,
                project="p1",
            ),
            _activity(
                base + timedelta(minutes=30),
                base + timedelta(minutes=45),
                ActivityType.RESEARCH,
                project=None,
            ),
            _activity(
                base + timedelta(minutes=45),
                base + timedelta(minutes=50),
                ActivityType.MEETING,
                project="p2",
            ),
        ]

        summary = summarize_time(activities)

        self.assertEqual(summary.total_min, 50)
        self.assertEqual(summary.by_type, {"coding": 30, "research": 15, "meeting": 5})
        # project が None のものは 'unassigned' にまとめられる
        self.assertEqual(summary.by_project, {"p1": 30, "unassigned": 15, "p2": 5})

    def test_empty_activities(self):
        summary = summarize_time([])
        self.assertEqual(summary.total_min, 0)
        self.assertEqual(summary.by_type, {})
        self.assertEqual(summary.by_project, {})


class ComputeFeaturesTests(unittest.TestCase):
    """compute_features: deep_work_min / context_switches / longest_focus_min / last_break_min_ago。"""

    def setUp(self):
        # 実 config.toml に依存せず、閾値を明示して疎結合にする
        self.config = AppConfig(
            data={"features": {"deep_work_min_sec": 1500, "context_switch_min_sec": 60}}
        )

    def test_features_values(self):
        tz = timezone.utc
        base = datetime(2026, 9, 23, 9, 0, tzinfo=tz)
        activities = [
            _activity(
                base, base + timedelta(minutes=40), ActivityType.CODING, project="proj_x"
            ),
            _activity(
                base + timedelta(minutes=40),
                base + timedelta(minutes=50),
                ActivityType.RESEARCH,
                project="proj_y",
            ),
            _activity(
                base + timedelta(minutes=50),
                base + timedelta(minutes=60),
                ActivityType.BREAK,
                project=None,
            ),
            _activity(
                base + timedelta(minutes=60),
                base + timedelta(minutes=90),
                ActivityType.CODING,
                project="proj_x",
            ),
        ]
        now = base + timedelta(minutes=100)  # 10:40

        features = compute_features(activities, [], self.config, now=now)

        self.assertEqual(features.deep_work_min, 70)  # 40分+30分（25分未満のresearchは除外）
        self.assertEqual(features.context_switches, 3)  # proj_x->proj_y, ->break, ->proj_x
        self.assertEqual(features.longest_focus_min, 40)
        self.assertEqual(features.active_min, 90)
        self.assertEqual(features.idle_min, 10)
        self.assertEqual(features.last_break_min_ago, 40)

    def test_no_break_means_last_break_none(self):
        tz = timezone.utc
        base = datetime(2026, 9, 23, 9, 0, tzinfo=tz)
        activities = [
            _activity(base, base + timedelta(minutes=30), ActivityType.CODING, project="p1")
        ]
        features = compute_features(
            activities, [], self.config, now=base + timedelta(minutes=30)
        )
        self.assertIsNone(features.last_break_min_ago)

    def test_empty_activities_returns_default(self):
        # データが1件も無くても例外にならず、既定値の WorkFeatures を返す
        features = compute_features([], [], self.config)
        self.assertEqual(features, WorkFeatures())


class ContextBuilderTests(unittest.TestCase):
    """ContextBuilder.build: 一時DBで合成activityを入れてCurrentStateが返ること。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "test.db"
        self.db = Database(db_path)
        self.db.initialize()
        self.addCleanup(self.db.close)
        # context_repo は実在しないパスにして、制約読み込みが例外を握りつぶすことを利用する
        self.config = AppConfig(
            data={"paths": {"context_repo": str(Path(self._tmp.name) / "context_repo")}}
        )
        self.tz = timeutil.now().tzinfo

    def test_build_with_synthetic_activities(self):
        target = date(2026, 9, 23)
        base = datetime.combine(target, dtime.min, tzinfo=self.tz) + timedelta(hours=9)
        repo = ActivityRepository(self.db)
        repo.add(
            _activity(base, base + timedelta(minutes=30), ActivityType.CODING, project="proj_x")
        )
        repo.add(
            _activity(
                base + timedelta(hours=1),
                base + timedelta(hours=1, minutes=15),
                ActivityType.MEETING,
                project=None,
            )
        )

        builder = ContextBuilder(self.db, self.config)
        now = base + timedelta(minutes=10)
        state = builder.build(target=target, now=now)

        self.assertIsInstance(state, CurrentState)
        self.assertEqual(state.target_date, target)
        self.assertEqual(state.today.total_min, 45)
        self.assertIsNotNone(state.current_activity)
        self.assertEqual(state.current_activity.project, "proj_x")

    def test_build_with_no_data_does_not_raise(self):
        # データが1件も無い日でも例外にならないこと
        target = date(2026, 9, 24)
        builder = ContextBuilder(self.db, self.config)
        try:
            state = builder.build(target=target)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"データが無い日で build() が例外を送出: {exc}")

        self.assertIsInstance(state, CurrentState)
        self.assertEqual(state.today.total_min, 0)
        self.assertIsNone(state.current_activity)
        self.assertEqual(state.open_tasks, 0)


class StateToFlatDictTests(unittest.TestCase):
    """state_to_flat_dict: キー集合の一致、生ログ・ウィンドウタイトルが含まれないこと。"""

    # docs/20260923_architecture.md の「5. Current State」節に載る一覧。
    # docs/20260923_module_contract.md 側は関数シグネチャのみでキー列挙は無いため、
    # 実装（context/builder.py の state_to_flat_dict）と一致するこちらの一覧を正とする。
    EXPECTED_KEYS = {
        "time",
        "date",
        "today_total_min",
        "today_focus_min",
        "by_type",
        "by_project",
        "current_activity_type",
        "current_project",
        "current_task",
        "task_elapsed_min",
        "open_tasks",
        "blocked_tasks",
        "recent_context_switches",
        "deep_work_min",
        "longest_focus_min",
        "active_min",
        "idle_min",
        "last_break_min_ago",
        "candidate_tasks",
        "recent_changes",
        "recent_decisions",
        "constraints",
        "task_blocked",
        "task_deadline_days",
        "task_priority",
        "task_remaining_steps",
        # 過去の傾向（recent_*）・未来の先読み（upcoming_*）
        "past_days",
        "past_total_min",
        "past_by_type",
        "past_by_project",
        "past_deep_work_min",
        "past_context_switches",
        "past_active_days",
        "past_change_count",
        "past_decision_count",
        "upcoming_days",
        "upcoming_planned_min",
        "upcoming_by_type",
        "upcoming_items",
        "upcoming_deadlines",
    }

    def _build_state(self):
        tz = timezone.utc
        base = datetime(2026, 9, 23, 9, 0, tzinfo=tz)
        secret_summary = "SECRET_WINDOW_TITLE_xyz"
        secret_detail_title = "raw-window-title-should-not-leak"
        current_activity = _activity(
            base,
            base + timedelta(minutes=30),
            ActivityType.CODING,
            project="proj_x",
            task="Implement X",
            summary=secret_summary,
            detail={"window_title": secret_detail_title, "process": "devenv.exe"},
        )
        current_task = Task(
            title="Implement X",
            project="proj_x",
            status=TaskStatus.IN_PROGRESS,
            priority=2,
            deadline=date(2026, 9, 25),
            blocked=False,
            remaining_steps=3,
        )
        candidate_tasks = [
            current_task,
            Task(title="Write docs", project="proj_x", priority=4, remaining_steps=1),
        ]
        state = CurrentState(
            generated_at=base,
            target_date=date(2026, 9, 23),
            now_time="09:30",
            today=TimeSummary(total_min=90, by_type={"coding": 90}, by_project={"proj_x": 90}),
            features=WorkFeatures(
                deep_work_min=70,
                context_switches=3,
                longest_focus_min=40,
                active_min=90,
                idle_min=10,
                last_break_min_ago=40,
            ),
            current_activity=current_activity,
            current_task=current_task,
            task_elapsed_min=45,
            open_tasks=5,
            blocked_tasks=1,
            candidate_tasks=candidate_tasks,
            recent_changes=[
                Change(ts=base, description="設定Xを変更", project="sample_project")
            ],
            recent_decisions=[
                Decision(ts=base, decision="Yに決定", reason="Zのため", project="sample_project")
            ],
            constraints=["制約1"],
        )
        return state, secret_summary, secret_detail_title

    def test_key_set_matches_contract(self):
        state, _summary, _detail_title = self._build_state()
        flat = state_to_flat_dict(state)
        self.assertEqual(set(flat.keys()), self.EXPECTED_KEYS)

    def test_recent_changes_include_project_and_time(self):
        """変化は案件名と日時つきで渡る。

        案件名は「今の案件のものか」、日時は「古い話か直近か」の判断に要る。
        """
        state, _summary, _detail = self._build_state()
        flat = state_to_flat_dict(state)
        self.assertEqual(len(flat["recent_changes"]), 1)
        change = flat["recent_changes"][0]
        self.assertEqual(change["description"], "設定Xを変更")
        self.assertEqual(change["project"], "sample_project")
        self.assertIn("ts", change)
        self.assertIsInstance(change["minutes_ago"], int)

    def test_recent_decisions_include_project_reason_and_time(self):
        """判断は案件名・理由・日時つきで渡る。"""
        state, _summary, _detail = self._build_state()
        flat = state_to_flat_dict(state)
        decision = flat["recent_decisions"][0]
        self.assertEqual(decision["decision"], "Yに決定")
        self.assertEqual(decision["reason"], "Zのため")
        self.assertEqual(decision["project"], "sample_project")
        self.assertIn("ts", decision)
        self.assertIsInstance(decision["minutes_ago"], int)

    def test_minutes_ago_uses_generated_at_as_reference(self):
        """経過時間の基準は state を作った時刻。同じ state なら何度呼んでも同じ値。"""
        state, _summary, _detail = self._build_state()
        first = state_to_flat_dict(state)["recent_changes"][0]["minutes_ago"]
        second = state_to_flat_dict(state)["recent_changes"][0]["minutes_ago"]
        self.assertEqual(first, second)

    def test_future_timestamp_gives_negative_minutes_ago(self):
        """未来の時刻（手入力で後の時刻を指定）は負になる。0 へ丸めて「今」と混ぜない。"""
        state, _summary, _detail = self._build_state()
        state.recent_changes[0].ts = state.generated_at + timedelta(minutes=20)
        flat = state_to_flat_dict(state)
        self.assertLess(flat["recent_changes"][0]["minutes_ago"], 0)

    def test_no_raw_log_or_window_title_leak(self):
        state, secret_summary, secret_detail_title = self._build_state()
        flat = state_to_flat_dict(state)
        serialized = json.dumps(flat, ensure_ascii=False, default=str)
        self.assertNotIn(secret_summary, serialized)
        self.assertNotIn(secret_detail_title, serialized)
        self.assertNotIn("devenv.exe", serialized)


class RecentUpcomingBuilderTests(unittest.TestCase):
    """ContextBuilder.build: 過去の傾向（recent）・未来の先読み（upcoming）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "test.db"
        self.db = Database(db_path)
        self.db.initialize()
        self.addCleanup(self.db.close)
        # context_repo は実在しないパスにして、制約読み込みが例外を握りつぶすことを利用する
        self.config = AppConfig(
            data={
                "paths": {"context_repo": str(Path(self._tmp.name) / "context_repo")},
                "context": {"past_days": 3, "upcoming_days": 2},
            }
        )
        self.tz = timeutil.now().tzinfo
        self.target = date(2026, 9, 24)
        self.target_start = datetime.combine(self.target, dtime.min, tzinfo=self.tz)
        self.activities = ActivityRepository(self.db)
        self.tasks = TaskRepository(self.db)

    def test_past_total_min_and_active_days_exclude_target_date(self):
        # 対象日の1〜3日前に30分ずつ activity（recent の対象）
        for days_ago in (1, 2, 3):
            day_start = self.target_start - timedelta(days=days_ago)
            self.activities.add(
                _activity(
                    day_start + timedelta(hours=9),
                    day_start + timedelta(hours=9, minutes=30),
                    ActivityType.CODING,
                    project="p1",
                )
            )
        # 対象日当日にも入れておく（recent には含まれないはず）
        self.activities.add(
            _activity(
                self.target_start + timedelta(hours=9),
                self.target_start + timedelta(hours=10),
                ActivityType.CODING,
                project="p1",
            )
        )

        builder = ContextBuilder(self.db, self.config)
        state = builder.build(
            target=self.target, now=self.target_start + timedelta(hours=9, minutes=30)
        )

        self.assertEqual(state.past.days, 3)
        self.assertEqual(state.past.total_min, 90)  # 30分 x 3日（対象日ぶんの60分は含まない）
        self.assertEqual(state.past.active_days, 3)

    def test_upcoming_items_planned_only_with_summary_no_double_count(self):
        at = self.target_start + timedelta(hours=9)
        planned_start = self.target_start + timedelta(days=1, hours=10)
        # planned（先読みの対象）
        self.activities.add(
            _activity(
                planned_start,
                planned_start + timedelta(minutes=60),
                ActivityType.MEETING,
                project="p1",
                layer=ActivityLayer.PLANNED,
                source=Source.CALENDAR,
                summary="定例MTG",
            )
        )
        # 同じ時間帯の confirmed（upcoming では二重に数えないことの確認用）
        self.activities.add(
            _activity(
                planned_start,
                planned_start + timedelta(minutes=60),
                ActivityType.MEETING,
                project="p1",
                layer=ActivityLayer.CONFIRMED,
                summary="実績側の件名",
            )
        )

        builder = ContextBuilder(self.db, self.config)
        state = builder.build(target=self.target, now=at)

        self.assertEqual(state.upcoming.days, 2)
        self.assertEqual(len(state.upcoming.items), 1)
        item = state.upcoming.items[0]
        self.assertEqual(item.summary, "定例MTG")
        self.assertEqual(item.duration_min, 60)
        self.assertEqual(state.upcoming.planned_min, 60)

    def test_upcoming_deadlines_only_in_range(self):
        at = self.target_start + timedelta(hours=9)
        self.tasks.upsert(
            Task(title="範囲内", project="p1", deadline=self.target + timedelta(days=1))
        )
        self.tasks.upsert(
            Task(title="範囲外", project="p1", deadline=self.target + timedelta(days=10))
        )

        builder = ContextBuilder(self.db, self.config)
        state = builder.build(target=self.target, now=at)

        titles = [t.title for t in state.upcoming.deadlines]
        self.assertIn("範囲内", titles)
        self.assertNotIn("範囲外", titles)

    def test_no_data_returns_empty_recent_and_upcoming_without_raising(self):
        # データが1件も無い日でも例外にならないこと
        builder = ContextBuilder(self.db, self.config)
        try:
            state = builder.build(target=self.target)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"データが無い日で build() が例外を送出: {exc}")

        self.assertEqual(state.past.total_min, 0)
        self.assertEqual(state.past.active_days, 0)
        self.assertEqual(state.upcoming.items, [])
        self.assertEqual(state.upcoming.deadlines, [])


class SafeStatePayloadUpcomingTests(unittest.TestCase):
    """planner._safe_state_payload: upcoming_items の件名が渡り、生ログは渡らないこと。"""

    def _build_state(self):
        tz = timezone.utc
        base = datetime(2026, 9, 23, 9, 0, tzinfo=tz)
        secret_summary = "SECRET_WINDOW_TITLE_xyz"
        secret_detail_title = "raw-window-title-should-not-leak"
        current_activity = _activity(
            base,
            base + timedelta(minutes=30),
            ActivityType.CODING,
            project="proj_x",
            summary=secret_summary,
            detail={"window_title": secret_detail_title, "process": "devenv.exe"},
        )
        planned_summary = "顧客Aとの定例MTG"
        state = CurrentState(
            generated_at=base,
            target_date=date(2026, 9, 23),
            now_time="09:30",
            current_activity=current_activity,
            past=PastSummary(days=3, total_min=60, active_days=1),
            upcoming=UpcomingSummary(
                days=2,
                planned_min=60,
                items=[
                    PlannedItem(
                        start_at=base + timedelta(days=1),
                        end_at=base + timedelta(days=1, minutes=60),
                        activity_type=ActivityType.MEETING,
                        summary=planned_summary,
                        project="proj_x",
                    )
                ],
            ),
        )
        return state, secret_summary, secret_detail_title, planned_summary

    def test_upcoming_items_carry_summary(self):
        state, _s, _d, planned_summary = self._build_state()
        payload = _safe_state_payload(state)
        self.assertIn("upcoming", payload)
        self.assertEqual(len(payload["upcoming"]["items"]), 1)
        self.assertEqual(payload["upcoming"]["items"][0]["summary"], planned_summary)
        self.assertEqual(payload["past"]["days"], 3)

    def test_current_activity_summary_and_detail_not_leaked(self):
        """current_activity.summary/detail（生ログ・ウィンドウタイトル）は渡さない回帰テスト。"""
        state, secret_summary, secret_detail_title, _planned = self._build_state()
        payload = _safe_state_payload(state)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        self.assertNotIn(secret_summary, serialized)
        self.assertNotIn(secret_detail_title, serialized)
        self.assertNotIn("devenv.exe", serialized)


if __name__ == "__main__":
    unittest.main()
