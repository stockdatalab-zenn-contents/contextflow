"""sources/git_sync.py

長期コンテキストのローカル Markdown（``context_repo``）を git で残す薄いラッパ。

- リポジトリが未作成でも動く（``status()`` は例外を投げず状態だけ返す）。
- ``remote`` が未設定なら push はせず、その旨を戻り値で伝える。
- ネットワークアクセスは ``push`` / ``sync`` 実行時のみ発生する。

標準ライブラリのみを使用する。git コマンドは ``subprocess`` 経由で呼ぶ（``shell=True`` は使わない）。
git 自体が未インストールの環境でも例外を投げず、日本語メッセージで状況を伝える。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from contextflow.config import AppConfig

# .gitignore の雛形（無いときだけ作る）
_GITIGNORE_TEMPLATE = ".DS_Store\nThumbs.db\n*.db\n"

_GIT_NOT_FOUND_MESSAGE = "git コマンドが見つからない（未インストールの可能性）"
_NOT_A_REPO_MESSAGE = "git リポジトリが未作成（先に init() を実行）"


@dataclass
class GitStatus:
    """git の現在状態を人間が読める形でまとめたもの。"""

    available: bool  # git コマンドが使えるか
    is_repo: bool  # root が git リポジトリか
    branch: str  # 現在のブランチ（不明なら空文字）
    remote: str  # 設定済みの origin URL（無ければ空文字）
    dirty: int  # 未コミットの変更ファイル数
    message: str  # 人間向けの状況説明（日本語1行）


class GitSync:
    """長期コンテキストのローカル Markdown を git で残す。

    リポジトリ未作成でも動く。remote が未設定なら push はせず、その旨を返すだけ。
    """

    def __init__(self, root: Path, *, remote: str = "", branch: str = "main") -> None:
        self.root = Path(root)
        self.remote = remote
        self.branch = branch or "main"

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def status(self) -> GitStatus:
        """git / リポジトリの現在状態を返す。git が無い・未初期化でも例外にしない。"""
        if not self._git_available():
            return GitStatus(False, False, "", "", 0, _GIT_NOT_FOUND_MESSAGE)

        if not self.root.exists():
            return GitStatus(
                True, False, "", "", 0, "context_repo フォルダがまだ無い（先に作成が必要）"
            )

        if not self._is_repo():
            return GitStatus(
                True, False, "", "", 0, "git リポジトリが未作成（init() で作成できる）"
            )

        branch = self._current_branch()
        remote = self._remote_url()
        dirty = self._dirty_count()
        remote_note = f"remote={remote}" if remote else "remote 未設定"
        message = f"リポジトリあり（branch={branch or '不明'}, 未コミット={dirty}件, {remote_note}）"
        return GitStatus(True, True, branch, remote, dirty, message)

    def init(self) -> str:
        """git init（既にリポジトリなら何もしない）+ 初回 commit。"""
        if not self._git_available():
            return _GIT_NOT_FOUND_MESSAGE

        self.root.mkdir(parents=True, exist_ok=True)
        if self._is_repo():
            return "既に git リポジトリ（init は不要）"

        result = self._run(["init", "-b", self.branch])
        if result is None:
            return _GIT_NOT_FOUND_MESSAGE
        if result.returncode != 0:
            # 古い git は `-b` 未対応 → `git init` 単体 + `checkout -b` へフォールバック
            result = self._run(["init"])
            if result is None:
                return _GIT_NOT_FOUND_MESSAGE
            if result.returncode != 0:
                return f"git init に失敗した: {self._error_text(result)}"
            self._run(["checkout", "-b", self.branch])

        self._write_gitignore_if_absent()
        commit_message = self.commit("chore: context repo を初期化")
        return f"git リポジトリを作成した / {commit_message}"

    def commit(self, message: str, paths: Optional[Sequence[Any]] = None) -> str:
        """git add して commit。変更が無ければ「変更なし」を返す。

        paths を指定すると、そのファイルだけを対象にする。
        連携先が普段使いの仕事管理リポジトリである場合、`add -A` では
        人が編集中の無関係なファイルまで巻き込んでしまうため、
        contextflow が書いたファイルだけを渡す想定。
        """
        if not self._git_available():
            return _GIT_NOT_FOUND_MESSAGE
        if not self.root.exists() or not self._is_repo():
            return _NOT_A_REPO_MESSAGE

        targets = self._relative_targets(paths)
        if paths is not None and not targets:
            return "対象ファイルが無いため commit しない"

        add_args = ["add", "--"] + targets if targets else ["add", "-A"]
        add_result = self._run(add_args)
        if add_result is None:
            return _GIT_NOT_FOUND_MESSAGE
        if add_result.returncode != 0:
            return f"git add に失敗した: {self._error_text(add_result)}"

        if self._staged_count(targets) == 0:
            return "変更なし"

        commit_args = ["commit", "-m", message]
        if targets:
            commit_args += ["--"] + targets
        commit_result = self._run(commit_args)
        if commit_result is None:
            return _GIT_NOT_FOUND_MESSAGE
        if commit_result.returncode != 0:
            stderr = commit_result.stderr or ""
            if "user.name" in stderr or "user.email" in stderr:
                return "git config user.name と user.email を設定してください"
            return f"commit に失敗した: {self._error_text(commit_result)}"
        return f"commit した: {message}"

    def _relative_targets(self, paths: Optional[Sequence[Any]]) -> list[str]:
        """commit 対象をリポジトリからの相対パス文字列へ揃える。"""
        if not paths:
            return []
        targets: list[str] = []
        for item in paths:
            if item is None:
                continue
            candidate = Path(item)
            try:
                relative = candidate.resolve().relative_to(self.root.resolve())
            except (ValueError, OSError):
                continue  # リポジトリ外のパスは対象にしない
            targets.append(relative.as_posix())
        return sorted(set(targets))

    def _staged_count(self, targets: Sequence[str]) -> int:
        """staged な変更の件数。targets 指定時はその範囲だけ数える。"""
        args = ["diff", "--cached", "--name-only"]
        if targets:
            args += ["--"] + list(targets)
        result = self._run(args)
        if result is None or result.returncode != 0:
            return 0
        return len([line for line in (result.stdout or "").splitlines() if line.strip()])

    def set_remote(self, url: str) -> str:
        """origin を追加または更新する。"""
        if not self._git_available():
            return _GIT_NOT_FOUND_MESSAGE
        if not self.root.exists() or not self._is_repo():
            return _NOT_A_REPO_MESSAGE

        self.remote = url
        if self._remote_url():
            result = self._run(["remote", "set-url", "origin", url])
            verb = "更新"
        else:
            result = self._run(["remote", "add", "origin", url])
            verb = "追加"

        if result is None:
            return _GIT_NOT_FOUND_MESSAGE
        if result.returncode != 0:
            return f"remote の設定に失敗した: {self._error_text(result)}"
        return f"origin を{verb}した: {url}"

    def push(self) -> str:
        """git push -u origin <branch>。remote 未設定なら push せずその旨を返す。"""
        if not self._git_available():
            return _GIT_NOT_FOUND_MESSAGE
        if not self.root.exists() or not self._is_repo():
            return _NOT_A_REPO_MESSAGE

        remote = self._remote_url()
        if not remote and self.remote:
            # config の github.remote だけ埋めれば push できるよう、origin をここで設定する
            self.set_remote(self.remote)
            remote = self._remote_url()
        if not remote:
            return "remote が未設定のため push しない（config の github.remote を設定する）"

        branch = self._current_branch() or self.branch
        result = self._run(["push", "-u", "origin", branch])
        if result is None:
            return _GIT_NOT_FOUND_MESSAGE
        if result.returncode != 0:
            return f"push に失敗した: {self._error_text(result)}"
        return f"push した: origin/{branch}"

    def sync(self, message: str, paths: Optional[Sequence[Any]] = None) -> str:
        """commit してから push（remote があれば）。

        paths を渡すと、そのファイルだけを commit する。
        """
        commit_message = self.commit(message, paths)
        push_message = self.push()
        return f"{commit_message} / {push_message}"

    # ------------------------------------------------------------------
    # 内部ヘルパ
    # ------------------------------------------------------------------

    def _git_available(self) -> bool:
        """git コマンドそのものが使えるか（root の状態に依存しない）。"""
        try:
            result = subprocess.run(
                ["git", "--version"], capture_output=True, text=True, encoding="utf-8"
            )
        except FileNotFoundError:
            return False
        return result.returncode == 0

    def _run(self, args: list[str]) -> subprocess.CompletedProcess | None:
        """root を cwd として git コマンドを実行する。git が無ければ None。"""
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except FileNotFoundError:
            return None

    def _is_repo(self) -> bool:
        result = self._run(["rev-parse", "--is-inside-work-tree"])
        return result is not None and result.returncode == 0 and result.stdout.strip() == "true"

    def _current_branch(self) -> str:
        result = self._run(["rev-parse", "--abbrev-ref", "HEAD"])
        if result is None or result.returncode != 0:
            return ""
        branch = result.stdout.strip()
        return "" if branch in ("", "HEAD") else branch  # HEAD = detached 状態

    def _remote_url(self) -> str:
        result = self._run(["remote", "get-url", "origin"])
        if result is None or result.returncode != 0:
            return ""
        return result.stdout.strip()

    def _dirty_count(self) -> int:
        result = self._run(["status", "--porcelain"])
        if result is None or result.returncode != 0:
            return 0
        return len([line for line in result.stdout.splitlines() if line.strip()])

    def _write_gitignore_if_absent(self) -> None:
        path = self.root / ".gitignore"
        if path.exists():
            return
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_GITIGNORE_TEMPLATE)

    @staticmethod
    def _error_text(result: subprocess.CompletedProcess | None) -> str:
        if result is None:
            return "git コマンドが見つからない"
        text = (result.stderr or result.stdout or "").strip()
        return text if text else "不明なエラー"


def open_git_sync(config: AppConfig) -> GitSync:
    """paths.context_repo と [github] 設定から GitSync を組み立てる。"""
    root = config.path("context_repo")
    remote = str(config.get("github.remote", "") or "")
    branch = str(config.get("github.branch", "main") or "main")
    return GitSync(root, remote=remote, branch=branch)
