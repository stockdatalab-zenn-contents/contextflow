"""sources/git_sync.py（GitSync / GitStatus / open_git_sync）の単体テスト。

標準ライブラリの unittest のみを使用する。tempfile.TemporaryDirectory の中だけで
実施し、プロジェクト内（特に app/data）には一切触れない。また push は実行しない
（実際に origin へ向けて `git push` するテストはネットワークアクセスになるため書かない。
remote 未設定のまま push() を呼ぶケースだけを確認する）。

git コマンドが無い環境でも `status()` は例外を投げない前提のテストはそのまま実行し、
init/commit など git が無いと成立しないテストは unittest.skipUnless でスキップする。
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.conftest_path import add_source_path

add_source_path()

from contextflow.config import AppConfig  # noqa: E402
from contextflow.sources.git_sync import GitStatus, GitSync, open_git_sync  # noqa: E402


def _git_available() -> bool:
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0


_GIT_AVAILABLE = _git_available()

# init/commit が誰の環境でも成功するよう、グローバルの git config に依存せず
# author/committer を環境変数で明示する（git はこれを config より優先して見る）。
_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "ContextFlow Test",
    "GIT_AUTHOR_EMAIL": "contextflow-test@example.invalid",
    "GIT_COMMITTER_NAME": "ContextFlow Test",
    "GIT_COMMITTER_EMAIL": "contextflow-test@example.invalid",
}


class GitSyncStatusNeverRaisesTests(unittest.TestCase):
    """git の有無・初期化状態によらず status() は例外を投げないこと。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "context_repo"

    def tearDown(self):
        self._tmp.cleanup()

    def test_status_does_not_raise_on_missing_directory(self):
        sync = GitSync(self.root)
        try:
            status = sync.status()
        except Exception as exc:  # pragma: no cover - 例外が出たらテスト失敗にする
            self.fail(f"status() が例外を投げた: {exc}")
        self.assertIsInstance(status, GitStatus)

    def test_uninitialized_is_not_repo(self):
        sync = GitSync(self.root)
        status = sync.status()
        self.assertFalse(status.is_repo)

    def test_status_does_not_raise_on_empty_existing_directory(self):
        self.root.mkdir(parents=True)
        sync = GitSync(self.root)
        try:
            status = sync.status()
        except Exception as exc:  # pragma: no cover
            self.fail(f"status() が例外を投げた: {exc}")
        self.assertFalse(status.is_repo)


@unittest.skipUnless(_GIT_AVAILABLE, "git コマンドが無い環境のためスキップ")
class GitSyncInitCommitTests(unittest.TestCase):
    """git init / commit / set_remote / push（remote未設定）の一連の挙動。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "context_repo"
        self._env_patch = mock.patch.dict("os.environ", _GIT_IDENTITY_ENV)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_init_creates_repo_with_given_branch(self):
        sync = GitSync(self.root, branch="main")
        message = sync.init()
        self.assertTrue(message)

        status = sync.status()
        self.assertTrue(status.is_repo)
        self.assertEqual(status.branch, "main")

    def test_init_twice_is_safe(self):
        sync = GitSync(self.root, branch="main")
        sync.init()
        second_message = sync.init()
        self.assertIn("既に", second_message)

        status = sync.status()
        self.assertTrue(status.is_repo)
        self.assertEqual(status.branch, "main")

    def test_commit_then_no_changes_is_reported_without_empty_commit(self):
        sync = GitSync(self.root, branch="main")
        sync.init()

        (self.root / "note.md").write_text("hello", encoding="utf-8")
        first = sync.commit("x")
        self.assertIn("commit した", first)

        second = sync.commit("y")
        self.assertEqual(second, "変更なし")

    def test_push_without_remote_does_not_raise_and_says_so(self):
        sync = GitSync(self.root, branch="main")
        sync.init()

        message = sync.push()
        self.assertIn("remote が未設定", message)

    def test_set_remote_is_reflected_in_status(self):
        sync = GitSync(self.root, branch="main")
        sync.init()

        sync.set_remote("https://example.invalid/x.git")
        status = sync.status()
        self.assertEqual(status.remote, "https://example.invalid/x.git")


class OpenGitSyncTests(unittest.TestCase):
    """open_git_sync: paths.context_repo と [github] の値を読むこと。

    実際の app/data には触れないよう、AppConfig の data を書き換えて
    一時ディレクトリを指す絶対パスを渡す。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_context_repo_path_and_github_settings(self):
        context_repo = self.tmp_root / "context_repo"
        config = AppConfig(
            data={
                "paths": {"context_repo": str(context_repo)},
                "github": {"remote": "https://example.invalid/x.git", "branch": "develop"},
            },
            root=self.tmp_root,
        )

        sync = open_git_sync(config)

        self.assertEqual(sync.root, context_repo)
        self.assertEqual(sync.remote, "https://example.invalid/x.git")
        self.assertEqual(sync.branch, "develop")

    def test_defaults_when_github_section_is_empty(self):
        context_repo = self.tmp_root / "context_repo"
        config = AppConfig(
            data={"paths": {"context_repo": str(context_repo)}, "github": {}},
            root=self.tmp_root,
        )

        sync = open_git_sync(config)

        self.assertEqual(sync.root, context_repo)
        self.assertEqual(sync.remote, "")
        self.assertEqual(sync.branch, "main")


if __name__ == "__main__":
    unittest.main()
