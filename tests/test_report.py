"""report/daily_markdown.py のユニットテスト。

標準ライブラリの unittest のみを使用する。write_daily の出力先は tempfile の
一時ディレクトリにし、プロジェクト内（特に app/data）は汚さない。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tests.conftest_path import add_source_path

add_source_path()

from contextflow.config import AppConfig  # noqa: E402
from contextflow.contracts.models import (  # noqa: E402
    Activity,
    ActivityLayer,
    ActivityType,
    Change,
    CurrentState,
    Decision,
    Source,
    TimeSummary,
    WorkFeatures,
)
from contextflow.report.daily_markdown import render_daily, write_daily  # noqa: E402


def _state(context_switches: int = 2) -> CurrentState:
    return CurrentState(
        generated_at=datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc),
        target_date=date(2026, 9, 23),
        now_time="18:00",
        today=TimeSummary(
            total_min=90, by_type={"coding": 60, "meeting": 30}, by_project={"proj_x": 90}
        ),
        features=WorkFeatures(context_switches=context_switches),
    )


def _activity(start, end, activity_type, project=None, task=None, summary=""):
    return Activity(
        start_at=start,
        end_at=end,
        activity_type=activity_type,
        layer=ActivityLayer.CONFIRMED,
        source=Source.WINDOWS,
        project=project,
        task=task,
        summary=summary,
    )


class RenderDailyTests(unittest.TestCase):
    """render_daily: Time / Main activities / Context switches / Changes / Decisions / Sources。"""

    def test_all_sections_present_with_data(self):
        state = _state(context_switches=2)
        base = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
        activities = [
            _activity(
                base,
                base + timedelta(minutes=60),
                ActivityType.CODING,
                project="proj_x",
                task="Impl",
                summary="機能Aを実装",
            ),
            _activity(
                base + timedelta(minutes=60),
                base + timedelta(minutes=90),
                ActivityType.MEETING,
                project="proj_x",
            ),
        ]
        changes = [Change(ts=base, description="設定Xを変更")]
        decisions = [Decision(ts=base, decision="方針Bに決定", reason="理由C")]

        text = render_daily(state, activities, changes, decisions)

        self.assertIn("# 2026-09-23", text)
        self.assertIn("## Time", text)
        self.assertIn("## Main activities", text)
        self.assertIn("## Context switches", text)
        self.assertIn("## Changes", text)
        self.assertIn("## Decisions", text)
        self.assertIn("## Sources", text)

        self.assertIn(str(state.features.context_switches), text)
        self.assertIn("設定Xを変更", text)
        self.assertIn("方針Bに決定（理由C）", text)
        # 修正後: source=WINDOWS の活動は summary（生ログ由来）を出力しないため出ない
        self.assertNotIn("機能Aを実装", text)

    def test_empty_changes_and_decisions_render_as_none(self):
        state = _state()
        text = render_daily(state, [], [], [])
        lines = text.splitlines()

        changes_idx = lines.index("## Changes")
        decisions_idx = lines.index("## Decisions")
        # changes・decisions が空なら「なし」相当の表記になること
        self.assertEqual(lines[changes_idx + 1], "- なし")
        self.assertEqual(lines[decisions_idx + 1], "- なし")

        # activity が無ければ Main activities / Sources も同様に「なし」
        main_idx = lines.index("## Main activities")
        sources_idx = lines.index("## Sources")
        self.assertEqual(lines[main_idx + 1], "- なし")
        self.assertEqual(lines[sources_idx + 1], "- なし")


class WriteDailyTests(unittest.TestCase):
    """write_daily: 一時ディレクトリへ activity/daily/YYYY-MM-DD.md が書かれること。"""

    def test_writes_expected_path_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(data={"paths": {"export_dir": tmp}})
            state = _state()

            path = write_daily(config, state, [], [], [])

            expected_path = Path(tmp) / "activity" / "daily" / "2026-09-23.md"
            self.assertEqual(path, expected_path)
            self.assertTrue(expected_path.exists())

            content = expected_path.read_text(encoding="utf-8")
            self.assertIn("# 2026-09-23", content)
            self.assertIn("## Time", content)


if __name__ == "__main__":
    unittest.main()
