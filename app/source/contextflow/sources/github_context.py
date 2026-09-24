"""sources/github_context.py

長期コンテキストを扱う2つのクラスをまとめる。

- ``ContextRepo``  : ローカル Markdown（`paths.context_repo` 配下）が長期記憶の実体。
                     生ログ（raw_events・window_title 等）は絶対に置かない。
- ``GitHubIssues`` : GitHub Issue を「これからやること（行動）」として扱う薄い REST クライアント。
                     Markdown（記憶）と Issue（行動）は役割を分ける。

前提となる運用ルール（仕事管理リポジトリ側の絶対ルール）:

- 各案件の ``tasks.md`` が **タスクの正本**。contextflow からは **読み取りのみ**で、
  絶対に書き換えない。
- GitHub Issue は ``tasks.md`` の写し。起票・更新・クローズは承認制の同期処理が担うため、
  contextflow は **Issue を読むだけ**（書き込み系メソッドは持たない）。
- 既存ファイルを上書きしない。追記のみ。人が書いた内容を推測で変更しない。
- リポジトリは「具体的な実施日時は管理しない」方針。日次の時間集計は既定で書き出さない。

標準ライブラリのみを使用する。GitHub API への通信は ``urllib.request`` のみで行う。
"""

from __future__ import annotations

import calendar
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from contextflow import timeutil
from contextflow.config import AppConfig
from contextflow.contracts.models import Task, TaskStatus
from contextflow.contracts.serde import parse_datetime
from contextflow.storage.db import Database
from contextflow.storage.repositories import TaskRepository

# ---------------------------------------------------------------------------
# レイアウト
# ---------------------------------------------------------------------------

# 既存の仕事管理リポジトリ（10_projects/... の構成）に合わせる。フォルダを勝手に作らない
LAYOUT_WORK_REPO = "work_repo"
# contextflow 専用の構成（projects/ context/ activity/ agent/）を自分で作る
LAYOUT_STANDALONE = "standalone"


@dataclass
class _RepoSettings:
    """config.toml の `[context_repo]` を、既定値込みで保持する内部設定。"""

    layout: str = LAYOUT_STANDALONE
    projects_dir: str = "10_projects"
    tasks_file: str = "tasks.md"
    decisions_file: str = "decisions.md"
    project_readme: str = "README.md"
    constraints_file: str = ""
    project_key: str = "suffix"
    write_daily: bool = False
    daily_dir: str = "20_knowledge/90_daily_activity"


def _build_settings(config: Optional[AppConfig]) -> _RepoSettings:
    """AppConfig から `[context_repo]` を読む。

    config が無い場合は contextflow 単独動作とみなし、従来どおりの
    ``standalone`` 構成（projects/ context/ activity/）を既定にする。
    config があるときの既定は ``work_repo``（config.toml の既定値と同じ）。
    """
    if config is None:
        return _standalone_settings()

    layout = str(config.get("context_repo.layout", LAYOUT_WORK_REPO) or LAYOUT_WORK_REPO)
    if layout == LAYOUT_STANDALONE:
        return _standalone_settings()

    return _RepoSettings(
        layout=LAYOUT_WORK_REPO,
        projects_dir=str(config.get("context_repo.projects_dir", "10_projects") or "10_projects"),
        tasks_file=str(config.get("context_repo.tasks_file", "tasks.md") or "tasks.md"),
        decisions_file=str(
            config.get("context_repo.decisions_file", "decisions.md") or "decisions.md"
        ),
        project_readme=str(config.get("context_repo.project_readme", "README.md") or "README.md"),
        constraints_file=str(config.get("context_repo.constraints_file", "") or ""),
        project_key=str(config.get("context_repo.project_key", "suffix") or "suffix"),
        write_daily=bool(config.get("context_repo.write_daily", False)),
        daily_dir=str(
            config.get("context_repo.daily_dir", "20_knowledge/90_daily_activity")
            or "20_knowledge/90_daily_activity"
        ),
    )


def _standalone_settings() -> _RepoSettings:
    """contextflow 専用構成の固定値。案件キーはフォルダ名（従来互換）。"""
    return _RepoSettings(
        layout=LAYOUT_STANDALONE,
        projects_dir="projects",
        tasks_file="tasks.md",
        decisions_file="decisions.md",
        project_readme="README.md",
        constraints_file="",
        project_key="folder",
        write_daily=True,
        daily_dir="activity/daily",
    )


# ---------------------------------------------------------------------------
# ContextRepo 用の雛形テキスト（standalone 構成で、無いときだけ作る）
# ---------------------------------------------------------------------------

_CONSTRAINTS_TEMPLATE = (
    "# Constraints\n"
    "\n"
    "# ここに制約条件を \"- \" 始まりの箇条書きで書く。\n"
    "# 例: - 稼働時間は平日 9:00-18:00 のみ\n"
)

_AGENT_README_TEMPLATE = (
    "# agent/\n"
    "\n"
    "Decision Engine（Jev風）が使う設定の置き場。\n"
    "\n"
    "## questions.toml について\n"
    "\n"
    "質問セットの実体は `app/source/config/questions.toml` にある。\n"
    "ここには複製を置かない（設定の正は1箇所に保つ）。\n"
    "\n"
    "## schemas/\n"
    "\n"
    "Decision Engine の出力スキーマ（JSON Schema 等）を置く場所。\n"
)


# ---------------------------------------------------------------------------
# 案件フォルダ
# ---------------------------------------------------------------------------

# 案件フォルダ名の期間部分。yyyymm / yyyymmdd。サンプル案件のプレースホルダも許す
_PERIOD_RE = re.compile(r"^(?:\d{6}|\d{8}|yyyymm|yyyymmdd)$", re.IGNORECASE)

# 案件走査から外すフォルダ（旧資材は原則参照しない）
_EXCLUDED_FOLDERS = {"99_old"}


@dataclass
class ProjectFolder:
    """`10_projects` 配下の案件フォルダ 1件。

    フォルダ名は `<prefix>_<yyyymm>_<suffix>` 形式
    （例 `50c_202609_sample_project` → prefix=`50c` / period=`202609` / suffix=`sample_project`）。
    区切りが足りない名前でも落とさず、取れた範囲だけ埋める。
    """

    path: Path
    folder: str
    prefix: str
    period: str
    suffix: str
    key_kind: str = "suffix"  # config の context_repo.project_key

    @property
    def key(self) -> str:
        """案件キー。`pjt:` ラベルと同じ値（既定は suffix）。"""
        if self.key_kind == "folder":
            return self.folder
        if self.key_kind == "prefix":
            return self.prefix or self.folder
        return self.suffix or self.folder


def parse_project_folder(path: Path, key_kind: str = "suffix") -> ProjectFolder:
    """フォルダ名を `<prefix>_<period>_<suffix>` として分解する。"""
    folder = path.name
    parts = folder.split("_")
    prefix = parts[0]
    period = ""
    suffix = ""
    if len(parts) >= 3 and _PERIOD_RE.match(parts[1]):
        period = parts[1]
        suffix = "_".join(parts[2:])
    elif len(parts) >= 2:
        # 期間が無い（または形式が違う）名前。残り全部を suffix にする
        suffix = "_".join(parts[1:])
    return ProjectFolder(
        path=path,
        folder=folder,
        prefix=prefix,
        period=period,
        suffix=suffix,
        key_kind=key_kind,
    )


# ---------------------------------------------------------------------------
# tasks.md の記法（仕事管理リポジトリの契約）
# ---------------------------------------------------------------------------

# 認識するセクションは次の5つのみ。見出しは H2（`## Next` 等）
_SECTION_STATUS: dict[str, tuple[TaskStatus, bool]] = {
    "next": (TaskStatus.OPEN, False),
    "later": (TaskStatus.OPEN, False),
    "waiting": (TaskStatus.BLOCKED, True),  # 待ち＝ブロック扱い
    "done": (TaskStatus.DONE, False),
    "cancelled": (TaskStatus.CANCELED, False),
    "canceled": (TaskStatus.CANCELED, False),  # 綴り違いも受ける
}

# 認識するタグ（正本は仕事管理リポジトリの app/source/issue_sync/tags.py）
_KNOWN_TAGS = ("@doing", "@today", "@this-month", "!high", "!low", "!skip")

_HEADING_RE = re.compile(r"^(#+)\s*(.+?)\s*$")
# チェックボックス行: '- [ ] タイトル' / '- [x] タイトル'
_CHECKBOX_RE = re.compile(r"^-\s*\[([ xX])\]\s*(.+)$")
# 行内タグ: '@doing' '!high' など
_TAG_RE = re.compile(r"(?:^|\s)([@!][A-Za-z0-9_-]+)")
# Issue 番号: ' #12'（reconcile が追記する）
_ISSUE_RE = re.compile(r"(?:^|\s)#(\d+)\b")
# 旧 contextflow 形式の行末 '(#12)'
_ISSUE_SUFFIX_RE = re.compile(r"\s*\(#(\d+)\)\s*$")


def _strip_tags(text: str) -> str:
    """タイトルから認識済みタグと `#番号` を取り除く。"""
    out = text
    for tag in _KNOWN_TAGS:
        out = re.sub(rf"(?:^|\s){re.escape(tag)}(?=\s|$)", " ", out, flags=re.IGNORECASE)
    out = _ISSUE_RE.sub(" ", out)
    return re.sub(r"\s+", " ", out).strip()


def _end_of_week(today: date) -> date:
    """今週末（日曜）。weekday() は月=0 … 日=6。"""
    return today + timedelta(days=6 - today.weekday())


def _end_of_month(today: date) -> date:
    """当月末。"""
    return today.replace(day=calendar.monthrange(today.year, today.month)[1])


def _parse_task_line(raw_line: str, project: str, section: str, today: date) -> Optional[Task]:
    """`- [ ] タイトル @tag !tag #番号` を Task へ変換する。

    deadline は **Issue の `when:` ラベル相当を contextflow の期限欄へ写した近似値**であり、
    実際の締切ではない（リポジトリは具体的な実施日時を管理しない方針のため）。
    """
    match = _CHECKBOX_RE.match(raw_line.strip())
    if not match:
        return None

    body = match.group(2).strip()
    tags = {tag.lower() for tag in _TAG_RE.findall(body)}

    if "!skip" in tags:
        return None  # 同期対象外。Task として返さない

    issue_numbers = _ISSUE_RE.findall(body)
    github_issue = int(issue_numbers[-1]) if issue_numbers else None

    title = _strip_tags(body)
    if not title:
        return None

    status, blocked = _SECTION_STATUS[section]
    # @doing は Next / Later のときだけ「着手中」にする
    if "@doing" in tags and section in ("next", "later"):
        status = TaskStatus.IN_PROGRESS

    priority = 3  # 優先度指定なし = priority:normal 相当
    if "!high" in tags:
        priority = 1
    elif "!low" in tags:
        priority = 5

    # 期限の目安（近似値）。@today / @this-month があればそれを優先する
    deadline: Optional[date] = None
    if "@today" in tags:
        deadline = today
    elif "@this-month" in tags:
        deadline = _end_of_month(today)
    elif section == "next":
        deadline = _end_of_week(today)  # Next の既定は when:this-week 相当

    return Task(
        title=title,
        project=project,
        status=status,
        priority=priority,
        deadline=deadline,
        github_issue=github_issue,
        blocked=blocked,
    )


def parse_tasks_markdown(text: str, project: str, today: Optional[date] = None) -> list[Task]:
    """tasks.md 本文を Task のリストへ変換する（5セクションのみ認識）。

    パースできない行は無視する（例外にしない）。
    """
    target_day = today or timeutil.today()
    tasks: list[Task] = []
    section: Optional[str] = None

    for raw_line in text.splitlines():
        heading = _HEADING_RE.match(raw_line.strip())
        if heading:
            level, name = heading.group(1), heading.group(2).lower()
            # 認識する H2 セクション以外に入ったら、対象外として読み飛ばす
            section = name if (len(level) == 2 and name in _SECTION_STATUS) else None
            continue
        if section is None:
            continue
        task = _parse_task_line(raw_line, project, section, target_day)
        if task is not None:
            tasks.append(task)
    return tasks


def _parse_tasks_legacy(text: str, project: str) -> list[Task]:
    """旧 contextflow 形式（セクション無しのチェックボックス）を読む。

    standalone 構成の後方互換のためだけに使う。
    - '- [x] タイトル' -> DONE / それ以外は OPEN
    - 行末の '(#12)' -> github_issue=12
    - '!' で始まるタイトル -> blocked=True / status=BLOCKED
    """
    tasks: list[Task] = []
    for raw_line in text.splitlines():
        match = _CHECKBOX_RE.match(raw_line.strip())
        if not match:
            continue
        checked = match.group(1).lower() == "x"
        title = match.group(2).strip()

        github_issue: Optional[int] = None
        issue_match = _ISSUE_SUFFIX_RE.search(title)
        if issue_match:
            github_issue = int(issue_match.group(1))
            title = _ISSUE_SUFFIX_RE.sub("", title).strip()

        status = TaskStatus.DONE if checked else TaskStatus.OPEN
        blocked = False
        if title.startswith("!"):
            title = title[1:].strip()
            blocked = True
            status = TaskStatus.BLOCKED

        tasks.append(
            Task(
                title=title,
                project=project,
                status=status,
                github_issue=github_issue,
                blocked=blocked,
            )
        )
    return tasks


# ---------------------------------------------------------------------------
# ContextRepo
# ---------------------------------------------------------------------------


class ContextRepo:
    """仕事管理リポジトリ（ローカル Markdown）の読み書き。

    `root` は config.toml の `paths.context_repo`。
    `context_repo.layout` により、既存リポジトリに合わせる ``work_repo`` と、
    contextflow 専用構成の ``standalone`` を切り替える。

    書き込みは「追記のみ」。`tasks.md` と人が書いた既存ファイルは絶対に変更しない。
    """

    def __init__(self, root: Path, config: Optional[AppConfig] = None) -> None:
        self.root = Path(root)
        self._settings = _build_settings(config)

    @property
    def layout(self) -> str:
        return self._settings.layout

    @property
    def _is_standalone(self) -> bool:
        return self._settings.layout == LAYOUT_STANDALONE

    @property
    def projects_dir(self) -> Path:
        return self.root / self._settings.projects_dir

    # ------------------------------------------------------------------
    # レイアウト
    # ------------------------------------------------------------------

    def ensure_layout(self) -> None:
        """構成を用意する。

        work_repo: **ディレクトリを新規作成しない**。`10_projects` の有無を確認するだけで、
                   無ければ何もしない（例外にしない）。既存リポジトリの構成を汚さないことが最優先。
        standalone: contextflow 専用の構成を作る。雛形ファイルは既存なら上書きしない。
        """
        if not self._is_standalone:
            # 存在確認のみ。副作用を起こさない
            self.projects_dir.is_dir()
            return

        (self.root / "projects").mkdir(parents=True, exist_ok=True)
        (self.root / "context" / "decisions").mkdir(parents=True, exist_ok=True)
        (self.root / "context" / "changes").mkdir(parents=True, exist_ok=True)
        (self.root / "activity" / "daily").mkdir(parents=True, exist_ok=True)
        (self.root / "activity" / "weekly").mkdir(parents=True, exist_ok=True)
        (self.root / "agent" / "schemas").mkdir(parents=True, exist_ok=True)

        self._write_template_if_absent(
            self.root / "context" / "constraints.md", _CONSTRAINTS_TEMPLATE
        )
        self._write_template_if_absent(
            self.root / "agent" / "README.md", _AGENT_README_TEMPLATE
        )

    @staticmethod
    def _write_template_if_absent(path: Path, template: str) -> None:
        if path.exists():
            return
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(template)

    # ------------------------------------------------------------------
    # 案件
    # ------------------------------------------------------------------

    def projects(self) -> list[ProjectFolder]:
        """案件フォルダの一覧。`99_old` と `.` 始まりは除外する。"""
        base = self.projects_dir
        if not base.is_dir():
            return []
        found: list[ProjectFolder] = []
        for path in sorted(base.iterdir(), key=lambda p: p.name):
            if not path.is_dir():
                continue
            if path.name.startswith(".") or path.name in _EXCLUDED_FOLDERS:
                continue
            found.append(parse_project_folder(path, self._settings.project_key))
        return found

    def find_project(self, key: str) -> Optional[ProjectFolder]:
        """案件キー・フォルダ名・suffix・prefix のいずれかで案件を探す。"""
        name = (key or "").strip()
        if not name:
            return None
        projects = self.projects()
        for attr in ("key", "folder", "suffix", "prefix"):
            for project in projects:
                if getattr(project, attr) == name:
                    return project
        return None

    # ------------------------------------------------------------------
    # 読み込み
    # ------------------------------------------------------------------

    def read_tasks(self) -> list[Task]:
        """各案件の `tasks.md` を Task へ変換する（読み取りのみ）。

        `tasks.md` は Issue 同期の正本。ここから書き換えることは絶対にしない。
        `tasks.md` が無い案件は飛ばす。
        """
        today = timeutil.today()
        tasks: list[Task] = []
        for project in self.projects():
            path = project.path / self._settings.tasks_file
            text = _read_text(path)
            if text is None:
                continue
            found = parse_tasks_markdown(text, project.key, today)
            if not found and self._is_standalone:
                # standalone 構成では旧形式（セクション無し）も読めるようにする
                found = _parse_tasks_legacy(text, project.key)
            tasks.extend(found)
        return tasks

    def read_constraints(self) -> list[str]:
        """制約条件の箇条書き（'- ' 始まり）を返す。無ければ空。

        work_repo: `context_repo.constraints_file` が空なら読まない。
        standalone: 従来どおり `context/constraints.md`。
        """
        if self._is_standalone:
            path = self.root / "context" / "constraints.md"
        else:
            if not self._settings.constraints_file:
                return []
            path = self.root / self._settings.constraints_file

        text = _read_text(path)
        if text is None:
            return []
        constraints: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("- "):
                constraints.append(line[2:].strip())
        return constraints

    def read_project_state(self, project: str) -> str:
        """案件の README.md（現在地）の本文を返す。無ければ空文字。"""
        folder = self.find_project(project)
        if folder is None:
            return ""
        text = _read_text(folder.path / self._settings.project_readme)
        if text is None and self._is_standalone:
            # 旧 contextflow 構成では state.md を使っていた
            text = _read_text(folder.path / "state.md")
        return text or ""

    # ------------------------------------------------------------------
    # 追記（判断・変化）
    # ------------------------------------------------------------------

    def append_decision(self, project: str, target: date, line: str) -> Optional[Path]:
        """案件の `decisions.md` へ `- 判断: <内容>` を追記する。

        案件が特定できなければ何も書かずに None を返す。
        """
        return self._append_entry(project, target, line, "判断", "decisions")

    def append_change(self, project: str, target: date, line: str) -> Optional[Path]:
        """案件の `decisions.md` へ `- 変化: <内容>` を追記する。

        案件が特定できなければ何も書かずに None を返す。
        """
        return self._append_entry(project, target, line, "変化", "changes")

    def append_month(self, kind: str, target: date, line: str) -> Optional[Path]:
        """後方互換。kind は 'decisions' か 'changes'。

        内部で append_decision / append_change を呼ぶ。案件を指定できない呼び出しのため、
        work_repo 構成では書き込み先を決められず None を返す。
        """
        if kind == "decisions":
            return self.append_decision("", target, line)
        if kind == "changes":
            return self.append_change("", target, line)
        raise ValueError(f"kind は 'decisions' か 'changes' のみ有効: {kind}")

    def _append_entry(
        self, project: str, target: date, line: str, label: str, kind: str
    ) -> Optional[Path]:
        content = (line or "").strip()
        if not content:
            return None

        # 既に '- ' で始まる完成した箇条書きはそのまま使う（append_month からの後方互換経路）
        entry = content if content.startswith("- ") else f"- {label}: {content}"

        path = self._entry_path(project, kind, target)
        if path is None:
            return None
        return _append_under_heading(path, target, entry)

    def _entry_path(self, project: str, kind: str, target: date) -> Optional[Path]:
        """判断・変化の追記先。決められなければ None。"""
        if self._is_standalone:
            # 従来どおり context/<kind>/YYYY-MM.md
            return self.root / "context" / kind / f"{target.strftime('%Y-%m')}.md"
        folder = self.find_project(project)
        if folder is None:
            return None
        return folder.path / self._settings.decisions_file

    # ------------------------------------------------------------------
    # 日次
    # ------------------------------------------------------------------

    def write_daily(self, target: date, body: str) -> Optional[Path]:
        """日次サマリを書き出す。

        work_repo: `context_repo.write_daily` が false（既定）なら **何も書かず None**。
                   リポジトリの「具体的な実施日時は管理しない」方針に反するため。
                   true のときは `daily_dir` 配下へ書き出す。ここは contextflow 自身の
                   生成物なので、同じ日に再実行したら最新の内容で置き換える
                   （人が書いたファイルは `daily_dir` の外にあり、触らない）。
        standalone: 従来どおり `activity/daily/YYYY-MM-DD.md` へ書き出す。
        """
        if self._is_standalone:
            path = self.root / "activity" / "daily" / f"{target.isoformat()}.md"
        else:
            if not self._settings.write_daily:
                return None
            path = self.root / self._settings.daily_dir / f"{target.isoformat()}.md"

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        return path


# ---------------------------------------------------------------------------
# ファイル入出力の共通処理
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> Optional[str]:
    """テキストを読む。無い・読めない場合は None（例外にしない）。"""
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _append_under_heading(path: Path, target: date, entry: str) -> Path:
    """`## YYYY-MM-DD` の見出しの下へ entry を追記する。

    同じ行が既にあれば追記しない（何度実行しても重複しない）。
    既存の内容は消さない。ファイルが無ければ見出し付きで新規作成する。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _read_text(path)
    lines = text.splitlines() if text is not None else []

    if entry in lines:
        return path  # 重複防止

    heading = f"## {target.isoformat()}"
    if heading in lines:
        # 見出しの下（次の見出しの手前）へ差し込む
        start = lines.index(heading)
        insert_at = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].startswith("## "):
                insert_at = i
                break
        lines.insert(insert_at, entry)
    else:
        # 見出しごとファイル末尾に追加
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(heading)
        lines.append(entry)

    body = "\n".join(lines).rstrip("\n") + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    return path


# ---------------------------------------------------------------------------
# GitHubIssues
# ---------------------------------------------------------------------------

# `status:<value>` → (TaskStatus, blocked)
_ISSUE_STATUS: dict[str, tuple[TaskStatus, bool]] = {
    "todo": (TaskStatus.OPEN, False),
    "doing": (TaskStatus.IN_PROGRESS, False),
    "waiting": (TaskStatus.BLOCKED, True),
    "done": (TaskStatus.DONE, False),
    "cancelled": (TaskStatus.CANCELED, False),
    "canceled": (TaskStatus.CANCELED, False),
}

# `priority:<value>` → 1(高) - 5(低)
_ISSUE_PRIORITY: dict[str, int] = {"high": 1, "normal": 3, "low": 5}


class GitHubIssues:
    """GitHub Issue（行動）を扱う薄い REST クライアント。

    **読み取り専用**。Issue の起票・更新・クローズは一切行わない
    （Issue は `tasks.md` の写しであり、GitHub への変更は承認制の同期処理が担うため）。
    書き込み系のメソッドをここへ追加してはならない。

    urllib.request のみで REST API を叩く（requests は使わない）。
    repo と token が両方そろっているときだけ有効になる。
    """

    _API_VERSION = "2022-11-28"
    _USER_AGENT = "contextflow/0.1 (+https://github.com)"
    _PER_PAGE = 100
    _MAX_PAGES = 3

    def __init__(
        self, repo: str, token: Optional[str], config: Optional[AppConfig] = None
    ) -> None:
        self._repo = (repo or "").strip()
        self._token = token
        # ラベル体系は仕事管理リポジトリの定義に合わせる（config の [github.issue_labels]）
        self._status_prefix = _label_prefix(config, "status_prefix", "status:")
        self._when_prefix = _label_prefix(config, "when_prefix", "when:")
        self._priority_prefix = _label_prefix(config, "priority_prefix", "priority:")
        self._project_prefix = _label_prefix(config, "project_prefix", "pjt:")

    @property
    def enabled(self) -> bool:
        """repo と token が両方そろっているか。"""
        return bool(self._repo) and bool(self._token)

    def list_open(self) -> list[Task]:
        """open 状態の Issue を Task へ変換して返す。未設定なら空リスト。"""
        if not self.enabled:
            return []

        tasks: list[Task] = []
        for page in range(1, self._MAX_PAGES + 1):
            items = self._fetch_page(page)
            if not items:
                break
            for item in items:
                if "pull_request" in item:
                    # Pull Request は Issue API に混ざって返るので除外する
                    continue
                tasks.append(self._issue_to_task(item))
            if len(items) < self._PER_PAGE:
                break  # 最終ページ
        return tasks

    def sync_to_db(self, db: Database) -> int:
        """list_open() の結果を TaskRepository へ upsert し、件数を返す。"""
        repo = TaskRepository(db)
        count = 0
        for task in self.list_open():
            repo.upsert(task)
            count += 1
        return count

    # ------------------------------------------------------------------
    # 内部ヘルパ
    # ------------------------------------------------------------------

    def _fetch_page(self, page: int) -> list[dict]:
        url = (
            f"https://api.github.com/repos/{self._repo}/issues"
            f"?state=open&per_page={self._PER_PAGE}&page={page}"
        )
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", self._API_VERSION)
        request.add_header("User-Agent", self._USER_AGENT)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8")
        except urllib.error.URLError as error:
            raise RuntimeError(f"GitHub Issue の取得に失敗した: {error}") from error

        try:
            data = json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"GitHub からの応答を解析できない: {error}") from error
        if not isinstance(data, list):
            raise RuntimeError(f"GitHub からの応答の形式が不正: {data!r}")
        return data

    def _issue_to_task(self, item: dict) -> Task:
        labels = [self._label_name(label) for label in item.get("labels", [])]
        today = timeutil.today()

        status, blocked = self._status_from_labels(labels)
        priority = self._priority_from_labels(labels)
        project = self._project_from_labels(labels)

        # milestone の due_on は実際の締切。無ければ when: ラベルからの近似値を使う
        deadline = None
        milestone = item.get("milestone") or {}
        due_on = milestone.get("due_on")
        if due_on:
            deadline = parse_datetime(due_on).date()
        else:
            deadline = self._deadline_from_labels(labels, today)

        # updated_at を入れないと TaskRepository.upsert で既存タスクの
        # updated_at が NULL で上書きされるため、Issue 側の値（無ければ現在時刻）を必ず入れる
        updated_at_raw = item.get("updated_at")
        updated_at = parse_datetime(updated_at_raw) if updated_at_raw else timeutil.now()

        return Task(
            title=str(item.get("title", "")),
            project=project,
            status=status,
            priority=priority,
            deadline=deadline,
            github_issue=item.get("number"),
            blocked=blocked,
            updated_at=updated_at,
        )

    @staticmethod
    def _label_name(label) -> str:
        if isinstance(label, dict):
            return str(label.get("name", "")).lower()
        return str(label).lower()

    def _values(self, labels: list[str], prefix: str) -> list[str]:
        """`<prefix><value>` 形式のラベルから value 部分だけ取り出す。"""
        size = len(prefix)
        return [label[size:] for label in labels if prefix and label.startswith(prefix)]

    def _status_from_labels(self, labels: list[str]) -> tuple[TaskStatus, bool]:
        # status:* は排他（1つだけ付く運用）。不明値は OPEN 扱い
        for value in self._values(labels, self._status_prefix):
            if value in _ISSUE_STATUS:
                return _ISSUE_STATUS[value]
        return TaskStatus.OPEN, False

    def _priority_from_labels(self, labels: list[str]) -> int:
        # 1(高) - 5(低)。ラベルが無ければ既定の 3（priority:normal 相当）
        for value in self._values(labels, self._priority_prefix):
            if value in _ISSUE_PRIORITY:
                return _ISSUE_PRIORITY[value]
        return 3

    def _deadline_from_labels(self, labels: list[str], today: date) -> Optional[date]:
        """when: ラベルを deadline の目安へ写す。

        これは **近似値**であり、実際の締切ではない
        （リポジトリは具体的な実施日時を管理しない方針のため）。
        """
        for value in self._values(labels, self._when_prefix):
            if value == "today":
                return today
            if value == "this-week":
                return _end_of_week(today)
            if value == "this-month":
                return _end_of_month(today)
        return None

    def _project_from_labels(self, labels: list[str]) -> Optional[str]:
        """`pjt:<suffix>` を Task.project へ。"""
        for value in self._values(labels, self._project_prefix):
            if value:
                return value
        return None


def _label_prefix(config: Optional[AppConfig], key: str, default: str) -> str:
    if config is None:
        return default
    value = config.get(f"github.issue_labels.{key}", default)
    return str(value or default).lower()
