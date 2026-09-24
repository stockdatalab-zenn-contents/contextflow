"""ui/api.py

`docs/20260923_ui_design.md` §4 の API 契約を、既存 API の呼び出しだけで満たす層。

ここにロジックは書かない。`ManualInput` / 各 `Repository` / `build_day` / `find_gaps` を
呼び、入力検証と JSON 化だけを担当する。
例外は「層の判定規則」と「空白時間の切り出し規則」の2箇所だけで、
これは `cli.py` を import しないため（CLI と同じ規則をここへ書き写す）。

DB 接続はリクエストごとに開いて閉じる（`ThreadingHTTPServer` で複数スレッドが触るため）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from contextflow import timeutil
from contextflow.config import AppConfig
from contextflow.contracts.models import (
    Activity,
    ActivityLayer,
    ActivityType,
    Change,
    Decision,
    Source,
)
from contextflow.collector.collector import HEARTBEAT_KEY
from contextflow.contracts.serde import to_jsonable
from contextflow.storage.db import Database, open_database
from contextflow.storage.repositories import (
    ActivityRepository,
    CalendarLabelRepository,
    ChangeRepository,
    DecisionRepository,
    MetaRepository,
    RawEventRepository,
    SessionRepository,
)
from contextflow.ui import collector_runner, options

# 空白時間とみなす最小の長さ（cmd_gaps の既定値と同じ）
MIN_GAP_SEC = 600

# ローカルで完結する Decision Engine。これ以外は外部へ HTTP で問い合わせる
_LOCAL_ENGINES = frozenset({"rule_based"})

# 生ログを一度に取り出せる最大範囲。画面共有時の事故を避けるため広げない
MAX_RAW_RANGE = timedelta(hours=24)

# '...T10:00:00 09:00' のように '+' が空白へ化けたタイムゾーンオフセットを見つける
_OFFSET_SPACE_RE = re.compile(r"(\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s(\d{2}:\d{2})$")


class _ApiError(Exception):
    """HTTP ステータスと日本語メッセージを持つ、この層だけの例外。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def handle(
    method: str, path: str, query: dict, body: dict, config: AppConfig
) -> tuple[int, Any]:
    """API 要求を1件処理し、(HTTPステータス, JSON にできる値) を返す。

    docs §4 に合わせ、`/api/projects` と `/api/raw` だけは配列を返す。
    それ以外は dict。エラーは `{"error": "日本語メッセージ"}`。
    """
    database = open_database(config)
    try:
        return _dispatch(method, path, query or {}, body or {}, config, database)
    except _ApiError as exc:
        return exc.status, {"error": exc.message}
    except ValueError as exc:
        # 既存 API が投げる日本語の ValueError（時刻の前後関係など）はそのまま 400 へ
        return 400, {"error": str(exc)}
    finally:
        database.close()


def _dispatch(
    method: str, path: str, query: dict, body: dict, config: AppConfig, db: Database
) -> tuple[int, Any]:
    """パスを見て担当関数へ振り分ける。"""
    parts = [segment for segment in path.split("/") if segment]
    if not parts or parts[0] != "api":
        raise _ApiError(404, "存在しない API")
    rest = parts[1:]

    # --- 参照 ---
    if method == "GET" and rest == ["day"]:
        return 200, _get_day(query, config, db)
    if method == "GET" and rest == ["projects"]:
        return 200, _get_projects(config)
    if method == "GET" and rest == ["raw"]:
        return 200, _get_raw(query, db)
    if method == "GET" and rest == ["options"]:
        return 200, options.build_options(config, db)
    if method == "GET" and rest == ["state"]:
        return 200, _get_state(query, config, db)
    if method == "GET" and rest == ["calendar", "events"]:
        return 200, _get_calendar_events(query, config, db)

    # --- 更新（予定への種別ラベル） ---
    if method == "PUT" and rest == ["calendar", "label"]:
        return 200, _put_calendar_label(body, db)

    # --- 更新（種別・案件・タスクの選択肢） ---
    if method == "PUT" and rest == ["options"]:
        return 200, options.update_options(config, db, body)

    # --- 更新（作業） ---
    if method == "POST" and rest == ["work", "start"]:
        return 200, _post_work_start(body, db)
    if method == "POST" and rest == ["work", "stop"]:
        return 200, _post_work_stop(body, db)
    if method == "POST" and rest == ["work", "add"]:
        return 200, _post_work_add(body, db)

    # --- 更新（活動） ---
    if len(rest) == 2 and rest[0] == "activity":
        activity_id = _as_id(rest[1])
        if method == "PUT":
            return 200, _put_activity(activity_id, body, db)
        if method == "DELETE":
            return 200, _delete_activity(activity_id, db)

    # --- 更新（変化） ---
    if method == "POST" and rest == ["change"]:
        return 200, _post_change(body, db)
    if len(rest) == 2 and rest[0] == "change":
        change_id = _as_id(rest[1])
        if method == "PUT":
            return 200, _put_change(change_id, body, db)
        if method == "DELETE":
            return 200, _delete_change(change_id, db)

    # --- 更新（判断） ---
    if method == "POST" and rest == ["decision"]:
        return 200, _post_decision(body, db)
    if len(rest) == 2 and rest[0] == "decision":
        decision_id = _as_id(rest[1])
        if method == "PUT":
            return 200, _put_decision(decision_id, body, db)
        if method == "DELETE":
            return 200, _delete_decision(decision_id, db)

    # --- 確定の作り直し ---
    if method == "POST" and rest == ["build"]:
        return 200, _post_build(body, config, db)

    # --- Current State（CLI の state と同じ処理） ---
    if method == "POST" and rest == ["state"]:
        return 200, _post_state(body, config, db)

    # --- 判断・計画（CLI の decide / plan と同じ処理） ---
    if method == "GET" and rest == ["decide", "info"]:
        return 200, _get_decide_info(config)
    if method == "POST" and rest == ["decide"]:
        return 200, _post_decide(body, config, db)
    if method == "POST" and rest == ["plan"]:
        return 200, _post_plan(body, config, db)

    # --- 収集（UI プロセス内のバックグラウンドスレッド） ---
    if method == "POST" and rest == ["collector", "start"]:
        return 200, _post_collector_start(config)
    if method == "POST" and rest == ["collector", "stop"]:
        return 200, _post_collector_stop()

    raise _ApiError(404, "存在しない API")


# ---------------------------------------------------------------------------
# 参照
# ---------------------------------------------------------------------------


def _collector_status(
    config: AppConfig, db: Database, start: datetime, end: datetime
) -> dict:
    """生ログ収集（collect）が動いているかを判断する。

    判断材料は、収集側が毎回更新する心拍（meta の `collector_heartbeat`）だけ。

    生ログの最終時刻では判断しない。生ログは flush_every 件ごとのまとめ書きなので
    最大 flush_every × interval 秒ぶん古く見え、
    「開始直後なのに停止中」「終了済みなのに収集中」の両方を起こすため。
    収集は終了時に心拍を消すので、停止は即座に反映される。
    """
    repository = RawEventRepository(db)
    last = repository.last()
    interval = int(config.get("collector.interval_sec", 5) or 5)
    flush_every = int(config.get("collector.flush_every", 12) or 12)
    events_today = len(repository.list_between(start, end))

    # 心拍は毎回更新されるので、採取間隔の3倍あれば取りこぼしにも耐える
    heartbeat = MetaRepository(db).get(HEARTBEAT_KEY)
    running = _within(heartbeat, interval * 3) if heartbeat else False

    return {
        "running": running,
        "last_event_at": to_jsonable(last.ts) if last else None,
        "events_in_range": events_today,
        "interval_sec": interval,
        "flush_every": flush_every,
        # この UI 自身が収集スレッドを持っているか（画面の「停止」ボタンの出し分けに使う）
        "owned_by_ui": collector_runner.is_running(),
    }


def _within(iso_text: str, limit_sec: float) -> bool:
    """ISO8601 の時刻が、今から limit_sec 以内かどうか。"""
    try:
        moment = timeutil.ensure_aware(datetime.fromisoformat(iso_text))
    except (TypeError, ValueError):
        return False
    elapsed = (timeutil.now() - moment).total_seconds()
    return 0 <= elapsed <= limit_sec


def _get_day(query: dict, config: AppConfig, db: Database) -> dict:
    """その日の全データを返す。"""
    target = _query_date(query, "date")
    start, end = timeutil.day_range(target)

    activities = _day_activities(db, start, end)
    sessions = SessionRepository(db).list_between(start, end)
    changes = ChangeRepository(db).list_between(start, end)
    decisions = DecisionRepository(db).list_between(start, end)

    from contextflow.sources.manual import ManualInput

    running = ManualInput(db).running()

    return {
        "date": target.isoformat(),
        "running": to_jsonable(running) if running else None,
        "collector": _collector_status(config, db, start, end),
        "sessions": [
            {
                "id": s.id,
                "start_at": to_jsonable(s.start_at),
                "end_at": to_jsonable(s.end_at),
                "process": s.process,
                "duration_sec": s.duration_sec,
            }
            for s in sessions
        ],
        "activities": [_activity_json(a) for a in activities],
        "gaps": [
            [to_jsonable(gap_start), to_jsonable(gap_end)]
            for gap_start, gap_end in _day_gaps(activities)
        ],
        "changes": [
            {
                "id": c.id,
                "ts": to_jsonable(c.ts),
                "description": c.description,
                "project": c.project,
            }
            for c in changes
        ],
        "decisions": [
            {
                "id": d.id,
                "ts": to_jsonable(d.ts),
                "decision": d.decision,
                "reason": d.reason,
                "project": d.project,
            }
            for d in decisions
        ],
        "activity_types": [item.value for item in ActivityType],
        # 種別・案件・タスクの表示ラベルと選択肢（日本語表示・画面からの編集用）。
        # 上の "activity_types"（文字列配列）は後方互換のためそのまま残す
        "options": options.build_options(config, db),
    }


def _day_activities(db: Database, start: datetime, end: datetime) -> list[Activity]:
    """その日の活動を1層だけ返す（`cli.py` の `_day_activities` と同じ規則）。

    層を指定せずに読むと確定と元データが両方返って二重計上になるため、
    確定 → 観測 → 手入力 → 予定 の順で最初に見つかった1層だけを使う。
    """
    repository = ActivityRepository(db)
    for layer in (
        ActivityLayer.CONFIRMED,
        ActivityLayer.OBSERVED,
        ActivityLayer.REPORTED,
        ActivityLayer.PLANNED,
    ):
        activities = repository.list_between(start, end, layer=layer)
        if activities:
            return activities
    return []


def _day_gaps(activities: list[Activity]) -> list[tuple[datetime, datetime]]:
    """証拠のある活動の間の空白を返す（`cmd_gaps` と同じ考え方）。

    予定（カレンダー由来）は証拠ではないので除き、
    最初の活動〜最後の活動の間だけを対象にする（就寝時間を空白と呼ばないため）。
    """
    from contextflow.pipeline.p30_activity_builder import find_gaps

    if len(activities) < 2:
        return []
    evidence = [a for a in activities if a.source is not Source.CALENDAR]
    if len(evidence) < 2:
        return []
    span_start = min(a.start_at for a in evidence)
    span_end = max(a.end_at for a in evidence)
    return find_gaps(evidence, span_start, span_end, min_gap_sec=MIN_GAP_SEC)


def _activity_json(activity: Activity) -> dict:
    """活動1件を画面用の dict へ。"""
    return {
        "id": activity.id,
        "start_at": to_jsonable(activity.start_at),
        "end_at": to_jsonable(activity.end_at),
        "activity_type": activity.activity_type.value,
        "layer": activity.layer.value,
        "source": activity.source.value,
        "project": activity.project,
        "task": activity.task,
        "summary": activity.summary,
        "confidence": activity.confidence,
        "editable": _is_editable(activity),
    }


def _is_editable(activity: Activity) -> bool:
    """編集できるのは本人入力の層（reported）だけ。

    PCログ由来（observed）・予定（planned）・確定（confirmed）は
    生成物なので画面から直させない。
    """
    return activity.layer is ActivityLayer.REPORTED


def _get_projects(config: AppConfig) -> list[dict]:
    """案件フォルダの一覧。読めない場合は空リスト（例外にしない）。"""
    try:
        from contextflow.sources.github_context import ContextRepo

        repo = ContextRepo(config.path("context_repo"), config)
        return [
            {
                "key": project.key,
                "folder": project.folder,
                "prefix": project.prefix,
                "suffix": project.suffix,
            }
            for project in repo.projects()
        ]
    except Exception:
        # 仕事管理リポジトリが未配置でも画面は開けるようにする
        return []


def _get_raw(query: dict, db: Database) -> list[dict]:
    """生ログ。範囲指定は必須で、24時間を超える範囲は受け付けない。"""
    start_text = str(query.get("start") or "").strip()
    end_text = str(query.get("end") or "").strip()
    if not start_text or not end_text:
        raise _ApiError(400, "生ログは start と end の範囲指定が必須")

    start = _parse_iso(start_text, "start")
    end = _parse_iso(end_text, "end")
    if end <= start:
        raise _ApiError(400, "end は start より後である必要がある")
    if end - start > MAX_RAW_RANGE:
        raise _ApiError(400, "生ログを一度に取り出せる範囲は24時間まで")

    return [
        {
            "ts": to_jsonable(event.ts),
            "process": event.process,
            "window_title": event.window_title,
            "idle_sec": event.idle_sec,
        }
        for event in RawEventRepository(db).list_between(start, end)
    ]


def _get_calendar_events(query: dict, config: AppConfig, db: Database) -> dict:
    """保存済みの予定（layer=planned）を uid でまとめて返す。種別ラベル付け画面用。

    範囲は「今日の0:00」から「days 日後の0:00」まで（days 省略時は
    config の calendar.fetch_days）。uid の無い予定は再取得後に対応付けできず
    種別を付けられないため events には含めず、件数だけ no_uid で返す。
    """
    days = _query_int(query, "days", int(config.get("calendar.fetch_days", 7)))
    start, _unused = timeutil.day_range(timeutil.today())
    end, _unused = timeutil.day_range(timeutil.today() + timedelta(days=days))

    activities = ActivityRepository(db).list_between(start, end, layer=ActivityLayer.PLANNED)
    labels = CalendarLabelRepository(db).all()

    groups: dict[str, dict] = {}
    no_uid = 0
    for activity in activities:
        uid = str(activity.detail.get("uid") or "").strip()
        if not uid:
            no_uid += 1
            continue
        group = groups.get(uid)
        if group is None:
            group = {
                "uid": uid,
                "summary": activity.summary,
                "activity_type": activity.activity_type.value,
                "labeled": uid in labels,
                "count": 0,
                "first_start": activity.start_at,
                "last_end": activity.end_at,
            }
            groups[uid] = group
        group["count"] += 1
        group["first_start"] = min(group["first_start"], activity.start_at)
        group["last_end"] = max(group["last_end"], activity.end_at)

    # labeled=false を先、その中では first_start の昇順
    ordered = sorted(groups.values(), key=lambda g: (g["labeled"], g["first_start"]))

    return {
        "days": days,
        "start": to_jsonable(start),
        "end": to_jsonable(end),
        "events": [
            {
                "uid": g["uid"],
                "summary": g["summary"],
                "activity_type": g["activity_type"],
                "labeled": g["labeled"],
                "count": g["count"],
                "first_start": to_jsonable(g["first_start"]),
                "last_end": to_jsonable(g["last_end"]),
            }
            for g in ordered
        ],
        "unlabeled": sum(1 for g in groups.values() if not g["labeled"]),
        "no_uid": no_uid,
    }


# ---------------------------------------------------------------------------
# 更新（作業）
# ---------------------------------------------------------------------------


def _post_work_start(body: dict, db: Database) -> dict:
    """作業を開始する。"""
    from contextflow.sources.manual import ManualInput

    activity_type = _activity_type(body.get("activity_type"))
    at = _optional_time(body, "at", base=timeutil.today())
    manual = ManualInput(db)
    # start() は実行中の作業があればそれを自動で終了する。画面の表示と食い違わないよう、
    # 何が起きたかを message で返す
    previous = manual.running()
    manual.start(
        activity_type,
        project=_optional_text(body, "project"),
        task=_optional_text(body, "task"),
        at=at,
    )
    # 種別の日本語ラベルは画面側が持つため、ここでは値だけ返して文言は組み立てない
    return {
        "ok": True,
        "running": to_jsonable(manual.running()),
        "stopped_type": previous["activity_type"].value if previous else None,
    }


def _post_work_stop(body: dict, db: Database) -> dict:
    """実行中の作業を終了して活動にする。実行中が無ければ activity は null。

    日をまたいだ場合は複数件に分割される。既存画面を壊さないよう `activity` には
    従来どおり単一の活動（分割時は終了時刻を含む最後の区間）を入れ、加えて
    分割後の全区間を `activities` に配列で入れる。
    """
    from contextflow.sources.manual import ManualInput

    at = _optional_time(body, "at", base=timeutil.today())
    manual = ManualInput(db)
    was_running = manual.running() is not None
    activities = manual.stop(at=at, summary=_text(body, "summary"))
    if not activities:
        # 実行中が無かったのか、開始直後で取り消したのかを区別して伝える
        message = "開始直後のため記録せず取り消した" if was_running else "実行中の作業なし"
        return {"ok": True, "activity": None, "activities": [], "message": message}
    return {
        "ok": True,
        "activity": _activity_json(activities[-1]),
        "activities": [_activity_json(a) for a in activities],
    }


def _post_work_add(body: dict, db: Database) -> dict:
    """過去の時間帯を後から活動として追加する。

    日をまたいだ場合は複数件に分割される。`activity` は従来どおり単一の活動
    （分割時は終了時刻を含む最後の区間）、`activities` に全区間を入れる。
    """
    from contextflow.sources.manual import ManualInput

    base = _body_date(body, "date")
    start = _required_time(body, "start", base)
    end = _required_time(body, "end", base)
    _check_order(start, end)

    activities = ManualInput(db).add(
        start,
        end,
        _activity_type(body.get("activity_type")),
        project=_optional_text(body, "project"),
        task=_optional_text(body, "task"),
        summary=_text(body, "summary"),
    )
    return {
        "ok": True,
        "activity": _activity_json(activities[-1]),
        "activities": [_activity_json(a) for a in activities],
    }


# ---------------------------------------------------------------------------
# 更新（活動）
# ---------------------------------------------------------------------------


def _editable_activity(activity_id: int, db: Database) -> Activity:
    """編集対象の活動を取り出す。編集できない層なら 409。"""
    activity = ActivityRepository(db).get(activity_id)
    if activity is None:
        raise _ApiError(404, "指定された活動が見つからない")
    if not _is_editable(activity):
        raise _ApiError(
            409,
            "PCログ・予定から作られた活動は編集できない。"
            "元になった手入力を直してから「タイムラインを作り直す」（build）で反映する",
        )
    return activity


def _put_activity(activity_id: int, body: dict, db: Database) -> dict:
    """手入力の活動を書き換える。"""
    current = _editable_activity(activity_id, db)
    base = current.start_at.date()
    start = _required_time(body, "start", base)
    end = _required_time(body, "end", base)
    _check_order(start, end)

    updated = Activity(
        start_at=start,
        end_at=end,
        activity_type=_activity_type(body.get("activity_type")),
        layer=current.layer,
        source=current.source,
        project=_optional_text(body, "project"),
        task=_optional_text(body, "task"),
        confidence=current.confidence,
        summary=_text(body, "summary"),
        detail=current.detail,
        id=current.id,
    )
    try:
        ActivityRepository(db).update(updated)
    except ValueError as exc:
        # 同じ時間帯・種別が既にある場合（UNIQUE 制約）
        raise _ApiError(409, str(exc)) from exc
    return {"ok": True, "activity": _activity_json(updated)}


def _delete_activity(activity_id: int, db: Database) -> dict:
    """手入力の活動を削除する。"""
    _editable_activity(activity_id, db)
    ActivityRepository(db).delete(activity_id)
    return {"ok": True, "id": activity_id}


# ---------------------------------------------------------------------------
# 更新（予定への種別ラベル）
# ---------------------------------------------------------------------------


def _put_calendar_label(body: dict, db: Database) -> dict:
    """予定（uid）に種別ラベルを付ける・外す。

    保存先は calendar_labels（uid キー）。次の calendar sync を待たず画面・build へ
    反映させるため、その uid を持つ既存の planned 行の activity_type も合わせて
    更新する。activity_type に null を渡すとラベルを削除し、planned 行は既定の
    meeting へ戻す。
    """
    uid = _text(body, "uid")
    if not uid:
        raise _ApiError(400, "uid が無い予定には種別を付けられない")

    label_repo = CalendarLabelRepository(db)
    activity_repo = ActivityRepository(db)

    raw_type = body.get("activity_type")
    if raw_type is None:
        label_repo.delete(uid)
        updated = activity_repo.update_type_by_uid(uid, ActivityType.MEETING)
        return {"ok": True, "uid": uid, "activity_type": None, "updated": updated}

    activity_type = _activity_type(raw_type)
    label_repo.set(uid, activity_type)
    updated = activity_repo.update_type_by_uid(uid, activity_type)
    return {"ok": True, "uid": uid, "activity_type": activity_type.value, "updated": updated}


# ---------------------------------------------------------------------------
# 更新（変化）
# ---------------------------------------------------------------------------


def _post_change(body: dict, db: Database) -> dict:
    """状況の変化を記録する。"""
    change = Change(
        ts=_timestamp(body),
        description=_required_text(body, "description", "変化の内容"),
        project=_optional_text(body, "project"),
    )
    change.id = ChangeRepository(db).add(change)
    return {"ok": True, "change": _change_json(change)}


def _put_change(change_id: int, body: dict, db: Database) -> dict:
    """記録済みの変化を書き換える。"""
    repository = ChangeRepository(db)
    current = repository.get(change_id)
    if current is None:
        raise _ApiError(404, "指定された変化が見つからない")

    current.description = _required_text(body, "description", "変化の内容")
    current.project = _optional_text(body, "project")
    ts = _optional_time(body, "ts", base=current.ts.date())
    if ts is not None:
        current.ts = ts
    repository.update(current)
    return {"ok": True, "change": _change_json(current)}


def _delete_change(change_id: int, db: Database) -> dict:
    repository = ChangeRepository(db)
    if repository.get(change_id) is None:
        raise _ApiError(404, "指定された変化が見つからない")
    repository.delete(change_id)
    return {"ok": True, "id": change_id}


def _change_json(change: Change) -> dict:
    return {
        "id": change.id,
        "ts": to_jsonable(change.ts),
        "description": change.description,
        "project": change.project,
    }


# ---------------------------------------------------------------------------
# 更新（判断）
# ---------------------------------------------------------------------------


def _post_decision(body: dict, db: Database) -> dict:
    """人が下した判断を記録する。"""
    decision = Decision(
        ts=_timestamp(body),
        decision=_required_text(body, "decision", "判断の内容"),
        reason=_text(body, "reason"),
        project=_optional_text(body, "project"),
    )
    decision.id = DecisionRepository(db).add(decision)
    return {"ok": True, "decision": _decision_json(decision)}


def _put_decision(decision_id: int, body: dict, db: Database) -> dict:
    """記録済みの判断を書き換える。"""
    repository = DecisionRepository(db)
    current = repository.get(decision_id)
    if current is None:
        raise _ApiError(404, "指定された判断が見つからない")

    current.decision = _required_text(body, "decision", "判断の内容")
    current.reason = _text(body, "reason")
    current.project = _optional_text(body, "project")
    ts = _optional_time(body, "ts", base=current.ts.date())
    if ts is not None:
        current.ts = ts
    repository.update(current)
    return {"ok": True, "decision": _decision_json(current)}


def _delete_decision(decision_id: int, db: Database) -> dict:
    repository = DecisionRepository(db)
    if repository.get(decision_id) is None:
        raise _ApiError(404, "指定された判断が見つからない")
    repository.delete(decision_id)
    return {"ok": True, "id": decision_id}


def _decision_json(decision: Decision) -> dict:
    return {
        "id": decision.id,
        "ts": to_jsonable(decision.ts),
        "decision": decision.decision,
        "reason": decision.reason,
        "project": decision.project,
    }


# ---------------------------------------------------------------------------
# 確定の作り直し
# ---------------------------------------------------------------------------


def _post_build(body: dict, config: AppConfig, db: Database) -> dict:
    """その日の確定 activity を作り直す。"""
    from contextflow.pipeline.p30_activity_builder import build_day

    target = _body_date(body, "date")
    confirmed = build_day(db, target, config)
    return {"ok": True, "date": target.isoformat(), "count": len(confirmed)}


# ---------------------------------------------------------------------------
# Current State（実行＝作って保存、参照＝保存済みを読む）
# ---------------------------------------------------------------------------


def _get_state(query: dict, config: AppConfig, db: Database) -> dict:
    """保存済みの Current State を返す。ここでは組み立てない。

    参照で暗黙に組み立てると「いつ時点の状態か」が分からなくなるため、
    作成は POST /api/state だけが行う。
    """
    from contextflow.storage.repositories import StateSnapshotRepository

    target = _query_date(query, "date")
    row = StateSnapshotRepository(db).latest_for(target)
    return {
        "date": target.isoformat(),
        "state": row["state_json"] if row else None,
        "generated_at": row["generated_at"] if row else None,
        "path": str(config.path("state_json")),
    }


def _post_state(body: dict, config: AppConfig, db: Database) -> dict:
    """Current State を組み立てて保存する（CLI の `cf.py state` と同じ）。"""
    from contextflow.context.builder import ContextBuilder

    target = _body_date(body, "date")
    builder = ContextBuilder(db, config)
    state = builder.build(target)
    path = builder.save_json(state)
    return {
        "ok": True,
        "date": target.isoformat(),
        "state": to_jsonable(state),
        "generated_at": to_jsonable(state.generated_at),
        "path": str(path),
    }


# ---------------------------------------------------------------------------
# 判断・計画（CLI の cmd_decide / cmd_plan と同じ規則。cli.py は import しない）
# ---------------------------------------------------------------------------


def _get_decide_info(config: AppConfig) -> dict:
    """画面が「どのエンジンで動くか」「どの質問セットが選べるか」を出すための情報。

    実行はせず、運用方針（decision.mode）と質問セット定義を読むだけ。
    """
    from contextflow.config import load_question_sets, resolve_mode

    mode = resolve_mode(config)
    # 外部へ出るかどうかで分ける。rule_based だけがローカル完結で、
    # claude / openai_compat は各社の API、jev は Jev Decision API へ HTTP で送る。
    # 「LLM かどうか」ではなく「外部送信が起きるか」を画面へ伝える（jev は LLM ではない）
    external_engines = [e for e in mode.engines if e not in _LOCAL_ENGINES]
    sets = load_question_sets(config.sibling("questions.toml"))
    # 表示名は設定から引く。無いセットは保存値（英語名）をそのまま出す
    set_labels = config.get("ui.question_set_labels") or {}

    return {
        "mode": mode.name,
        "description": mode.description,
        "engines": list(mode.engines),
        "planner": mode.planner,
        "external_engines": external_engines,
        "planner_external": bool(mode.uses_llm_planner),
        "sends_external": bool(external_engines) or bool(mode.uses_llm_planner),
        "threshold": float(config.get("decision.confidence_threshold", 0.7)),
        "default_set": config.get("decision.question_set", "next_action"),
        "sets": [
            {
                "name": name,
                "label": str(set_labels.get(name) or name),
                "questions": [_question_json(q) for q in questions],
            }
            for name, questions in sets.items()
        ],
        # choice 型の選択肢の表示名（質問の key ごと）。画面が日本語へ置き換えるのに使う
        "choice_labels": config.get("ui.choice_labels") or {},
    }


def _question_json(question: Any) -> dict:
    """質問定義1件ぶんの画面用 dict。"""
    return {
        "key": question.key,
        "type": question.type.value,
        "instruction": question.instruction,
        "choices": list(question.choices),
        "min": question.min,
        "max": question.max,
    }


def _run_decision(body: dict, config: AppConfig, db: Database) -> tuple[Any, Any, dict]:
    """state を組み立てて保存し、判断まで行う（decide / plan の共通処理）。

    画面の「現在の状態」カードと判断に使った材料を一致させるため、
    判断の前に必ず state を保存する。戻り値は (state, response, payload)。
    """
    import sys

    from contextflow.config import load_question_sets
    from contextflow.context.builder import ContextBuilder, state_to_flat_dict
    from contextflow.contracts.decision import DecisionRequest
    from contextflow.decision.registry import create_engine, create_engine_chain
    from contextflow.storage.repositories import DecisionLogRepository

    target = _body_date(body, "date")
    builder = ContextBuilder(db, config)
    state = builder.build(target)
    builder.save_json(state)

    set_name = _optional_text(body, "set") or config.get(
        "decision.question_set", "next_action"
    )
    questions = load_question_sets(config.sibling("questions.toml")).get(set_name)
    if not questions:
        raise _ApiError(400, f"存在しない質問セット: {set_name}")

    engine = create_engine_chain(config, db, mode=None)
    request = DecisionRequest(state=state_to_flat_dict(state), questions=questions)
    try:
        response = engine.ask(request)
    except RuntimeError as error:
        # cli.py の _ask_with_fallback と同じ規則: チェーンが全滅しても rule_based へ退避
        print(f"警告: {error} → rule_based へ退避", file=sys.stderr)
        response = create_engine(config, db, name="rule_based").ask(request)

    DecisionLogRepository(db).add_response(request, response)

    threshold = float(config.get("decision.confidence_threshold", 0.7))
    answers = []
    for question in questions:
        answer = response.answers.get(question.key)
        if answer is None:
            continue
        answers.append(
            {
                "key": question.key,
                "type": question.type.value,
                "instruction": question.instruction,
                "value": answer.value,
                "confidence": answer.confidence,
                "raw_confidence": answer.raw_confidence,
                "engine": answer.engine,
                "rationale": answer.rationale,
                "held": answer.confidence < threshold,
            }
        )

    payload = {
        "ok": True,
        "date": target.isoformat(),
        "set": set_name,
        "engine": response.engine,
        "latency_ms": response.latency_ms,
        "threshold": threshold,
        "generated_at": to_jsonable(state.generated_at),
        "answers": answers,
    }
    return state, response, payload


def _post_decide(body: dict, config: AppConfig, db: Database) -> dict:
    """型付き判断を実行する（`cf.py decide` と同じ処理）。"""
    _state, _response, payload = _run_decision(body, config, db)
    return payload


def _post_plan(body: dict, config: AppConfig, db: Database) -> dict:
    """判断結果をもとに、人間向けの説明・計画を作る（`cf.py plan` と同じ処理）。"""
    from contextflow.planner.planner import Planner

    state, response, payload = _run_decision(body, config, db)
    payload["text"] = Planner(config, mode=None).make_plan(state, response)
    return payload


# ---------------------------------------------------------------------------
# 収集（UI プロセス内のバックグラウンドスレッド）
# ---------------------------------------------------------------------------


def _post_collector_start(config: AppConfig) -> dict:
    """生ログ収集を開始する。別プロセスが収集中なら 409。"""
    try:
        result = collector_runner.start(config)
    except ValueError as exc:
        # 別プロセス（CLI の collect や別の UI）が収集中
        raise _ApiError(409, str(exc)) from exc
    return {
        "running": result.running,
        "message": result.message,
        "started_at": to_jsonable(result.started_at) if result.started_at else None,
    }


def _post_collector_stop() -> dict:
    """生ログ収集を止める。動いていなくても例外にしない。"""
    result = collector_runner.stop()
    return {
        "running": result.running,
        "message": result.message,
        "collected": result.collected,
    }


# ---------------------------------------------------------------------------
# 入力検証のヘルパ
# ---------------------------------------------------------------------------


def _as_id(text: str) -> int:
    """パス末尾の id を整数へ。"""
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise _ApiError(400, "id は整数で指定する") from None
    if value <= 0:
        raise _ApiError(400, "id は1以上で指定する")
    return value


def _text(body: dict, key: str) -> str:
    """任意の文字列。未指定なら空文字。"""
    value = body.get(key)
    return "" if value is None else str(value).strip()


def _optional_text(body: dict, key: str) -> Optional[str]:
    """任意の文字列。空なら None（案件名など、未指定を残したい項目）。"""
    value = _text(body, key)
    return value or None


def _required_text(body: dict, key: str, label: str) -> str:
    """必須の文字列。空なら 400。"""
    value = _text(body, key)
    if not value:
        raise _ApiError(400, f"{label}は必須")
    return value


def _activity_type(value: Any) -> ActivityType:
    """種別を検証する。`ActivityType` に無ければ 400。"""
    text = "" if value is None else str(value).strip()
    try:
        return ActivityType(text)
    except ValueError:
        choices = " / ".join(item.value for item in ActivityType)
        raise _ApiError(
            400, f"種別が不正: {text or '(未指定)'}（指定できる値: {choices}）"
        ) from None


def _parse_time(text: str, base: date, label: str) -> datetime:
    """'13:05' や ISO8601 を tz 付き datetime へ。不正なら 400。"""
    try:
        return timeutil.parse_hhmm(text, base)
    except (ValueError, TypeError):
        raise _ApiError(400, f"{label} の時刻形式が不正: {text}") from None


def _required_time(body: dict, key: str, base: date) -> datetime:
    value = _text(body, key)
    if not value:
        raise _ApiError(400, f"{key} は必須")
    return _parse_time(value, base, key)


def _optional_time(body: dict, key: str, *, base: date) -> Optional[datetime]:
    value = _text(body, key)
    return _parse_time(value, base, key) if value else None


def _parse_iso(text: str, label: str) -> datetime:
    """ISO8601 文字列を tz 付き datetime へ。不正なら 400。

    クエリ文字列で '+09:00' の '+' が空白に化けることがあるため、
    末尾のタイムゾーンオフセットだけ元へ戻してから解釈する。
    """
    normalized = _OFFSET_SPACE_RE.sub(r"\1+\2", text.strip()).replace(" ", "T")
    try:
        return timeutil.ensure_aware(datetime.fromisoformat(normalized))
    except (ValueError, TypeError):
        raise _ApiError(400, f"{label} の日時形式が不正: {text}") from None


def _check_order(start: datetime, end: datetime) -> None:
    """開始 >= 終了 は 400。"""
    if start >= end:
        raise _ApiError(400, "開始時刻は終了時刻より前である必要がある")


def _to_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _ApiError(400, f"日付の形式が不正（YYYY-MM-DD）: {text}") from None


def _query_date(query: dict, key: str) -> date:
    """クエリの日付。省略なら今日。"""
    value = str(query.get(key) or "").strip()
    return _to_date(value) if value else timeutil.today()


def _query_int(query: dict, key: str, default: int) -> int:
    """クエリの整数。省略なら default。不正なら 400。"""
    value = str(query.get(key) or "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise _ApiError(400, f"{key} は整数で指定する: {value}") from None


def _body_date(body: dict, key: str) -> date:
    """本体の日付。省略なら今日。"""
    value = _text(body, key)
    return _to_date(value) if value else timeutil.today()


def _timestamp(body: dict) -> datetime:
    """変化・判断の記録時刻。`date` 省略で今日、`time` 省略で現在時刻。"""
    target = _body_date(body, "date")
    time_text = _text(body, "time")
    if not time_text:
        # 過去日を指定して時刻を省略した場合は、その日の現在時刻とみなす
        current = timeutil.now()
        return current if target == current.date() else _parse_time(
            timeutil.hhmm(current), target, "time"
        )
    return _parse_time(time_text, target, "time")
