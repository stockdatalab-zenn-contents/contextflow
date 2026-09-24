"""コントラクト層: 全モジュールが共有するデータ型。

ここは「厳密に保つ層」。実装層（collector / pipeline / decision など）は
この型だけに依存し、互いを直接 import しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 列挙型
# ---------------------------------------------------------------------------


class ActivityType(str, Enum):
    """活動の種別。PC操作有無にかかわらず同じ語彙を使う。"""

    CODING = "coding"
    RESEARCH = "research"
    MEETING = "meeting"
    DOCUMENT = "document"
    COMMUNICATION = "communication"
    THINKING = "thinking"
    PLANNING = "planning"
    REVIEW = "review"
    ADMIN = "admin"
    BREAK = "break"
    OTHER = "other"
    UNKNOWN = "unknown"


class Source(str, Enum):
    """情報源。どこから得た情報かを必ず残す。"""

    WINDOWS = "windows"
    MANUAL = "manual"
    CALENDAR = "calendar"
    GITHUB = "github"
    SYSTEM = "system"


class ActivityLayer(str, Enum):
    """予定・観測・自己申告・確定を分離するための層。"""

    PLANNED = "planned"      # カレンダー等の予定
    OBSERVED = "observed"    # PCログ等の観測
    REPORTED = "reported"    # 本人入力
    CONFIRMED = "confirmed"  # 上記をマージした確定値


class TaskStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELED = "canceled"


# ---------------------------------------------------------------------------
# 観測データ
# ---------------------------------------------------------------------------


@dataclass
class RawEvent:
    """一定間隔で採取した前面ウィンドウのスナップショット。"""

    ts: datetime
    process: str
    window_title: str
    idle_sec: int = 0
    host: str = ""
    id: Optional[int] = None


@dataclass
class Session:
    """連続する RawEvent を圧縮した区間。"""

    start_at: datetime
    end_at: datetime
    process: str
    window_title: str
    duration_sec: int
    idle_sec: int = 0
    sample_count: int = 0
    id: Optional[int] = None

    @property
    def duration_min(self) -> int:
        return int(round(self.duration_sec / 60))


# ---------------------------------------------------------------------------
# 活動・変化・判断・タスク
# ---------------------------------------------------------------------------


@dataclass
class Activity:
    """PC作業・非PC作業を区別せず扱う共通の活動レコード。"""

    start_at: datetime
    end_at: datetime
    activity_type: ActivityType = ActivityType.UNKNOWN
    layer: ActivityLayer = ActivityLayer.OBSERVED
    source: Source = Source.WINDOWS
    project: Optional[str] = None
    task: Optional[str] = None
    confidence: float = 1.0
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None

    @property
    def duration_sec(self) -> int:
        return max(0, int((self.end_at - self.start_at).total_seconds()))

    @property
    def duration_min(self) -> int:
        return int(round(self.duration_sec / 60))


@dataclass
class Change:
    """状況の変化。「何が変わったか」。"""

    ts: datetime
    description: str
    project: Optional[str] = None
    source: Source = Source.MANUAL
    id: Optional[int] = None


@dataclass
class Decision:
    """人が下した判断。「何を決めたか」。"""

    ts: datetime
    decision: str
    reason: str = ""
    project: Optional[str] = None
    source: Source = Source.MANUAL
    change_id: Optional[int] = None
    id: Optional[int] = None


@dataclass
class Task:
    """次にやること。GitHub Issue と対応づけ可能。"""

    title: str
    project: Optional[str] = None
    status: TaskStatus = TaskStatus.OPEN
    priority: int = 3           # 1(高) - 5(低)
    deadline: Optional[date] = None
    github_issue: Optional[int] = None
    blocked: bool = False
    remaining_steps: int = 0
    updated_at: Optional[datetime] = None
    id: Optional[int] = None


@dataclass
class Project:
    """プロジェクト（案件）。"""

    name: str
    title: str = ""
    status: str = "active"
    note: str = ""
    id: Optional[int] = None


# ---------------------------------------------------------------------------
# 現在状態（Current State）
# ---------------------------------------------------------------------------


@dataclass
class TimeSummary:
    """当日の時間集計。"""

    total_min: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    by_project: dict[str, int] = field(default_factory=dict)


@dataclass
class WorkFeatures:
    """判断に使う特徴量。生ログではなくここを Decision Engine へ渡す。"""

    deep_work_min: int = 0
    context_switches: int = 0
    longest_focus_min: int = 0
    active_min: int = 0
    idle_min: int = 0
    last_break_min_ago: Optional[int] = None


@dataclass
class PlannedItem:
    """先読み用の予定1件。件名も持つ（判断の材料にするため）。"""

    start_at: datetime
    end_at: datetime
    activity_type: ActivityType = ActivityType.MEETING
    summary: str = ""
    project: Optional[str] = None

    @property
    def duration_min(self) -> int:
        return max(0, int(round((self.end_at - self.start_at).total_seconds() / 60)))


@dataclass
class PastSummary:
    """過去 N 日の傾向。対象日は含まない（当日は today が持つため）。"""

    days: int = 0
    start_date: Optional[date] = None
    end_date: Optional[date] = None      # 対象日の前日
    total_min: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    by_project: dict[str, int] = field(default_factory=dict)
    deep_work_min: int = 0
    context_switches: int = 0
    active_days: int = 0                 # 記録があった日数
    change_count: int = 0
    decision_count: int = 0


@dataclass
class UpcomingSummary:
    """今後 N 日の予定と締切。"""

    days: int = 0
    planned_min: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    items: list[PlannedItem] = field(default_factory=list)
    deadlines: list[Task] = field(default_factory=list)   # 期限が範囲内の未完了タスク


@dataclass
class CurrentState:
    """Context Builder が生成する、システムの中心データ構造。"""

    generated_at: datetime
    target_date: date
    now_time: str = ""
    today: TimeSummary = field(default_factory=TimeSummary)
    features: WorkFeatures = field(default_factory=WorkFeatures)
    current_activity: Optional[Activity] = None
    current_task: Optional[Task] = None
    task_elapsed_min: int = 0
    open_tasks: int = 0
    blocked_tasks: int = 0
    candidate_tasks: list[Task] = field(default_factory=list)
    recent_changes: list[Change] = field(default_factory=list)
    recent_decisions: list[Decision] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 過去の傾向・未来の先読み（対象日1日ぶんの today/features だけでは
    # 未来の判断材料が無いため追加）。既定値を持たせ、既存呼び出しとの後方互換を保つ
    past: PastSummary = field(default_factory=PastSummary)
    upcoming: UpcomingSummary = field(default_factory=UpcomingSummary)
