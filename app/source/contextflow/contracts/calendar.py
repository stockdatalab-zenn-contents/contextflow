"""コントラクト層: カレンダー取得のインターフェース。

取得元（classic Outlook の COM / .ics ファイル / 将来の Microsoft Graph）を
差し替えられるよう、アプリ側は CalendarProvider だけを見る。

参照資料の方針どおり、ここで得られるのは「予定」であって「実績」ではない。
Activity へ変換する際は layer=PLANNED として扱う。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class CalendarEvent:
    """カレンダー1件。取得元に依らない共通形。"""

    start: datetime
    end: datetime
    subject: str = ""
    location: str = ""
    organizer: str = ""
    is_all_day: bool = False
    is_cancelled: bool = False
    is_recurring: bool = False
    busy_status: str = "busy"        # free / tentative / busy / oof / unknown
    response_status: str = "unknown"  # none / organizer / accepted / declined / tentative / unknown
    uid: str = ""
    provider: str = ""
    categories: list[str] = field(default_factory=list)

    @property
    def duration_min(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() / 60))


@dataclass
class ProviderStatus:
    """取得元が使える状態かどうか。CLI がそのまま表示する。"""

    available: bool
    message: str = ""


class CalendarProvider(abc.ABC):
    """取得元の共通インターフェース。

    実装は `check()` と `fetch()` の2つだけを提供する。
    取得できない理由（未インストール・未設定など）は例外ではなく
    `check()` の message で伝え、`fetch()` は RuntimeError を投げる。
    """

    name: str = "base"

    @abc.abstractmethod
    def check(self) -> ProviderStatus:
        """利用可否と、その理由（日本語1行）を返す。"""

    @abc.abstractmethod
    def fetch(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """[start, end) に重なる予定を返す。取得できなければ RuntimeError。"""

    def __str__(self) -> str:  # ログ表示用
        return self.name


class CalendarFilter:
    """取得した予定のうち、活動として扱わないものを落とす。

    Outlook から自動取得すると、辞退済み・キャンセル・終日・空き時間表示の予定まで
    入ってくる。これらを実績の材料にすると時間集計が壊れるため、ここで除く。
    """

    def __init__(
        self,
        *,
        skip_all_day: bool = True,
        skip_declined: bool = True,
        skip_cancelled: bool = True,
        skip_free: bool = True,
    ) -> None:
        self.skip_all_day = skip_all_day
        self.skip_declined = skip_declined
        self.skip_cancelled = skip_cancelled
        self.skip_free = skip_free

    def reject_reason(self, event: CalendarEvent) -> Optional[str]:
        """除外する場合は理由（日本語）、残す場合は None。"""
        if self.skip_cancelled and event.is_cancelled:
            return "キャンセル済み"
        if self.skip_declined and event.response_status == "declined":
            return "辞退済み"
        if self.skip_all_day and event.is_all_day:
            return "終日予定"
        if self.skip_free and event.busy_status == "free":
            return "空き時間扱い"
        if event.end <= event.start:
            return "区間が不正"
        return None

    def apply(self, events: list[CalendarEvent]) -> tuple[list[CalendarEvent], dict[str, int]]:
        """フィルタを適用し、残った予定と除外理由の件数を返す。"""
        kept: list[CalendarEvent] = []
        rejected: dict[str, int] = {}
        for event in events:
            reason = self.reject_reason(event)
            if reason is None:
                kept.append(event)
            else:
                rejected[reason] = rejected.get(reason, 0) + 1
        return kept, rejected
