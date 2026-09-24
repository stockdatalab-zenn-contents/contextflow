"""sources/calendar_outlook.py

classic Outlook を「Python 用の予定取得アダプタ」として使う。
普段使いは新 Outlook のまま、Python からは classic Outlook の
`Outlook.Application`（COM / pywin32）経由で予定表だけを読む
（新 Outlook には COM が無いため）。

参照資料: 非公開の検討資料（Outlook 併用方法）§2〜§4

この環境は pywin32 未導入・Outlook 未起動を前提とする。そのため:
  - `win32com` / `pythoncom` は関数内で遅延 import する（未導入でもこのモジュール
    自体の import は成功する）。
  - `check()` は COM オブジェクトを一切生成せず、`winreg`（標準ライブラリ）で
    ProgID の登録有無だけを見る（Outlook を起動させない）。
  - テストは `OutlookComProvider(dispatch=...)` で偽の COM オブジェクトに
    差し替えて行う。実際に Outlook を起動する検証はしない。
"""

from __future__ import annotations

import winreg
from datetime import datetime
from typing import Any, Callable, Optional

from contextflow.contracts.calendar import CalendarEvent, CalendarProvider, ProviderStatus
from contextflow.timeutil import ensure_aware

OL_FOLDER_CALENDAR = 9  # olFolderCalendar

# Outlook.Application の ProgID。レジストリ確認・COM 生成の両方で使う。
_PROG_ID = "Outlook.Application"

# MeetingStatus のうちキャンセル扱いとする値
# 5 = olMeetingCanceled / 7 = olMeetingReceivedAndCanceled
_CANCELLED_MEETING_STATUSES = {5, 7}

_BUSY_STATUS_MAP = {
    0: "free",
    1: "tentative",
    2: "busy",
    3: "oof",
    4: "working_elsewhere",
}

_RESPONSE_STATUS_MAP = {
    0: "none",
    1: "organizer",
    2: "tentative",
    3: "accepted",
    4: "declined",
    5: "not_responded",
}

# Restrict() に渡す日時書式。地域設定に依存するため、まず英語圏書式で試し、
# 例外になったら日本語環境書式で1回だけ再試行する（それでも失敗したら全件走査）。
_RESTRICT_FORMAT_EN = "%m/%d/%Y %I:%M %p"
_RESTRICT_FORMAT_JP = "%Y/%m/%d %H:%M"
_RESTRICT_FORMATS = (_RESTRICT_FORMAT_EN, _RESTRICT_FORMAT_JP)


def format_restrict_datetime(value: datetime, fmt: str = _RESTRICT_FORMAT_EN) -> str:
    """Restrict() の条件式に埋め込む日時文字列を作る。既定は英語圏書式。

    英語圏書式の AM/PM は `%p` を使わず自分で組み立てる。
    `%p` は実行時ロケール（LC_TIME）に依存し、環境によっては「午前」「午後」を返して
    Restrict() が解釈できなくなるため。
    """
    if fmt is _RESTRICT_FORMAT_EN or "%p" in fmt:
        hour24 = value.hour
        meridiem = "AM" if hour24 < 12 else "PM"
        hour12 = hour24 % 12 or 12
        return f"{value.month:02d}/{value.day:02d}/{value.year} {hour12:02d}:{value.minute:02d} {meridiem}"
    return value.strftime(fmt)


def _registry_has_prog_id(prog_id: str) -> bool:
    """`HKEY_CLASSES_ROOT\\<prog_id>\\CLSID` の存在確認のみ行う（COM は生成しない）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{prog_id}\\CLSID"):
            return True
    except OSError:
        return False


def _safe_get(item: Any, attr: str, default: Any = None) -> Any:
    """COM プロパティへ防御的にアクセスする。属性が無い・例外を投げる場合は default。"""
    try:
        return getattr(item, attr)
    except Exception:
        return default


def to_event(item: Any, *, provider_name: str = "outlook") -> Optional[CalendarEvent]:
    """COM の AppointmentItem から CalendarEvent へ変換する。

    属性アクセスは全て防御的（個別に例外を潰して既定値にする）。
    Start / End が取れない項目は None を返し、呼び出し側で捨てる。
    """
    start_raw = _safe_get(item, "Start")
    end_raw = _safe_get(item, "End")
    if start_raw is None or end_raw is None:
        return None
    try:
        start = ensure_aware(start_raw)
        end = ensure_aware(end_raw)
    except Exception:
        # naive/aware の変換に失敗する型（None・不正値など）は取り込まない
        return None

    subject = _safe_get(item, "Subject", "") or ""
    location = _safe_get(item, "Location", "") or ""
    organizer = _safe_get(item, "Organizer", "") or ""
    is_all_day = bool(_safe_get(item, "AllDayEvent", False))
    is_recurring = bool(_safe_get(item, "IsRecurring", False))

    try:
        meeting_status = int(_safe_get(item, "MeetingStatus"))
    except (TypeError, ValueError):
        meeting_status = None
    is_cancelled = meeting_status in _CANCELLED_MEETING_STATUSES

    try:
        busy_status = _BUSY_STATUS_MAP[int(_safe_get(item, "BusyStatus"))]
    except (TypeError, ValueError, KeyError):
        busy_status = "unknown"

    try:
        response_status = _RESPONSE_STATUS_MAP[int(_safe_get(item, "ResponseStatus"))]
    except (TypeError, ValueError, KeyError):
        response_status = "unknown"

    uid = _safe_get(item, "GlobalAppointmentID", "") or ""

    categories_raw = _safe_get(item, "Categories", "") or ""
    categories = [c.strip() for c in categories_raw.split(",") if c.strip()]

    return CalendarEvent(
        start=start,
        end=end,
        subject=str(subject),
        location=str(location),
        organizer=str(organizer),
        is_all_day=is_all_day,
        is_cancelled=is_cancelled,
        is_recurring=is_recurring,
        busy_status=busy_status,
        response_status=response_status,
        uid=str(uid),
        provider=provider_name,
        categories=categories,
    )


def _scan_all_in_range(items: Any, start: datetime, end: datetime) -> list[Any]:
    """Restrict が2書式とも失敗したときのフォールバック。

    全件を走査し、Python 側で [start, end) に重なる予定だけを残す。
    予定表の件数が多いと遅くなるため、Restrict が使える環境ではここへは来ない。
    """
    matched: list[Any] = []
    for item in items:
        item_start = _safe_get(item, "Start")
        item_end = _safe_get(item, "End")
        if item_start is None or item_end is None:
            continue
        try:
            item_start = ensure_aware(item_start)
            item_end = ensure_aware(item_end)
        except Exception:
            continue
        if item_start < end and item_end >= start:
            matched.append(item)
    return matched


def _restrict_or_scan(items: Any, start: datetime, end: datetime) -> Any:
    """Restrict を試し、日時書式起因の失敗なら別書式で1回だけ再試行する。

    2書式とも失敗したら Restrict を諦め、全件走査フォールバックへ落とす。
    """
    for fmt in _RESTRICT_FORMATS:
        restriction = (
            f"[Start] < '{format_restrict_datetime(end, fmt)}' AND "
            f"[End] >= '{format_restrict_datetime(start, fmt)}'"
        )
        try:
            return items.Restrict(restriction)
        except Exception:
            continue
    return _scan_all_in_range(items, start, end)


class OutlookComProvider(CalendarProvider):
    """classic Outlook を COM 経由で読む取得元。"""

    name = "outlook"

    def __init__(
        self, config: Any = None, *, dispatch: Optional[Callable[[str], Any]] = None
    ) -> None:
        # config: 他の取得元（IcsFileProvider / 将来の GraphProvider）と生成の形を揃えるために受ける。
        #         現状この取得元は設定を見ないが、取得元の差し替えを1行で済ませるため引数は残す。
        # dispatch: テスト用の差し替え口。None なら win32com.client.Dispatch を
        #           遅延 import して使う（fetch() 内で行う。check() では使わない）。
        self._config = config
        self._dispatch = dispatch

    def check(self) -> ProviderStatus:
        """利用可否だけを見る。COM オブジェクトは生成しない（Outlook を起動させない）。"""
        try:
            import win32com.client  # noqa: F401  存在確認のみ
        except ImportError:
            return ProviderStatus(False, "pywin32 が未導入。pip install pywin32 を実行する")

        if not _registry_has_prog_id(_PROG_ID):
            return ProviderStatus(
                False,
                "Outlook.Application が未登録。classic Outlook をインストールする",
            )
        return ProviderStatus(True, "classic Outlook (COM) が利用可能")

    def fetch(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """[start, end) に重なる予定を classic Outlook から取得する。"""
        dispatch = self._dispatch
        if dispatch is None:
            try:
                import win32com.client
            except ImportError as exc:
                raise RuntimeError(
                    "pywin32 が未導入。pip install pywin32 を実行する"
                ) from exc
            dispatch = win32com.client.Dispatch

        try:
            import pythoncom
        except ImportError:
            pythoncom = None  # type: ignore[assignment]

        if pythoncom is not None:
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass  # 呼び出し元スレッドで既に初期化済み等は無視する

        try:
            try:
                outlook = dispatch(_PROG_ID)
                namespace = outlook.GetNamespace("MAPI")
                calendar = namespace.GetDefaultFolder(OL_FOLDER_CALENDAR)
                items = calendar.Items
                items.Sort("[Start]")           # IncludeRecurrences より先に呼ぶ必要がある
                items.IncludeRecurrences = True  # 定期予定を展開する
            except Exception as exc:
                raise RuntimeError(
                    f"Outlook への接続に失敗: {exc}。"
                    "classic Outlook にアカウントが設定されているか確認する"
                ) from exc

            matched_items = _restrict_or_scan(items, start, end)

            events: list[CalendarEvent] = []
            for item in matched_items:
                event = to_event(item, provider_name=self.name)
                if event is not None:
                    events.append(event)

            events.sort(key=lambda e: e.start)
            return events
        finally:
            if pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
