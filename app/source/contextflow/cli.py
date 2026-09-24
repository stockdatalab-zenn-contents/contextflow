"""contextflow のコマンドラインインターフェース。

各実装モジュールはコマンド関数の中で遅延 import する。
一部の機能（LLM など）が使えなくても、他のコマンドは動くようにするため。

使い方: プロジェクトルートで `python app/cf.py <command>`
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from contextflow import timeutil
from contextflow.config import AppConfig, load_config, load_question_sets
from contextflow.contracts.models import (
    Activity,
    ActivityType,
    Change,
    Decision,
    Source,
    Task,
    TaskStatus,
)
from contextflow.contracts.serde import to_jsonable


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------


def _bootstrap(args: argparse.Namespace):
    """設定と DB を用意する。"""
    from contextflow.storage.db import open_database

    config = load_config(Path(args.config) if getattr(args, "config", None) else None)
    database = open_database(config)
    return config, database


def _target_date(args: argparse.Namespace) -> date:
    value = getattr(args, "date", None)
    return date.fromisoformat(value) if value else timeutil.today()


def _echo(text: str) -> None:
    print(text)


def _echo_json(value: Any) -> None:
    print(json.dumps(to_jsonable(value), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# init / collect
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """DB と長期コンテキスト用ディレクトリを作る。"""
    from contextflow.sources.github_context import ContextRepo

    config, database = _bootstrap(args)
    _echo(f"DB を初期化: {database.path}")
    repo = ContextRepo(config.path("context_repo"), config)
    repo.ensure_layout()
    layout = config.get("context_repo.layout", "work_repo")
    _echo(f"長期コンテキスト: {repo.root}（layout={layout}）")
    if layout == "work_repo":
        projects = repo.projects()
        if projects:
            _echo(f"  案件 {len(projects)} 件を認識: " + ", ".join(p.key for p in projects[:5]))
        else:
            _echo(
                "  案件フォルダが見つからない。"
                "既存の仕事管理リポジトリを指すか、layout を standalone にする。"
            )
    for key in ("export_dir", "calendar_dir"):
        config.path(key).mkdir(parents=True, exist_ok=True)
    database.close()
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """前面ウィンドウを収集して SQLite へ記録。"""
    from contextflow.collector.collector import Collector
    from contextflow.collector.win32 import is_supported

    config, database = _bootstrap(args)
    if not is_supported():
        _echo("Windows 以外では収集できない。")
        return 1
    collector = Collector(config, database)
    if args.once:
        event = collector.sample()
        _echo_json(event)
        database.close()
        return 0
    interval = config.get("collector.interval_sec", 5)
    _echo(f"収集開始（間隔 {interval} 秒）。Ctrl+C で終了。")
    count = collector.run(duration_sec=args.duration)
    _echo(f"収集件数: {count}")
    database.close()
    return 0


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def cmd_sessionize(args: argparse.Namespace) -> int:
    """生ログを session へ圧縮。"""
    from contextflow.pipeline.p10_sessionizer import sessionize_day

    config, database = _bootstrap(args)
    target = _target_date(args)
    sessions = sessionize_day(database, target, config)
    _echo(f"{target}: session {len(sessions)} 件")
    for session in sessions[:20]:
        _echo(
            f"  {timeutil.hhmm(session.start_at)}-{timeutil.hhmm(session.end_at)} "
            f"{session.process} ({timeutil.fmt_minutes(session.duration_min)}) {session.window_title[:40]}"
        )
    database.close()
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """session・手入力・カレンダーを統合して確定 activity を作る。"""
    from contextflow.pipeline.p30_activity_builder import build_day

    config, database = _bootstrap(args)
    target = _target_date(args)
    activities = build_day(database, target, config)
    _echo(f"{target}: activity {len(activities)} 件")
    for activity in activities[:30]:
        _echo(
            f"  {timeutil.hhmm(activity.start_at)}-{timeutil.hhmm(activity.end_at)} "
            f"{activity.activity_type.value:14s} {activity.source.value:8s} "
            f"conf={activity.confidence:.2f} {activity.project or '-'} {activity.summary[:30]}"
        )
    database.close()
    return 0


# ---------------------------------------------------------------------------
# 手入力（非PC作業）
# ---------------------------------------------------------------------------


def _activity_type(text: str) -> ActivityType:
    try:
        return ActivityType(text)
    except ValueError:
        choices = ", ".join(t.value for t in ActivityType)
        raise SystemExit(f"不正な activity_type: {text}（選択肢: {choices}）")


def _activities_summary(activities: list[Activity]) -> str:
    """work stop / work add の結果表示用に1行へまとめる。

    日をまたいで複数件に分割された場合は、合計時間に加えて分割された旨を添える
    （種別は分割区間すべてで共通のため先頭の1件から取る）。
    """
    total_min = sum(a.duration_min for a in activities)
    text = f"{activities[0].activity_type.value} {timeutil.fmt_minutes(total_min)}"
    if len(activities) >= 2:
        text += f"（日をまたぐため{len(activities)}件に分割）"
    return text


def cmd_work(args: argparse.Namespace) -> int:
    """会議・思考・相談など、PCに残らない作業を記録。"""
    from contextflow.sources.manual import ManualInput

    config, database = _bootstrap(args)
    manual = ManualInput(database)

    if args.work_command == "start":
        at = timeutil.parse_hhmm(args.at) if args.at else None
        manual.start(
            _activity_type(args.activity_type), project=args.project, task=args.task, at=at
        )
        _echo(f"開始: {args.activity_type} / {args.project or '-'} / {args.task or '-'}")

    elif args.work_command == "stop":
        at = timeutil.parse_hhmm(args.at) if args.at else None
        was_running = manual.running() is not None
        activities = manual.stop(at=at, summary=args.summary or "")
        if not activities:
            # 実行中が無かったのか、開始直後で取り消したのかを区別する
            _echo("開始直後のため記録せず取り消した。" if was_running else "実行中の作業なし。")
        else:
            _echo(f"終了: {_activities_summary(activities)}")

    elif args.work_command == "add":
        base = _target_date(args)
        activities = manual.add(
            timeutil.parse_hhmm(args.start, base),
            timeutil.parse_hhmm(args.end, base),
            _activity_type(args.activity_type),
            project=args.project,
            task=args.task,
            summary=args.summary or "",
        )
        _echo(f"追加: {_activities_summary(activities)}")

    elif args.work_command == "status":
        running = manual.running()
        _echo_json(running) if running else _echo("実行中の作業なし。")

    elif args.work_command == "say":
        from contextflow.planner.planner import Planner

        activity = Planner(config).parse_activity_text(args.text, base_date=_target_date(args))
        if activity is None:
            _echo("自然文の解釈に失敗（LLM 未設定の可能性）。work add を使う。")
            database.close()
            return 1
        manual.add(
            activity.start_at,
            activity.end_at,
            activity.activity_type,
            project=activity.project,
            task=activity.task,
            summary=activity.summary,
        )
        _echo_json(activity)

    database.close()
    return 0


# ---------------------------------------------------------------------------
# カレンダー
# ---------------------------------------------------------------------------


def cmd_calendar(args: argparse.Namespace) -> int:
    """カレンダーを予定（planned）として取り込む。"""
    config, database = _bootstrap(args)

    if args.calendar_command == "status":
        from contextflow.sources.calendar_ics import IcsFileProvider

        _echo("取得元の状態:")
        try:
            from contextflow.sources.calendar_outlook import OutlookComProvider

            status = OutlookComProvider().check()
            mark = "利用可" if status.available else "利用不可"
            _echo(f"  outlook : {mark} — {status.message}")
        except Exception as error:  # モジュールごと読めない場合も落とさない
            _echo(f"  outlook : 利用不可 — {error}")
        ics_status = IcsFileProvider(config).check()
        _echo(f"  ics     : {'利用可' if ics_status.available else '利用不可'} — {ics_status.message}")
        _echo(f"既定の取得元: {config.get('calendar.provider', 'outlook')}")
        _echo(
            f"取得範囲: 前 {config.get('calendar.fetch_days_back', 1)} 日 〜 "
            f"先 {config.get('calendar.fetch_days', 7)} 日"
        )
        database.close()
        return 0

    if args.calendar_command == "import":
        from contextflow.sources.calendar_ics import import_ics_dir

        target = _target_date(args) if getattr(args, "date", None) else None
        activities = import_ics_dir(database, config, target)
        _echo(f"取り込んだ予定: {len(activities)} 件")
        for activity in activities[:20]:
            _echo(
                f"  {activity.start_at:%m/%d} {timeutil.hhmm(activity.start_at)}-"
                f"{timeutil.hhmm(activity.end_at)} {activity.summary}"
            )
        database.close()
        return 0

    # sync: 取得元（既定は classic Outlook）から直近1週間ぶんを取り直す
    from contextflow.sources.calendar_sync import create_provider, sync_calendar

    provider = create_provider(config, getattr(args, "provider", None))
    try:
        result = sync_calendar(
            database,
            config,
            provider=provider,
            days=args.days,
            days_back=args.days_back,
        )
    except RuntimeError as error:
        _echo(f"取得に失敗: {error}")
        _echo("取得元の状態は `calendar status` で確認する。")
        database.close()
        return 1

    _echo(
        f"取得元={result.provider}  範囲={result.start:%m/%d %H:%M} 〜 {result.end:%m/%d %H:%M}"
    )
    _echo(f"取得 {result.fetched} 件 / 保存 {len(result.saved)} 件")
    for reason, count in sorted(result.rejected.items()):
        _echo(f"  除外 {reason}: {count} 件")
    for activity in result.saved[:20]:
        _echo(
            f"  {activity.start_at:%m/%d} {timeutil.hhmm(activity.start_at)}-"
            f"{timeutil.hhmm(activity.end_at)} {activity.summary}"
        )
    if len(result.saved) > 20:
        _echo(f"  ... 他 {len(result.saved) - 20} 件")
    database.close()
    return 0


# ---------------------------------------------------------------------------
# 状態・判断・計画
# ---------------------------------------------------------------------------


def _build_state(config: AppConfig, database, args: argparse.Namespace):
    from contextflow.context.builder import ContextBuilder

    builder = ContextBuilder(database, config)
    return builder, builder.build(_target_date(args))


def _day_activities(database, start: datetime, end: datetime):
    """その日の確定 activity を返す。

    層を指定せずに読むと、確定(confirmed)と元データ(reported/observed/planned)が
    両方返って二重計上になる。必ず1つの層だけを返し、
    確定が無い日は observed → reported → planned の順で1層だけ拾う。
    """
    from contextflow.contracts.models import ActivityLayer
    from contextflow.storage.repositories import ActivityRepository

    repository = ActivityRepository(database)
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


def cmd_state(args: argparse.Namespace) -> int:
    """Current State を組み立てて表示・保存。"""
    config, database = _bootstrap(args)
    builder, state = _build_state(config, database, args)
    if args.json:
        _echo_json(state)
    else:
        _echo(f"# {state.target_date} {state.now_time}")
        _echo(f"合計 {timeutil.fmt_minutes(state.today.total_min)}")
        for key, minutes in sorted(state.today.by_type.items(), key=lambda x: -x[1]):
            _echo(f"  {key}: {timeutil.fmt_minutes(minutes)}")
        _echo(
            f"deep work {timeutil.fmt_minutes(state.features.deep_work_min)} / "
            f"切替 {state.features.context_switches} 回"
        )
        _echo(f"現在タスク: {state.current_task.title if state.current_task else '-'}")
        _echo(f"未完了 {state.open_tasks} 件 / blocked {state.blocked_tasks} 件")
    path = builder.save_json(state)
    _echo(f"state を保存: {path}")
    database.close()
    return 0


def _ask_decision(config: AppConfig, database, args: argparse.Namespace):
    """state を作り、Decision Engine へ質問する。"""
    from contextflow.context.builder import state_to_flat_dict
    from contextflow.contracts.decision import DecisionRequest
    from contextflow.storage.repositories import DecisionLogRepository

    _builder, state = _build_state(config, database, args)
    question_set = args.set or config.get("decision.question_set", "next_action")
    # --config で別ディレクトリを指した場合も questions.toml を追従させる
    questions = load_question_sets(config.sibling("questions.toml")).get(question_set)
    if not questions:
        raise SystemExit(f"質問セットが無い: {question_set}")
    engine = _engine_for(config, database, args)
    request = DecisionRequest(state=state_to_flat_dict(state), questions=questions)
    response = _ask_with_fallback(engine, request, config, database)
    DecisionLogRepository(database).add_response(request, response)
    return state, response


def _engine_for(config: AppConfig, database, args: argparse.Namespace):
    """運用方針（decision.mode）に沿った判断エンジンを作る。

    --engine が指定されたときだけ単一エンジンを直接使う。
    """
    from contextflow.decision.registry import create_engine, create_engine_chain

    name = getattr(args, "engine", None)
    if name:
        return create_engine(config, database, name=name)
    return create_engine_chain(config, database, mode=getattr(args, "mode", None))


def _ask_with_fallback(engine, request, config: AppConfig, database):
    """最後の保険。チェーンが全滅しても判断を止めない。"""
    from contextflow.decision.registry import create_engine

    try:
        return engine.ask(request)
    except RuntimeError as error:
        print(f"警告: {error} → rule_based へ退避", file=sys.stderr)
        return create_engine(config, database, name="rule_based").ask(request)


def cmd_decide(args: argparse.Namespace) -> int:
    """Jev風 Decision Engine に小さな判断をさせる。"""
    config, database = _bootstrap(args)
    state, response = _ask_decision(config, database, args)
    threshold = float(config.get("decision.confidence_threshold", 0.7))
    _echo(f"engine={response.engine} ({response.latency_ms} ms)")
    for key, answer in response.answers.items():
        mark = "  " if answer.confidence >= threshold else "? "
        _echo(f"{mark}{key}: {answer.value}  conf={answer.confidence:.2f}  {answer.rationale}")
    _echo(f"（conf < {threshold} の項目は保留扱い）")
    database.close()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """ブラウザから確認・記録するためのローカルサーバを起動する。

    手動起動。127.0.0.1 固定でバインドし、外部からは触れない。
    """
    from contextflow.ui.server import serve

    config, database = _bootstrap(args)
    database.close()  # サーバ側はリクエストごとに開き直すため、ここでは閉じる
    return serve(config, port=args.port, open_browser=not args.no_browser)


def cmd_mode(args: argparse.Namespace) -> int:
    """運用方針の一覧と、現在の選択を表示。"""
    from contextflow.config import load_modes, resolve_mode

    config, database = _bootstrap(args)
    database.close()
    current = resolve_mode(config)
    _echo(f"現在の運用方針: {current.name}")
    _echo(f"  判断エンジン: {' → '.join(current.engines)}（左から順に試し、失敗したら次へ退避）")
    _echo(f"  Planner     : {current.planner}")
    _echo("")
    _echo("選べる運用方針:")
    for name, mode in sorted(load_modes(config).items()):
        mark = "*" if name == current.name else " "
        _echo(f" {mark} {name:11s} {' → '.join(mode.engines):38s} planner={mode.planner}")
        if mode.description:
            _echo(f"     {mode.description}")
    _echo("")
    _echo(f"変更は {config.config_dir / 'config.toml'} の [decision] mode を書き換える。")
    _echo("一時的に変えるだけなら decide / plan / gaps に --mode を付ける。")
    return 0


def cmd_gaps(args: argparse.Namespace) -> int:
    """PC操作が無い空白時間を洗い出し、その種別を推定する。

    PC idle をそのまま「休憩」とは判定しない。予定の有無と前後の活動から
    Decision Engine に推定させ、確信度が閾値を超えたものだけ記録する。
    """
    from contextflow.contracts.decision import DecisionRequest
    from contextflow.contracts.models import (
        Activity,
        ActivityLayer,
        ActivityType,
        Source,
    )
    from contextflow.pipeline.p30_activity_builder import find_gaps
    from contextflow.storage.repositories import ActivityRepository

    config, database = _bootstrap(args)
    target = _target_date(args)
    start, end = timeutil.day_range(target)
    activities = _day_activities(database, start, end)
    if len(activities) < 2:
        _echo("活動が足りないため空白時間を判定できない。先に build を実行する。")
        database.close()
        return 1

    # 空白は「実際の証拠（PCログ・手入力）が無い区間」。予定由来は証拠ではないので除く
    evidence = [a for a in activities if a.source is not Source.CALENDAR]
    if not evidence:
        _echo("PCログ・手入力の実績が無いため空白時間を判定できない。")
        database.close()
        return 1

    # 対象は「最初の活動〜最後の活動」の間だけ。就寝時間を空白と呼ばないため
    span_start = min(activity.start_at for activity in evidence)
    span_end = max(activity.end_at for activity in evidence)
    gaps = find_gaps(evidence, span_start, span_end, min_gap_sec=args.min_gap_min * 60)
    if not gaps:
        _echo("空白時間なし。")
        database.close()
        return 0

    planned = ActivityRepository(database).list_between(start, end, layer=ActivityLayer.PLANNED)
    questions = load_question_sets(config.sibling("questions.toml"))["gap_fill"]
    engine = _engine_for(config, database, args)
    threshold = float(config.get("decision.confidence_threshold", 0.7))
    applied = 0

    # 予定の境界で空白を切り分ける。長い空白を丸ごと1種別と断定しないため
    segments: list[tuple[datetime, datetime]] = []
    for gap_start, gap_end in gaps:
        segments.extend(_split_by_events(gap_start, gap_end, planned))

    for gap_start, gap_end in segments:
        has_event = any(
            timeutil.overlap_sec(gap_start, gap_end, item.start_at, item.end_at) > 0
            for item in planned
        )
        # 判断材料は集計値のみ。予定名やウィンドウタイトルは渡さない
        state = {
            "date": target.isoformat(),
            "gap_start": timeutil.hhmm(gap_start),
            "gap_end": timeutil.hhmm(gap_end),
            "gap_min": timeutil.minutes_between(gap_start, gap_end),
            "gap_has_calendar_event": has_event,
            "prev_activity_type": _neighbor_type(activities, gap_start, before=True),
            "next_activity_type": _neighbor_type(activities, gap_end, before=False),
        }
        response = _ask_with_fallback(
            engine, DecisionRequest(state=state, questions=questions), config, database
        )
        kind = response.value("gap_activity_type")
        is_work = response.value("gap_is_work")
        confidence = response.confidence("gap_activity_type")
        mark = "  " if confidence >= threshold else "? "
        _echo(
            f"{mark}{state['gap_start']}-{state['gap_end']} "
            f"({timeutil.fmt_minutes(state['gap_min'])}) 推定={kind} "
            f"仕事={is_work} conf={confidence:.2f} 予定={'あり' if has_event else 'なし'}"
        )
        if args.apply and confidence >= threshold and kind and kind != "unknown":
            # AI推定として記録。人の手入力で後から上書きできるよう source で区別する
            ActivityRepository(database).add(
                Activity(
                    start_at=gap_start,
                    end_at=gap_end,
                    activity_type=ActivityType(kind),
                    layer=ActivityLayer.REPORTED,
                    source=Source.SYSTEM,
                    confidence=confidence,
                    summary="空白時間の推定",
                )
            )
            applied += 1

    if args.apply:
        _echo(f"記録した推定: {applied} 件（build を再実行して確定へ反映する）")
    else:
        _echo("記録するには --apply を付ける。")
    database.close()
    return 0


def _split_by_events(
    start: datetime, end: datetime, events, *, min_sec: int = 60
) -> list[tuple[datetime, datetime]]:
    """区間を予定の開始・終了時刻で切り分ける。"""
    points = {start, end}
    for event in events:
        for edge in (event.start_at, event.end_at):
            if start < edge < end:
                points.add(edge)
    ordered = sorted(points)
    segments = [
        (ordered[index], ordered[index + 1])
        for index in range(len(ordered) - 1)
        if (ordered[index + 1] - ordered[index]).total_seconds() >= min_sec
    ]
    return segments


def _neighbor_type(activities, at: datetime, *, before: bool) -> str:
    """空白の直前・直後の活動種別を返す。無ければ 'none'。"""
    if before:
        candidates = [a for a in activities if a.end_at <= at]
        target = max(candidates, key=lambda a: a.end_at, default=None)
    else:
        candidates = [a for a in activities if a.start_at >= at]
        target = min(candidates, key=lambda a: a.start_at, default=None)
    return target.activity_type.value if target else "none"


def cmd_plan(args: argparse.Namespace) -> int:
    """判断結果をもとに、人間向けの説明・計画を作る。"""
    from contextflow.planner.planner import Planner

    config, database = _bootstrap(args)
    state, response = _ask_decision(config, database, args)
    _echo(Planner(config, mode=getattr(args, "mode", None)).make_plan(state, response))
    database.close()
    return 0


# ---------------------------------------------------------------------------
# 変化・判断・タスク
# ---------------------------------------------------------------------------


def cmd_change(args: argparse.Namespace) -> int:
    """状況の変化を記録。"""
    from contextflow.storage.repositories import ChangeRepository

    _config, database = _bootstrap(args)
    repo = ChangeRepository(database)
    if args.change_command == "add":
        repo.add(Change(ts=timeutil.now(), description=args.text, project=args.project))
        _echo("変化を記録。")
    else:
        for change in repo.recent(limit=args.limit):
            _echo(f"{change.ts:%Y-%m-%d %H:%M} [{change.project or '-'}] {change.description}")
    database.close()
    return 0


def cmd_decision(args: argparse.Namespace) -> int:
    """人が下した判断を記録。"""
    from contextflow.storage.repositories import DecisionRepository

    _config, database = _bootstrap(args)
    repo = DecisionRepository(database)
    if args.decision_command == "add":
        repo.add(
            Decision(
                ts=timeutil.now(),
                decision=args.text,
                reason=args.reason or "",
                project=args.project,
            )
        )
        _echo("判断を記録。")
    else:
        for item in repo.recent(limit=args.limit):
            reason = f"（{item.reason}）" if item.reason else ""
            _echo(f"{item.ts:%Y-%m-%d %H:%M} [{item.project or '-'}] {item.decision}{reason}")
    database.close()
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    """タスクの追加・一覧・状態変更。"""
    from contextflow.storage.repositories import TaskRepository

    _config, database = _bootstrap(args)
    repo = TaskRepository(database)
    if args.task_command == "add":
        repo.upsert(
            Task(
                title=args.title,
                project=args.project,
                priority=args.priority,
                deadline=date.fromisoformat(args.deadline) if args.deadline else None,
                updated_at=timeutil.now(),
            )
        )
        _echo("タスクを追加。")
    elif args.task_command == "set":
        task = repo.find_by_title(args.title, args.project)
        if task is None and args.project is None:
            # --project 省略時は、タイトルが一意ならプロジェクト指定なしでも拾う
            matched = [t for t in repo.list() if t.title == args.title]
            if len(matched) == 1:
                task = matched[0]
            elif len(matched) > 1:
                projects = ", ".join(str(t.project) for t in matched)
                _echo(f"同名タスクが複数ある。--project を指定する（候補: {projects}）")
                database.close()
                return 1
        if task is None:
            _echo("該当タスクなし。")
            database.close()
            return 1
        task.status = TaskStatus(args.status)
        task.blocked = task.status is TaskStatus.BLOCKED
        task.updated_at = timeutil.now()
        repo.upsert(task)
        _echo(f"状態を {args.status} へ変更。")
    else:
        status = TaskStatus(args.status) if getattr(args, "status", None) else None
        for task in repo.list(status=status):
            flag = "!" if task.blocked else " "
            deadline = task.deadline.isoformat() if task.deadline else "-"
            _echo(
                f"{flag} p{task.priority} [{task.status.value:11s}] "
                f"{task.title}  ({task.project or '-'}, 期限 {deadline})"
            )
    database.close()
    return 0


# ---------------------------------------------------------------------------
# レポート・GitHub
# ---------------------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    """日次サマリを Markdown で出力。"""
    from contextflow.report.daily_markdown import write_daily
    from contextflow.storage.repositories import ChangeRepository, DecisionRepository

    config, database = _bootstrap(args)
    target = _target_date(args)
    start, end = timeutil.day_range(target)
    _builder, state = _build_state(config, database, args)
    activities = _day_activities(database, start, end)
    changes = ChangeRepository(database).list_between(start, end)
    decisions = DecisionRepository(database).list_between(start, end)
    path = write_daily(config, state, activities, changes, decisions)
    _echo(f"出力: {path}")
    if args.show:
        _echo(path.read_text(encoding="utf-8"))
    database.close()
    return 0


def _note_written(
    written: list[Path], path: Optional[Path], project: Optional[str], kind: str, text: str
) -> None:
    """追記結果を集計しつつ、案件が特定できなかったものを知らせる。"""
    if path is None:
        _echo(f"  {kind}を書けない（案件 '{project or '未指定'}' が見つからない）: {text[:40]}")
        return
    if path not in written:
        written.append(path)


def cmd_github(args: argparse.Namespace) -> int:
    """長期コンテキスト（Markdown）と GitHub の同期。

    リポジトリ未作成でも動く。作成後は config の github.repo / remote を埋めるだけで
    Issue 取り込みと push が有効になる。
    """
    from contextflow.sources.git_sync import open_git_sync
    from contextflow.sources.github_context import ContextRepo, GitHubIssues

    config, database = _bootstrap(args)

    if args.github_command == "status":
        status = open_git_sync(config).status()
        _echo(f"context_repo : {config.path('context_repo')}")
        _echo(f"git          : {'利用可' if status.available else '利用不可'}")
        _echo(f"リポジトリ   : {'初期化済み' if status.is_repo else '未初期化（github init-repo）'}")
        _echo(f"ブランチ     : {status.branch or '-'}")
        _echo(f"remote       : {status.remote or '未設定（config の github.remote）'}")
        _echo(f"未コミット   : {status.dirty} 件")
        token = config.secret("github.token_env")
        repo = config.get("github.repo", "")
        _echo(f"Issue 連携   : repo={repo or '未設定'} / token={'設定済み' if token else '未設定'}")
        _echo(f"状況         : {status.message}")
        database.close()
        return 0

    if args.github_command == "init-repo":
        _echo(open_git_sync(config).init())
        database.close()
        return 0

    if args.github_command == "push":
        _echo(open_git_sync(config).sync(args.message or "contextflow: 長期コンテキスト更新"))
        database.close()
        return 0

    if args.github_command == "pull":
        issues = GitHubIssues(config.get("github.repo", ""), config.secret("github.token_env"))
        if issues.enabled and config.get("github.issue_sync", True):
            count = issues.sync_to_db(database)
            _echo(f"Issue を {count} 件取り込み。")
        else:
            # GitHub 未設定なら、ローカルの projects/*/tasks.md からタスクを読む
            from contextflow.storage.repositories import TaskRepository

            repo = ContextRepo(config.path("context_repo"), config)
            tasks = repo.read_tasks()
            task_repository = TaskRepository(database)
            for task in tasks:
                task.updated_at = timeutil.now()
                task_repository.upsert(task)
            _echo(
                f"GitHub 未設定のため tasks.md から {len(tasks)} 件取り込み"
                "（Issue 連携は config.github.repo と token を設定する）。"
            )
    else:
        target = _target_date(args)
        start, end = timeutil.day_range(target)
        from contextflow.report.daily_markdown import render_daily
        from contextflow.storage.repositories import ChangeRepository, DecisionRepository

        _builder, state = _build_state(config, database, args)
        repo = ContextRepo(config.path("context_repo"), config)
        repo.ensure_layout()
        written: list[Path] = []

        changes = ChangeRepository(database).list_between(start, end)
        decisions = DecisionRepository(database).list_between(start, end)

        # 判断・変化は案件の decisions.md へ追記する（時間情報ではないため方針に反しない）
        for change in changes:
            path = repo.append_change(change.project or "", target, change.description)
            _note_written(written, path, change.project, "変化", change.description)
        for decision in decisions:
            text = decision.decision
            if decision.reason:
                text = f"{text}（理由: {decision.reason}）"
            path = repo.append_decision(decision.project or "", target, text)
            _note_written(written, path, decision.project, "判断", decision.decision)

        # 日次の時間集計は、リポジトリの「実施日時は管理しない」方針により既定で書き出さない
        body = render_daily(state, _day_activities(database, start, end), changes, decisions)
        daily_path = repo.write_daily(target, body)
        if daily_path is None:
            _echo(
                "日次サマリはリポジトリへ書き出さない設定"
                "（[context_repo] write_daily = false）。ローカルの report を使う。"
            )
        else:
            written.append(daily_path)
            _echo(f"日次サマリ: {daily_path}")

        _echo(f"書き出したファイル: {len(written)} 件")
        if args.push or config.get("github.auto_push", False):
            # contextflow が書いたファイルだけを commit する（人の編集を巻き込まないため）
            _echo(open_git_sync(config).sync(f"contextflow: {target} の記録", written))
    database.close()
    return 0


# ---------------------------------------------------------------------------
# フィードバック・校正
# ---------------------------------------------------------------------------


def cmd_feedback(args: argparse.Namespace) -> int:
    """AI判断の答え合わせを記録（校正の材料）。"""
    from contextflow.storage.repositories import DecisionLogRepository, FeedbackRepository

    _config, database = _bootstrap(args)
    logs = DecisionLogRepository(database).recent(question_key=args.key, limit=1)
    if not logs:
        _echo(f"'{args.key}' の判断履歴が無い。先に decide を実行する。")
        database.close()
        return 1
    log = logs[0]
    predicted = str(log.get("value"))
    FeedbackRepository(database).add(
        engine=str(log.get("engine")),
        question_key=args.key,
        predicted=predicted,
        actual=args.actual,
        correct=predicted.strip().lower() == args.actual.strip().lower(),
        raw_confidence=float(log.get("raw_confidence", 0.5)),
        decision_log_id=log.get("id"),
        note=args.note or "",
    )
    _echo(f"記録: 予測={predicted} / 実際={args.actual}")
    database.close()
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """蓄積したフィードバックから confidence を校正。"""
    from contextflow.decision.calibration import fit_all

    _config, database = _bootstrap(args)
    results = fit_all(database, min_samples=args.min_samples)
    if not results:
        _echo("校正に足るサンプルが無い。feedback を貯める。")
    for item in results:
        _echo(
            f"{item.get('engine')}/{item.get('question_key')}: "
            f"{item.get('method')} (n={item.get('sample_count')})"
        )
    database.close()
    return 0


# ---------------------------------------------------------------------------
# パーサ
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cf", description="contextflow: 操作ログと長期コンテキストから判断を支援する"
    )
    parser.add_argument("--config", help="config.toml のパス")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="DB とディレクトリを初期化").set_defaults(func=cmd_init)

    collect = sub.add_parser("collect", help="前面ウィンドウを収集")
    collect.add_argument("--duration", type=int, help="収集する秒数（省略で無限）")
    collect.add_argument("--once", action="store_true", help="1回だけ採取して表示")
    collect.set_defaults(func=cmd_collect)

    sessionize = sub.add_parser("sessionize", help="生ログを session へ圧縮")
    sessionize.add_argument("--date", help="対象日 YYYY-MM-DD")
    sessionize.set_defaults(func=cmd_sessionize)

    build = sub.add_parser("build", help="確定 activity を組み立て")
    build.add_argument("--date", help="対象日 YYYY-MM-DD")
    build.set_defaults(func=cmd_build)

    work = sub.add_parser("work", help="非PC作業の手入力")
    work_sub = work.add_subparsers(dest="work_command", required=True)
    w_start = work_sub.add_parser("start", help="作業開始")
    w_start.add_argument("activity_type")
    w_start.add_argument("--project")
    w_start.add_argument("--task")
    w_start.add_argument("--at", help="開始時刻 HH:MM")
    w_stop = work_sub.add_parser("stop", help="作業終了")
    w_stop.add_argument("--at", help="終了時刻 HH:MM")
    w_stop.add_argument("--summary")
    w_add = work_sub.add_parser("add", help="後から時間帯を追加")
    w_add.add_argument("start", help="開始 HH:MM")
    w_add.add_argument("end", help="終了 HH:MM")
    w_add.add_argument("activity_type")
    w_add.add_argument("--project")
    w_add.add_argument("--task")
    w_add.add_argument("--summary")
    w_add.add_argument("--date", help="対象日 YYYY-MM-DD")
    work_sub.add_parser("status", help="実行中の作業を表示")
    w_say = work_sub.add_parser("say", help="自然文から記録（LLM）")
    w_say.add_argument("text")
    w_say.add_argument("--date", help="対象日 YYYY-MM-DD")
    work.set_defaults(func=cmd_work)

    calendar = sub.add_parser("calendar", help="カレンダーを予定として取り込み")
    calendar_sub = calendar.add_subparsers(dest="calendar_command", required=True)
    cal_sync = calendar_sub.add_parser("sync", help="取得元から直近1週間ぶんを取り直す")
    cal_sync.add_argument("--provider", choices=["outlook", "ics"], help="取得元を上書き")
    cal_sync.add_argument("--days", type=int, help="今日から何日先まで取るか（既定7）")
    cal_sync.add_argument(
        "--days-back", type=int, dest="days_back", help="何日前まで取り直すか（既定1）"
    )
    cal_import = calendar_sub.add_parser("import", help=".ics ファイルから取り込む")
    cal_import.add_argument("--date", help="対象日 YYYY-MM-DD")
    calendar_sub.add_parser("status", help="取得元の利用可否を表示")
    calendar.set_defaults(func=cmd_calendar)

    state = sub.add_parser("state", help="Current State を作る")
    state.add_argument("--date", help="対象日 YYYY-MM-DD")
    state.add_argument("--json", action="store_true")
    state.set_defaults(func=cmd_state)

    decide = sub.add_parser("decide", help="Jev風 Decision Engine に判断させる")
    decide.add_argument("--set", help="質問セット名（questions.toml）")
    decide.add_argument("--mode", help="運用方針を一時的に上書き（rule_first 等）")
    decide.add_argument("--engine", help="rule_based / claude / openai_compat / jev")
    decide.add_argument("--date", help="対象日 YYYY-MM-DD")
    decide.set_defaults(func=cmd_decide)

    gaps = sub.add_parser("gaps", help="空白時間を洗い出して種別を推定")
    gaps.add_argument("--date", help="対象日 YYYY-MM-DD")
    gaps.add_argument("--mode", help="運用方針を一時的に上書き")
    gaps.add_argument("--engine")
    gaps.add_argument("--min-gap-min", type=int, default=10, dest="min_gap_min")
    gaps.add_argument("--apply", action="store_true", help="推定結果を記録する")
    gaps.set_defaults(func=cmd_gaps)

    plan = sub.add_parser("plan", help="判断結果から計画・説明を作る")
    plan.add_argument("--set", help="質問セット名")
    plan.add_argument("--mode", help="運用方針を一時的に上書き")
    plan.add_argument("--engine")
    plan.add_argument("--date", help="対象日 YYYY-MM-DD")
    plan.set_defaults(func=cmd_plan)

    change = sub.add_parser("change", help="状況の変化")
    change_sub = change.add_subparsers(dest="change_command", required=True)
    c_add = change_sub.add_parser("add")
    c_add.add_argument("text")
    c_add.add_argument("--project")
    c_list = change_sub.add_parser("list")
    c_list.add_argument("--limit", type=int, default=10)
    change.set_defaults(func=cmd_change)

    decision = sub.add_parser("decision", help="人が下した判断")
    decision_sub = decision.add_subparsers(dest="decision_command", required=True)
    d_add = decision_sub.add_parser("add")
    d_add.add_argument("text")
    d_add.add_argument("--reason")
    d_add.add_argument("--project")
    d_list = decision_sub.add_parser("list")
    d_list.add_argument("--limit", type=int, default=10)
    decision.set_defaults(func=cmd_decision)

    task = sub.add_parser("task", help="タスク")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    t_add = task_sub.add_parser("add")
    t_add.add_argument("title")
    t_add.add_argument("--project")
    t_add.add_argument("--priority", type=int, default=3)
    t_add.add_argument("--deadline", help="YYYY-MM-DD")
    t_set = task_sub.add_parser("set")
    t_set.add_argument("title")
    t_set.add_argument("status", choices=[s.value for s in TaskStatus])
    t_set.add_argument("--project")
    t_list = task_sub.add_parser("list")
    t_list.add_argument("--status", choices=[s.value for s in TaskStatus])
    task.set_defaults(func=cmd_task)

    ui = sub.add_parser("ui", help="ブラウザから確認・記録する（ローカル起動）")
    ui.add_argument("--port", type=int, default=8765, help="待ち受けポート（既定 8765）")
    ui.add_argument(
        "--no-browser", action="store_true", dest="no_browser", help="ブラウザを自動で開かない"
    )
    ui.set_defaults(func=cmd_ui)

    sub.add_parser("mode", help="運用方針（判断エンジンの並び）を表示").set_defaults(func=cmd_mode)

    report = sub.add_parser("report", help="日次サマリ Markdown")
    report.add_argument("--date", help="対象日 YYYY-MM-DD")
    report.add_argument("--show", action="store_true", help="内容も表示")
    report.set_defaults(func=cmd_report)

    github = sub.add_parser("github", help="長期コンテキストとの同期")
    github_sub = github.add_subparsers(dest="github_command", required=True)
    github_sub.add_parser("status", help="git とIssue連携の設定状況を表示")
    github_sub.add_parser("init-repo", help="context_repo を git リポジトリにする")
    github_sub.add_parser("pull", help="Issue（未設定なら tasks.md）をタスクとして取り込み")
    g_export = github_sub.add_parser("export", help="日次サマリ等を書き出し")
    g_export.add_argument("--date", help="対象日 YYYY-MM-DD")
    g_export.add_argument("--push", action="store_true", help="書き出し後に commit & push")
    g_push = github_sub.add_parser("push", help="commit して push")
    g_push.add_argument("-m", "--message", help="コミットメッセージ")
    github.set_defaults(func=cmd_github)

    feedback = sub.add_parser("feedback", help="AI判断の答え合わせ")
    feedback.add_argument("key", help="質問 key")
    feedback.add_argument("actual", help="実際の値")
    feedback.add_argument("--note")
    feedback.set_defaults(func=cmd_feedback)

    calibrate = sub.add_parser("calibrate", help="confidence を校正")
    calibrate.add_argument("--min-samples", type=int, default=20, dest="min_samples")
    calibrate.set_defaults(func=cmd_calibrate)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _echo("\n中断。")
        return 130
    except SystemExit:
        raise
    except Exception as error:  # 想定外は原因を1行で出す
        print(f"エラー: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
