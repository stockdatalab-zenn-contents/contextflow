"""既存の仕事管理リポジトリ（work_repo layout）との連携テスト。

連携先の仕事管理リポジトリの記法（`docs/20260923_work_repo_integration.md`）を
一時ディレクトリに再現し、`ContextRepo`（work_repo / standalone 両レイアウト）・
`GitHubIssues`（ラベル変換）・`GitSync.commit`（paths 指定）を検証する。

- `tempfile.TemporaryDirectory` の外には一切書き込まない（app/data・references を汚さない）。
- ネットワークアクセスは行わない（GitHubIssues は urlopen をモックする）。
- `AppConfig` は `load_config()` で実ファイルを読んだ上で `data` を書き換えるだけで、
  実設定ファイル（app/source/config/config.toml）は一切書き換えない。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests.conftest_path import add_source_path

add_source_path()

from contextflow.config import load_config  # noqa: E402
from contextflow.contracts.models import TaskStatus  # noqa: E402
from contextflow.sources.git_sync import GitSync  # noqa: E402
from contextflow.sources.github_context import (  # noqa: E402
    LAYOUT_STANDALONE,
    LAYOUT_WORK_REPO,
    ContextRepo,
    GitHubIssues,
)

# read_tasks / GitHubIssues の期限計算を固定するための基準日（2026-09-23 は水曜日）
_FIXED_TODAY = date(2026, 9, 23)
_END_OF_WEEK = date(2026, 9, 27)   # 今週末（日曜）
_END_OF_MONTH = date(2026, 9, 30)  # 当月末


def _snapshot(root: Path) -> set[str]:
    """root 配下の全パス（ファイル・ディレクトリ）を相対パス文字列の集合で返す。"""
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _build_pseudo_work_repo(root: Path) -> None:
    """連携先の仕事管理リポジトリのフォルダ構成を模した最小の擬似リポジトリを作る。"""
    main_project = root / "10_projects" / "50c_202609_tasks_demo"
    (main_project / "meetings").mkdir(parents=True, exist_ok=True)
    (main_project / "notes").mkdir(parents=True, exist_ok=True)
    (main_project / "README.md").write_text("# tasks demo\n\n現在地: 設計中\n", encoding="utf-8")

    # 案件走査から除外されるべきフォルダ
    (root / "10_projects" / "99_old" / "archived_thing").mkdir(parents=True, exist_ok=True)
    (root / "10_projects" / ".hidden_wip").mkdir(parents=True, exist_ok=True)

    # 期間部分の形式が崩れている・無い案件名
    (root / "10_projects" / "misc_notes").mkdir(parents=True, exist_ok=True)
    (root / "10_projects" / "archive").mkdir(parents=True, exist_ok=True)

    (root / "20_knowledge" / "00a_sample_synthesized").mkdir(parents=True, exist_ok=True)
    (root / "00_inbox").mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# work repo\n", encoding="utf-8")


def _work_repo_config(**overrides):
    """実 config.toml を読み込んだ AppConfig の [context_repo] だけ上書きする。

    実ファイルは一切書き換えない（load_config() のたびに新しい dict が作られるため、
    ここでの変更はこの AppConfig インスタンスに閉じる）。
    """
    config = load_config()
    section = dict(config.data.get("context_repo", {}))
    section.update(overrides)
    config.data["context_repo"] = section
    return config


# ---------------------------------------------------------------------------
# work_repo レイアウト: ensure_layout の非破壊性 / projects() のフォルダ名分解 / project_key
# ---------------------------------------------------------------------------


class WorkRepoLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "work_repo"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ensure_layout_creates_nothing_when_root_missing(self) -> None:
        # root 自体がまだ存在しない状態でも新規作成しない
        self.assertFalse(self.root.exists())
        repo = ContextRepo(self.root, _work_repo_config())
        repo.ensure_layout()
        self.assertFalse(self.root.exists())

    def test_ensure_layout_does_not_add_or_remove_anything(self) -> None:
        _build_pseudo_work_repo(self.root)
        before = _snapshot(self.root)

        repo = ContextRepo(self.root, _work_repo_config())
        repo.ensure_layout()

        after = _snapshot(self.root)
        self.assertEqual(before, after)

    def test_projects_parses_prefix_period_suffix_and_excludes_noise(self) -> None:
        _build_pseudo_work_repo(self.root)
        repo = ContextRepo(self.root, _work_repo_config())

        projects = repo.projects()
        names = {p.folder for p in projects}
        self.assertNotIn("99_old", names)
        self.assertNotIn(".hidden_wip", names)

        by_folder = {p.folder: p for p in projects}

        main = by_folder["50c_202609_tasks_demo"]
        self.assertEqual(main.prefix, "50c")
        self.assertEqual(main.period, "202609")
        self.assertEqual(main.suffix, "tasks_demo")

        no_period = by_folder["misc_notes"]
        self.assertEqual(no_period.prefix, "misc")
        self.assertEqual(no_period.period, "")
        self.assertEqual(no_period.suffix, "notes")

        solo = by_folder["archive"]
        self.assertEqual(solo.prefix, "archive")
        self.assertEqual(solo.period, "")
        self.assertEqual(solo.suffix, "")

    def test_project_key_switches_by_suffix_prefix_folder(self) -> None:
        _build_pseudo_work_repo(self.root)

        cases = (
            ("suffix", "tasks_demo"),
            ("prefix", "50c"),
            ("folder", "50c_202609_tasks_demo"),
        )
        for key_kind, expected in cases:
            repo = ContextRepo(self.root, _work_repo_config(project_key=key_kind))
            main = repo.find_project("50c_202609_tasks_demo")  # フォルダ名では常に引ける
            self.assertIsNotNone(main, msg=f"key_kind={key_kind}")
            self.assertEqual(main.key, expected, msg=f"key_kind={key_kind}")


# ---------------------------------------------------------------------------
# read_tasks（今回の中心）
# ---------------------------------------------------------------------------

_MAIN_TASKS_MD = """## Next
- [ ] 設計レビューの指摘を反映する @doing !high
- [ ] 見積前提を確認する @today
- [ ] 社内調整メモを作る !skip

## Later
- [ ] 運用手順を見直す @this-month

## Waiting
- [ ] 法務レビューの回答待ち

## Cancelled
- [ ] 旧方式の検証を進める

## Done
- [x] キックオフ資料を作成した #12
"""

_BROKEN_TASKS_MD = (
    "## Unknown Section\n"
    "- [ ] このセクションは無視される\n"
    "\n"
    "## Next\n"
    "not a checkbox line\n"
    "- [ ] \n"
    "- [ ] 正常なタスク\n"
    "### Not H2 heading\n"
    "- [ ] H3見出し配下は無視\n"
)

_LATER_PLAIN_TASKS_MD = "## Later\n- [ ] タグ無しのLaterタスク\n"


class ReadTasksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "work_repo"
        projects_dir = self.root / "10_projects"

        main_dir = projects_dir / "50c_202609_tasks_demo"
        main_dir.mkdir(parents=True)
        self.main_tasks_path = main_dir / "tasks.md"
        self.main_tasks_path.write_text(_MAIN_TASKS_MD, encoding="utf-8")

        no_tasks_dir = projects_dir / "50d_202609_no_tasks"
        no_tasks_dir.mkdir(parents=True)
        (no_tasks_dir / "README.md").write_text("# no tasks\n", encoding="utf-8")

        broken_dir = projects_dir / "50e_202609_broken_tasks"
        broken_dir.mkdir(parents=True)
        (broken_dir / "tasks.md").write_text(_BROKEN_TASKS_MD, encoding="utf-8")

        later_dir = projects_dir / "50f_202609_later_plain"
        later_dir.mkdir(parents=True)
        (later_dir / "tasks.md").write_text(_LATER_PLAIN_TASKS_MD, encoding="utf-8")

        self.repo = ContextRepo(self.root, _work_repo_config())

        self._today_patch = mock.patch(
            "contextflow.timeutil.today", return_value=_FIXED_TODAY
        )
        self._today_patch.start()
        self.addCleanup(self._today_patch.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_read_tasks_interprets_sections_and_tags(self) -> None:
        tasks = self.repo.read_tasks()
        demo_tasks = {t.title: t for t in tasks if t.project == "tasks_demo"}

        # !skip の行は Task として返らないため 6 件（7 行 - 1）
        self.assertEqual(len(demo_tasks), 6)
        self.assertNotIn("社内調整メモを作る", demo_tasks)

        design = demo_tasks["設計レビューの指摘を反映する"]
        self.assertEqual(design.status, TaskStatus.IN_PROGRESS)  # @doing
        self.assertEqual(design.priority, 1)  # !high
        self.assertEqual(design.deadline, _END_OF_WEEK)  # Next の既定
        self.assertFalse(design.blocked)

        estimate = demo_tasks["見積前提を確認する"]
        self.assertEqual(estimate.status, TaskStatus.OPEN)
        self.assertEqual(estimate.priority, 3)
        self.assertEqual(estimate.deadline, _FIXED_TODAY)  # @today

        ops = demo_tasks["運用手順を見直す"]
        self.assertEqual(ops.status, TaskStatus.OPEN)
        self.assertEqual(ops.deadline, _END_OF_MONTH)  # @this-month

        waiting = demo_tasks["法務レビューの回答待ち"]
        self.assertEqual(waiting.status, TaskStatus.BLOCKED)
        self.assertTrue(waiting.blocked)
        self.assertIsNone(waiting.deadline)

        cancelled = demo_tasks["旧方式の検証を進める"]
        self.assertEqual(cancelled.status, TaskStatus.CANCELED)
        self.assertIsNone(cancelled.deadline)

        done = demo_tasks["キックオフ資料を作成した"]
        self.assertEqual(done.status, TaskStatus.DONE)
        self.assertEqual(done.github_issue, 12)  # #12 がタイトルから除去されて格納される
        self.assertIsNone(done.deadline)

    def test_read_tasks_skips_project_without_tasks_md(self) -> None:
        tasks = self.repo.read_tasks()
        self.assertFalse(any(t.project == "no_tasks" for t in tasks))

    def test_read_tasks_ignores_unknown_sections_and_broken_lines(self) -> None:
        tasks = self.repo.read_tasks()
        broken_tasks = [t for t in tasks if t.project == "broken_tasks"]
        self.assertEqual(len(broken_tasks), 1)
        self.assertEqual(broken_tasks[0].title, "正常なタスク")

    def test_read_tasks_later_without_tag_has_no_deadline(self) -> None:
        tasks = self.repo.read_tasks()
        later_tasks = [t for t in tasks if t.project == "later_plain"]
        self.assertEqual(len(later_tasks), 1)
        self.assertEqual(later_tasks[0].status, TaskStatus.OPEN)
        self.assertIsNone(later_tasks[0].deadline)

    def test_read_tasks_does_not_modify_tasks_md_bytes(self) -> None:
        before = self.main_tasks_path.read_bytes()
        self.repo.read_tasks()
        after = self.main_tasks_path.read_bytes()
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# append_decision / append_change
# ---------------------------------------------------------------------------


class AppendDecisionChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "work_repo"
        self.project_dir = self.root / "10_projects" / "50c_202609_tasks_demo"
        self.project_dir.mkdir(parents=True)
        self.decisions_path = self.project_dir / "decisions.md"
        self.repo = ContextRepo(self.root, _work_repo_config())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_decision_creates_heading_and_line(self) -> None:
        path = self.repo.append_decision("tasks_demo", date(2026, 9, 23), "設計方針をAに決定")
        self.assertEqual(path, self.decisions_path)
        text = self.decisions_path.read_text(encoding="utf-8")
        self.assertIn("## 2026-09-23", text)
        self.assertIn("- 判断: 設計方針をAに決定", text)

    def test_append_change_line_format(self) -> None:
        self.repo.append_change("tasks_demo", date(2026, 9, 23), "要件が追加された")
        text = self.decisions_path.read_text(encoding="utf-8")
        self.assertIn("- 変化: 要件が追加された", text)

    def test_append_decision_does_not_duplicate_same_line(self) -> None:
        target = date(2026, 9, 23)
        self.repo.append_decision("tasks_demo", target, "重複確認用の判断")
        self.repo.append_decision("tasks_demo", target, "重複確認用の判断")

        text = self.decisions_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("- 判断: 重複確認用の判断"), 1)
        self.assertEqual(text.count("## 2026-09-23"), 1)  # 見出しも増えない

    def test_append_decision_unknown_project_returns_none_and_writes_nothing(self) -> None:
        result = self.repo.append_decision("no-such-project", date(2026, 9, 23), "書かれないはず")
        self.assertIsNone(result)
        self.assertFalse(self.decisions_path.exists())

    def test_append_decision_preserves_existing_content(self) -> None:
        self.decisions_path.write_text(
            "# decisions\n\n## 2026-09-01\n- 判断: 既存の判断\n", encoding="utf-8"
        )
        self.repo.append_decision("tasks_demo", date(2026, 9, 23), "新しい判断")

        text = self.decisions_path.read_text(encoding="utf-8")
        self.assertIn("既存の判断", text)
        self.assertIn("新しい判断", text)


# ---------------------------------------------------------------------------
# write_daily
# ---------------------------------------------------------------------------


class WriteDailyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "work_repo"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_daily_default_false_returns_none_and_creates_nothing(self) -> None:
        repo = ContextRepo(self.root, _work_repo_config())  # write_daily は既定で false
        result = repo.write_daily(date(2026, 9, 23), "本文")
        self.assertIsNone(result)
        self.assertEqual(_snapshot(self.root), set())  # ファイルが一切作られていない

    def test_write_daily_true_writes_under_daily_dir(self) -> None:
        repo = ContextRepo(
            self.root,
            _work_repo_config(write_daily=True, daily_dir="20_knowledge/90_daily_activity"),
        )
        result = repo.write_daily(date(2026, 9, 23), "本文テスト")

        expected = self.root / "20_knowledge" / "90_daily_activity" / "2026-09-23.md"
        self.assertEqual(result, expected)
        self.assertEqual(expected.read_text(encoding="utf-8"), "本文テスト")


# ---------------------------------------------------------------------------
# standalone layout
# ---------------------------------------------------------------------------


class StandaloneLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "standalone_repo"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_standalone_layout_via_config_creates_expected_dirs(self) -> None:
        repo = ContextRepo(self.root, _work_repo_config(layout=LAYOUT_STANDALONE))
        self.assertEqual(repo.layout, LAYOUT_STANDALONE)

        repo.ensure_layout()
        self.assertTrue((self.root / "projects").is_dir())
        self.assertTrue((self.root / "context" / "decisions").is_dir())
        self.assertTrue((self.root / "context" / "changes").is_dir())
        self.assertTrue((self.root / "activity" / "daily").is_dir())
        self.assertTrue((self.root / "activity" / "weekly").is_dir())
        self.assertTrue((self.root / "agent" / "schemas").is_dir())

    def test_contextrepo_without_config_behaves_as_standalone(self) -> None:
        repo = ContextRepo(self.root)  # config 省略 = 後方互換
        self.assertEqual(repo.layout, LAYOUT_STANDALONE)

        repo.ensure_layout()
        self.assertTrue((self.root / "projects").is_dir())
        self.assertTrue((self.root / "activity" / "daily").is_dir())

        # standalone の project_key は folder
        project_dir = self.root / "projects" / "demo"
        project_dir.mkdir(parents=True)
        (project_dir / "tasks.md").write_text("- [ ] 何かする\n", encoding="utf-8")

        projects = repo.projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].key, "demo")

        tasks = repo.read_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "何かする")
        self.assertEqual(tasks[0].project, "demo")


# ---------------------------------------------------------------------------
# GitHubIssues のラベル変換（ネットワークアクセス禁止。urlopen をモックする）
# ---------------------------------------------------------------------------

_ISSUES_JSON = [
    {
        "number": 101,
        "title": "todoの確認",
        "labels": [
            {"name": "status:todo"},
            {"name": "priority:high"},
            {"name": "when:today"},
            {"name": "pjt:tasks_demo"},
        ],
        "updated_at": "2026-09-20T01:00:00Z",
    },
    {
        # ラベルが文字列のみの形式でも読めることを確認する
        "number": 102,
        "title": "doing中の対応",
        "labels": ["status:doing", "priority:normal", "when:this-week"],
        "updated_at": "2026-09-20T01:00:00Z",
    },
    {
        "number": 103,
        "title": "waiting中の確認",
        "labels": [{"name": "status:waiting"}],
        "updated_at": "2026-09-20T01:00:00Z",
    },
    {
        "number": 104,
        "title": "done済み",
        "labels": [{"name": "status:done"}, {"name": "priority:low"}],
        "updated_at": "2026-09-20T01:00:00Z",
    },
    {
        "number": 105,
        "title": "cancelled済み",
        "labels": [{"name": "status:cancelled"}],
        "updated_at": "2026-09-20T01:00:00Z",
    },
    {
        "number": 106,
        "title": "this-monthの予定",
        "labels": [{"name": "when:this-month"}, {"name": "pjt:other_project"}],
        "updated_at": "2026-09-20T01:00:00Z",
    },
]


class GitHubIssuesLabelTests(unittest.TestCase):
    def test_disabled_without_token_does_not_touch_network(self) -> None:
        client = GitHubIssues(repo="owner/repo", token=None)
        self.assertFalse(client.enabled)
        with mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network access attempted")
        ):
            tasks = client.list_open()
        self.assertEqual(tasks, [])

    def test_list_open_converts_labels_without_network(self) -> None:
        client = GitHubIssues(repo="owner/repo", token="dummy-token")
        self.assertTrue(client.enabled)

        body = json.dumps(_ISSUES_JSON).encode("utf-8")
        with mock.patch("urllib.request.urlopen") as mock_urlopen, mock.patch(
            "contextflow.timeutil.today", return_value=_FIXED_TODAY
        ):
            mock_urlopen.return_value.__enter__.return_value.read.return_value = body
            tasks = client.list_open()

        by_number = {t.github_issue: t for t in tasks}
        self.assertEqual(len(by_number), len(_ISSUES_JSON))

        todo = by_number[101]
        self.assertEqual(todo.status, TaskStatus.OPEN)
        self.assertFalse(todo.blocked)
        self.assertEqual(todo.priority, 1)  # priority:high
        self.assertEqual(todo.deadline, _FIXED_TODAY)  # when:today
        self.assertEqual(todo.project, "tasks_demo")  # pjt:tasks_demo

        doing = by_number[102]
        self.assertEqual(doing.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(doing.priority, 3)  # priority:normal
        self.assertEqual(doing.deadline, _END_OF_WEEK)  # when:this-week

        waiting = by_number[103]
        self.assertEqual(waiting.status, TaskStatus.BLOCKED)
        self.assertTrue(waiting.blocked)

        done = by_number[104]
        self.assertEqual(done.status, TaskStatus.DONE)
        self.assertEqual(done.priority, 5)  # priority:low

        cancelled = by_number[105]
        self.assertEqual(cancelled.status, TaskStatus.CANCELED)

        this_month = by_number[106]
        self.assertEqual(this_month.deadline, _END_OF_MONTH)  # when:this-month
        self.assertEqual(this_month.project, "other_project")


# ---------------------------------------------------------------------------
# GitSync.commit の paths 指定（git が使える環境のみ）
# ---------------------------------------------------------------------------


def _git_available() -> bool:
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0


_GIT_AVAILABLE = _git_available()

# 環境の git config に依存せず init/commit が必ず成功するよう author/committer を明示する
_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "ContextFlow Test",
    "GIT_AUTHOR_EMAIL": "contextflow-test@example.invalid",
    "GIT_COMMITTER_NAME": "ContextFlow Test",
    "GIT_COMMITTER_EMAIL": "contextflow-test@example.invalid",
}


@unittest.skipUnless(_GIT_AVAILABLE, "git コマンドが無い環境のためスキップ")
class GitSyncCommitPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "context_repo"
        self._env_patch = mock.patch.dict("os.environ", _GIT_IDENTITY_ENV)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        self.sync = GitSync(self.root, branch="main")
        self.sync.init()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_commit_with_paths_commits_only_specified_file(self) -> None:
        file_a = self.root / "a.md"
        file_b = self.root / "b.md"
        file_a.write_text("A", encoding="utf-8")
        file_b.write_text("B", encoding="utf-8")

        status_before = self.sync.status()
        self.assertEqual(status_before.dirty, 2)  # 2ファイルとも未コミット

        message = self.sync.commit("add only a", [file_a])
        self.assertIn("commit した", message)

        status_after = self.sync.status()
        self.assertEqual(status_after.dirty, 1)  # b.md だけが未コミットのまま残る

    def test_commit_same_content_again_reports_no_changes(self) -> None:
        file_a = self.root / "a.md"
        file_a.write_text("A", encoding="utf-8")

        first = self.sync.commit("add a", [file_a])
        self.assertIn("commit した", first)

        second = self.sync.commit("add a again", [file_a])  # 内容が変わっていない
        self.assertEqual(second, "変更なし")

    def test_commit_with_path_outside_repo_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as outside_dir:
            outside_file = Path(outside_dir) / "outside.md"
            outside_file.write_text("outside", encoding="utf-8")

            try:
                message = self.sync.commit("should be no-op", [outside_file])
            except Exception as exc:  # pragma: no cover - 例外が出たらテスト失敗にする
                self.fail(f"commit() が例外を投げた: {exc}")
            self.assertIn("対象ファイルが無い", message)

            # リポジトリ外のファイルが誤って対象になっていないこと
            status = self.sync.status()
            self.assertEqual(status.dirty, 0)


if __name__ == "__main__":
    unittest.main()
