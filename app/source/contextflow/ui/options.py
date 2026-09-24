"""ui/options.py

画面から編集する「種別・案件・タスクの選択肢」を扱う層。

`ActivityType` の値（DB・設定・ドキュメントの契約）は不変。ここで扱うのは
**表示ラベルと、画面に出す選択肢（並び順・非表示・追加分）だけ**。

保存先は `app/data/ui_options.json`（`paths.database` と同じディレクトリ）。
`config.toml`（`app/source/config/`）はリポジトリに同梱される既定値であり、
`.venv` と同様にユーザーごとの変更を書き戻す先ではない。画面からの編集を
`config.toml` へ書き戻すと同梱資材を汚してしまい、「画面からの編集でリポジトリや
ファイルを作らない」という方針にも反するため、画面が書き換える値だけを
`app/data/`（ローカルのデータ置き場）側に分離し、`config.toml` の既定へ
実行時に上書き適用する。

ここにも `ui/api.py` と同じ方針を適用する。ロジックは薄く保ち、
`ActivityType` と `ContextRepo` / `TaskRepository` を読むだけに留める。
標準ライブラリのみを使用する（`json` / `pathlib`）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from contextflow.config import AppConfig
from contextflow.contracts.models import ActivityType
from contextflow.storage.db import Database
from contextflow.storage.repositories import TaskRepository

# 保存ファイル名。paths.database と同じディレクトリに置く
DEFAULT_FILENAME = "ui_options.json"

# tasks.suggestions の上限（際限なく増え続けないようにする）
MAX_TASK_SUGGESTIONS = 50

# ui_options.json が持てるトップレベルキー。これ以外は無視する
_KNOWN_SECTIONS = ("activity_types", "projects", "tasks")


# ---------------------------------------------------------------------------
# 保存・読み込み
# ---------------------------------------------------------------------------


def options_path(config: AppConfig) -> Path:
    """保存先のパス。`paths.database` と同じディレクトリに置く。"""
    return config.path("database").parent / DEFAULT_FILENAME


def load_options(config: AppConfig) -> dict:
    """保存値を読む。ファイルが無い・壊れている場合は空 dict（例外にしない）。"""
    path = options_path(config)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_options(config: AppConfig, data: dict) -> None:
    """保存値を書く。"""
    path = options_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 画面へ渡す確定形
# ---------------------------------------------------------------------------


def build_options(config: AppConfig, db: Database) -> dict:
    """画面へ渡す確定形を組み立てる。画面はこれだけを見ればよい。"""
    saved = load_options(config)
    return {
        "activity_types": _build_activity_types(config, saved.get("activity_types") or {}),
        "projects": _build_projects(config, saved.get("projects") or {}),
        "task_suggestions": _build_task_suggestions(db, saved.get("tasks") or {}),
    }


def _build_activity_types(config: AppConfig, saved: dict) -> list[dict]:
    """種別の選択肢。母集合は `ActivityType` の全値。"""
    default_labels = config.get("ui.activity_labels") or {}
    labels = dict(default_labels)
    labels.update(saved.get("labels") or {})
    order = _as_str_list(saved.get("order"))
    # hidden は設定を既定値とし、画面で保存した内容があればそちらを使う。
    # 画面のチェックを外した／戻した結果が、設定に打ち消されないようにするため
    saved_hidden = saved.get("hidden")
    if saved_hidden is None:
        hidden = set(_as_str_list(config.get("ui.hidden_activity_types")))
    else:
        hidden = set(_as_str_list(saved_hidden))

    all_values = [item.value for item in ActivityType]
    ordered = _apply_order(all_values, order)

    result = []
    for value in ordered:
        if value in hidden:
            continue
        result.append({"value": value, "label": str(labels.get(value) or value)})
    return result


def _build_projects(config: AppConfig, saved: dict) -> list[dict]:
    """案件の選択肢。リポジトリが読めない場合も extra だけを返す（例外にしない）。"""
    labels = saved.get("labels") or {}
    order = _as_str_list(saved.get("order"))
    hidden = set(_as_str_list(saved.get("hidden")))
    extra = _dedupe_strings(saved.get("extra") or [])

    # key -> from_repo。リポジトリ由来を先に積み、画面追加分を後から足す
    items: dict[str, bool] = {p.key: True for p in _repo_projects(config)}
    for key in extra:
        items.setdefault(key, False)

    ordered = _apply_order(list(items.keys()), order)

    result = []
    for key in ordered:
        if key in hidden:
            continue
        result.append(
            {
                "key": key,
                "label": str(labels.get(key) or key),
                "from_repo": items[key],
            }
        )
    return result


def _build_task_suggestions(db: Database, saved: dict) -> list[str]:
    """タスク候補。保存値の suggestions が先、その後に DB のタスクのタイトル。"""
    suggestions = _dedupe_strings(saved.get("suggestions") or [])
    seen = set(suggestions)
    result = list(suggestions)
    for task in TaskRepository(db).list():
        title = (task.title or "").strip()
        if title and title not in seen:
            seen.add(title)
            result.append(title)
    return result


# ---------------------------------------------------------------------------
# 検証して保存
# ---------------------------------------------------------------------------


def update_options(config: AppConfig, db: Database, body: dict) -> dict:
    """本体（保存する形）を検証して保存し、`build_options()` の結果を返す。

    指定されたセクション（activity_types / projects / tasks）だけを置き換え、
    指定しなかったセクションは既存の保存値のまま残す。未知のトップレベルキーは
    無視する（壊れた JSON で落ちないようにするため。既存の保存値に紛れ込んでいた
    ものも、保存し直す際に取り除かれる）。

    不正な入力は日本語メッセージの `ValueError`。呼び出し側（`api.handle`）が
    既存の作法どおり 400 へ変換する。
    """
    body = body if isinstance(body, dict) else {}
    current = load_options(config)
    data = {key: current[key] for key in _KNOWN_SECTIONS if key in current}

    if "activity_types" in body:
        data["activity_types"] = _validate_activity_types(body.get("activity_types"))
    if "projects" in body:
        data["projects"] = _validate_projects(config, body.get("projects"))
    if "tasks" in body:
        data["tasks"] = _validate_tasks(body.get("tasks"))

    save_options(config, data)
    return build_options(config, db)


def _validate_activity_types(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    valid_values = {item.value for item in ActivityType}

    raw_labels = raw.get("labels")
    if raw_labels is None:
        raw_labels = {}
    if not isinstance(raw_labels, dict):
        raise ValueError("activity_types.labels はオブジェクトで指定する")

    labels: dict[str, str] = {}
    for key, value in raw_labels.items():
        key_text = str(key)
        if key_text not in valid_values:
            choices = " / ".join(item.value for item in ActivityType)
            raise ValueError(f"種別が不正: {key_text}（選べる値: {choices}）")
        label_text = "" if value is None else str(value).strip()
        # 空にしたら「既定へ戻す」。案件のラベルと扱いを揃える
        # （画面で入力欄を空にしたときに、エラーではなく既定へ戻るのが自然なため）
        if label_text:
            labels[key_text] = label_text

    order = _as_str_list(raw.get("order"))
    hidden = _as_str_list(raw.get("hidden"))

    visible = [value for value in valid_values if value not in set(hidden)]
    if not visible:
        raise ValueError("種別をすべて非表示にはできない（選択肢が無くなるため）")

    return {"labels": labels, "order": order, "hidden": hidden}


def _validate_projects(config: AppConfig, raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}

    raw_labels = raw.get("labels")
    if raw_labels is None:
        raw_labels = {}
    if not isinstance(raw_labels, dict):
        raise ValueError("projects.labels はオブジェクトで指定する")
    # 空にしたら「既定へ戻す」。種別のラベルと扱いを揃える
    labels = {
        str(key): str(value).strip()
        for key, value in raw_labels.items()
        if value is not None and str(value).strip()
    }

    order = _as_str_list(raw.get("order"))
    hidden = _as_str_list(raw.get("hidden"))

    raw_extra = raw.get("extra")
    if raw_extra is None:
        raw_extra = []
    if not isinstance(raw_extra, list):
        raise ValueError("projects.extra はリストで指定する")
    extra = _dedupe_strings(raw_extra)

    repo_keys = {p.key for p in _repo_projects(config)}
    conflicts = [key for key in extra if key in repo_keys]
    if conflicts:
        raise ValueError(
            "projects.extra にリポジトリ由来の案件名と同じものは入れられない: "
            + ", ".join(conflicts)
        )

    return {"labels": labels, "order": order, "hidden": hidden, "extra": extra}


def _validate_tasks(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}

    raw_suggestions = raw.get("suggestions")
    if raw_suggestions is None:
        raw_suggestions = []
    if not isinstance(raw_suggestions, list):
        raise ValueError("tasks.suggestions はリストで指定する")
    if len(raw_suggestions) > MAX_TASK_SUGGESTIONS:
        raise ValueError(f"tasks.suggestions は最大{MAX_TASK_SUGGESTIONS}件まで")
    for item in raw_suggestions:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("tasks.suggestions の要素は空でない文字列で指定する")

    return {"suggestions": _dedupe_strings(raw_suggestions)}


# ---------------------------------------------------------------------------
# 内部ヘルパ
# ---------------------------------------------------------------------------


def _repo_projects(config: AppConfig) -> list:
    """`ContextRepo.projects()` の結果。読めない場合は空リスト（例外にしない）。

    `context_repo` が未配置・パス不正でも画面は開けるようにするため、
    `ui/api.py` の `_get_projects` と同じ考え方で例外を握りつぶす。
    """
    try:
        from contextflow.sources.github_context import ContextRepo

        return ContextRepo(config.path("context_repo"), config).projects()
    except Exception:
        return []


def _apply_order(values: list[str], order: list[str]) -> list[str]:
    """`order` に書かれた順を先頭に、書かれていない値は元の順のまま後ろへ。"""
    ordered = [value for value in order if value in values]
    rest = [value for value in values if value not in order]
    return ordered + rest


def _as_str_list(value: Any) -> list[str]:
    """リストなら文字列のリストへ、そうでなければ空リスト。"""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _dedupe_strings(values: Any) -> list[str]:
    """前後の空白を除いた文字列の重複を除く（順序は保つ。空文字は落とす）。"""
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
