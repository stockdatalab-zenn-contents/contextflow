# モジュール公開API仕様（内部コントラクト）

作成日: 2026-09-23

実装層はこの仕様どおりの公開関数・クラスだけを外部へ出す。
互いの内部実装には依存せず、`contracts/` `config.py` `timeutil.py` `storage/` のみを import する。

例外は次の2つ（いずれも同一パッケージ内、または遅延 import で切り離し可能なもの）。

- 同一パッケージ内の隣接モジュール（`collector` 内 / `pipeline` 内 / `decision` 内 / `context` 内）
- `context/builder.py` → `sources/github_context.ContextRepo`（制約の読み込み。関数内で遅延 import し、
  読めない場合は空リストで続行する）

- パッケージルート: `app/source/contextflow/`
- import 例: `from contextflow.contracts.models import Activity`
- 日本語コメント。標準ライブラリのみ（`anthropic` だけは Claude Adapter 内で遅延 import）

---

## contracts/models.py の列挙（DB に入る値）

**これらの値は DB・設定・エクスポートに書かれる契約。** 表示ラベルだけを変え、値は変えない。

| 列挙 | 値 | 意味 |
| --- | --- | --- |
| `ActivityType` | `coding` / `research` / `meeting` / `document` / `communication` / `thinking` / `planning` / `review` / `admin` / `break` / `other` / `unknown` | 活動の種別。PC操作の有無に関わらず同じ語彙を使う |
| `ActivityLayer` | `planned` / `observed` / `reported` / `confirmed` | 予定 / PCログ / 本人申告 / 突き合わせ結果 |
| `Source` | `windows` / `calendar` / `manual` / `github` / `system` | どこから得た情報か |
| `TaskStatus` | `open` / `in_progress` / `blocked` / `done` / `canceled` | タスクの状態 |

`planning` は `thinking` へ集約したため、既定では画面の選択肢に出さない
（`[ui] hidden_activity_types`）。**列挙からは消さない**ので、過去に記録した値はそのまま読める。

表示ラベルは `[ui.activity_labels]`（種別）で決める。詳細は `docs/20260923_config_reference.md`。

---

## storage/db.py

```python
class Database:
    def __init__(self, path: str | Path) -> None
    def initialize(self) -> None                  # schema.sql を適用（冪等）
    def connect(self) -> sqlite3.Connection       # row_factory = sqlite3.Row
    def close(self) -> None
    def __enter__ / __exit__                      # with 構文対応
    @property path -> Path

def open_database(config: AppConfig) -> Database  # paths.database を使い initialize 済みで返す
```

## storage/repositories.py

すべて `__init__(self, db: Database)`。戻り値の dataclass は `contracts/models.py` のもの。

```python
class RawEventRepository:
    def add(self, event: RawEvent) -> int
    def add_many(self, events: Sequence[RawEvent]) -> int
    def list_between(self, start: datetime, end: datetime) -> list[RawEvent]
    def last(self) -> RawEvent | None

class SessionRepository:
    def replace_between(self, start: datetime, end: datetime, sessions: Sequence[Session]) -> int
    def list_between(self, start: datetime, end: datetime) -> list[Session]

class ActivityRepository:
    def add(self, activity: Activity) -> int
    def replace_between(self, start, end, activities, *, layer: ActivityLayer | None = None,
                        source: Source | None = None) -> int
    def list_between(self, start, end, *, layer: ActivityLayer | None = None) -> list[Activity]
    def current(self, at: datetime, *, layer: ActivityLayer | None = None) -> Activity | None
    def get(self, activity_id: int) -> Activity | None
    def update(self, activity: Activity) -> bool     # id 必須。UNIQUE 衝突は ValueError
    def delete(self, activity_id: int) -> bool

class ChangeRepository:
    def add(self, change: Change) -> int
    def recent(self, limit: int = 10) -> list[Change]
    def list_between(self, start, end) -> list[Change]
    def get(self, change_id: int) -> Change | None
    def update(self, change: Change) -> bool
    def delete(self, change_id: int) -> bool

class DecisionRepository:          # 人の判断（decisions テーブル）
    def add(self, decision: Decision) -> int
    def recent(self, limit: int = 10) -> list[Decision]
    def list_between(self, start, end) -> list[Decision]
    def get(self, decision_id: int) -> Decision | None
    def update(self, decision: Decision) -> bool
    def delete(self, decision_id: int) -> bool

class TaskRepository:
    def upsert(self, task: Task) -> int            # (title, project) で一意
    def list(self, *, status: TaskStatus | None = None) -> list[Task]
    def get(self, task_id: int) -> Task | None
    def find_by_title(self, title: str, project: str | None = None) -> Task | None
    def counts(self) -> dict[str, int]             # {'open': n, 'blocked': n, ...}

class ProjectRepository:
    def upsert(self, project: Project) -> int
    def list(self) -> list[Project]

class DecisionLogRepository:       # Decision Engine の履歴
    def add_response(self, request: DecisionRequest, response: DecisionResponse) -> list[int]
    def recent(self, *, engine: str | None = None, question_key: str | None = None,
               limit: int = 50) -> list[dict]

class FeedbackRepository:
    def add(self, *, engine: str, question_key: str, predicted: str, actual: str,
            correct: bool, raw_confidence: float, decision_log_id: int | None = None,
            note: str = "") -> int
    def list(self, *, engine: str | None = None, question_key: str | None = None) -> list[dict]

class CalibrationRepository:
    def save(self, *, engine: str, question_key: str, method: str, params: dict,
             sample_count: int) -> int
    def get(self, engine: str, question_key: str) -> dict | None
    def list(self) -> list[dict]

class StateSnapshotRepository:
    def add(self, state: CurrentState) -> int
    def latest(self) -> dict | None

class MetaRepository:
    def get(self, key: str) -> str | None
    def set(self, key: str, value: str) -> None
    def delete(self, key: str) -> None
```

## collector/win32.py

```python
@dataclass
class ForegroundInfo:
    process: str
    window_title: str
    pid: int

def get_foreground_info() -> ForegroundInfo     # 取得失敗時は process='' を返す
def get_idle_sec() -> int                        # 最終入力からの経過秒
def get_host() -> str
def is_supported() -> bool                       # Windows 以外なら False
```

ctypes のみ使用。Windows 以外では例外を投げず空情報を返す。

## collector/collector.py

```python
class Collector:
    def __init__(self, config: AppConfig, db: Database) -> None
    def sample(self) -> RawEvent            # 1回ぶん採取（マスク適用済み）
    def run(self, duration_sec: int | None = None) -> int   # ループ。件数を返す

def mask_title(title: str, config: AppConfig) -> str   # privacy 設定を適用
```

## pipeline/p10_sessionizer.py

```python
def build_sessions(events: Sequence[RawEvent], config: AppConfig) -> list[Session]
def sessionize_day(db: Database, target: date, config: AppConfig) -> list[Session]  # 保存まで
```

連結条件: 同一 process（`merge_by_process_only=false` ならタイトルも一致）かつ間隔が `merge_gap_sec` 以内。
`idle_threshold_sec` を超える idle のイベントは session を切る。`min_duration_sec` 未満は破棄。

## pipeline/p20_classifier.py

```python
class Classifier:
    def __init__(self, categories: dict) -> None
    def classify(self, process: str, window_title: str) -> tuple[ActivityType, str | None]

def load_classifier(path: Path | None = None) -> Classifier
```

評価順: title_rules → process_rules → default。project は project_rules から。

## pipeline/p30_activity_builder.py

```python
def sessions_to_activities(sessions, classifier, config) -> list[Activity]   # layer=OBSERVED
def merge_layers(planned, observed, reported, config) -> list[Activity]      # layer=CONFIRMED
def build_day(db: Database, target: date, config: AppConfig) -> list[Activity]  # 保存まで
def find_gaps(activities, start, end, min_gap_sec: int = 600) -> list[tuple[datetime, datetime]]
```

優先度: reported(manual) > observed(windows) > planned(calendar)。
上位層の区間が下位層を上書きし、残った部分だけ下位層を採用する。confidence は元レコードを引き継ぐ。

## sources/manual.py

```python
class ManualInput:
    def __init__(self, db: Database) -> None
    def start(self, activity_type: ActivityType, *, project=None, task=None, at=None) -> None
    def stop(self, *, at=None, summary: str = "") -> list[Activity]
    def running(self) -> dict | None
    def add(self, start, end, activity_type, *, project=None, task=None, summary="") -> list[Activity]
```

実行中の作業は meta テーブル（key=`manual_running`）に JSON で保持。layer=REPORTED / source=MANUAL。

`stop` / `add` は日をまたぐ区間（ローカル日付の 00:00 をまたぐ場合）を日境界ごとに
複数の Activity へ分割して保存する（`ActivityRepository.list_between` が `start_at`
だけで絞り込むため、分割しないと終了日側の一覧・集計から抜け落ちるための対策。
calendar_ics.py の日またぎ分割と同じ考え方）。日をまたがない通常ケースでは、
今までどおり1件だけの list を返す。`stop` が実行中なし・開始直後の取り消しで
何も保存しない場合は空リストを返す（従来の `None` から変更）。

## contracts/calendar.py（カレンダー取得の窓口）

```python
@dataclass
class CalendarEvent:
    start: datetime; end: datetime
    subject: str; location: str; organizer: str
    is_all_day: bool; is_cancelled: bool; is_recurring: bool
    busy_status: str        # free / tentative / busy / oof / unknown
    response_status: str    # none / organizer / accepted / declined / tentative / unknown
    uid: str; provider: str; categories: list[str]

@dataclass
class ProviderStatus:
    available: bool
    message: str            # 日本語1行。使えない場合は直し方

class CalendarProvider(abc.ABC):
    name: str
    def check(self) -> ProviderStatus
    def fetch(self, start: datetime, end: datetime) -> list[CalendarEvent]

class CalendarFilter:
    def __init__(self, *, skip_all_day=True, skip_declined=True,
                 skip_cancelled=True, skip_free=True) -> None
    def reject_reason(self, event) -> str | None
    def apply(self, events) -> tuple[list[CalendarEvent], dict[str, int]]
```

取得元（classic Outlook COM / .ics / 将来の Graph）はこのインターフェースだけを実装する。
取得できない理由は例外ではなく `check()` の message で伝え、`fetch()` は RuntimeError を投げる。

## sources/calendar_outlook.py

```python
OL_FOLDER_CALENDAR = 9

class OutlookComProvider(CalendarProvider):   # name = "outlook"
    def __init__(self, config: AppConfig | None = None, *, dispatch=None) -> None

def format_restrict_datetime(value: datetime) -> str
def to_event(item, *, provider_name: str = "outlook") -> CalendarEvent | None
```

`dispatch` はテスト用の差し替え口（既定は `win32com.client.Dispatch` を遅延 import）。
`check()` は COM オブジェクトを生成しない（Outlook を起動させないため、`winreg` で ProgID を見る）。
`fetch()` は `Sort("[Start]")` → `IncludeRecurrences = True` の順で設定し（逆だと定期予定が展開されない）、
`Restrict` は英語圏書式 → 日本語環境書式の順に試し、両方失敗したら全件走査へ退避する。

## sources/calendar_sync.py

```python
@dataclass
class SyncResult:
    provider: str; start: datetime; end: datetime
    fetched: int; rejected: dict[str, int]
    saved: list[Activity]; message: str

def create_provider(config: AppConfig, name: str | None = None) -> CalendarProvider
def fetch_window(config, *, days=None, days_back=None, base=None) -> tuple[datetime, datetime]
def events_to_activities(events, config) -> list[Activity]
def sync_calendar(db, config, *, provider=None, days=None, days_back=None, base=None) -> SyncResult
```

`create_provider` は outlook が使えなければ警告を出して ics へ退避する。
`sync_calendar` は取得範囲を日ごとに `replace_between(layer=PLANNED)` で置き換える
（範囲内で予定が0件の日も空で置き換え、範囲外の日には触れない）。

## sources/calendar_ics.py

```python
def parse_ics(text: str) -> list[dict]     # {'summary','start','end','location'}
def import_ics_dir(db: Database, config: AppConfig, target: date | None = None) -> list[Activity]

class IcsFileProvider(CalendarProvider):   # name = "ics"
    def __init__(self, config: AppConfig) -> None
```

標準ライブラリのみの最小 ICS パーサ。VEVENT の SUMMARY / DTSTART / DTEND のみ扱う。
layer=PLANNED / source=CALENDAR / activity_type=MEETING。

## sources/github_context.py

```python
@dataclass
class ProjectFolder:                     # 10_projects/<prefix>_<yyyymm>_<suffix>
    path: Path; folder: str
    prefix: str; period: str; suffix: str
    @property key -> str                 # context_repo.project_key に従う

class ContextRepo:                       # 仕事管理リポジトリ（長期コンテキスト）
    def __init__(self, root: Path, config: AppConfig | None = None) -> None
    def ensure_layout(self) -> None      # work_repo では何も作らない
    def projects(self) -> list[ProjectFolder]
    def find_project(self, key: str) -> ProjectFolder | None
    def read_tasks(self) -> list[Task]   # tasks.md の5セクション記法。読み取り専用
    def read_constraints(self) -> list[str]
    def read_project_state(self, project: str) -> str
    def append_decision(self, project: str, target: date, line: str) -> Path | None
    def append_change(self, project: str, target: date, line: str) -> Path | None
    def write_daily(self, target: date, body: str) -> Path | None
    def append_month(self, kind: str, target: date, line: str) -> Path | None  # 後方互換

class GitHubIssues:                      # urllib で REST。**読み取り専用**
    def __init__(self, repo: str, token: str | None, config: AppConfig | None = None) -> None
    @property enabled -> bool
    def list_open(self) -> list[Task]
    def sync_to_db(self, db: Database) -> int
```

`layout` は `context_repo.layout`（既定 `work_repo`）で決まる。
`config` を渡さない場合は後方互換のため `standalone` として動く。

- `work_repo`: 既存の仕事管理リポジトリに合わせる。`ensure_layout` はディレクトリを作らない。
  `tasks.md` と Issue は読み取り専用。書き込むのは案件の `decisions.md` のみ。
  `write_daily` は `context_repo.write_daily` が true のときだけ書く（既定 false）。
- `standalone`: 従来の contextflow 専用構成（`projects/` `context/` `activity/` `agent/`）。

詳細は `docs/20260923_work_repo_integration.md`。

## sources/git_sync.py

```python
@dataclass
class GitStatus:
    available: bool    # git コマンドが使えるか
    is_repo: bool      # root が git リポジトリか
    branch: str
    remote: str        # 設定済みの origin URL
    dirty: int         # 未コミットのファイル数
    message: str       # 人間向けの状況説明（日本語1行）

class GitSync:
    def __init__(self, root: Path, *, remote: str = "", branch: str = "main") -> None
    def status(self) -> GitStatus
    def init(self) -> str
    def commit(self, message: str, paths: Sequence[Path] | None = None) -> str
    def set_remote(self, url: str) -> str
    def push(self) -> str
    def sync(self, message: str, paths: Sequence[Path] | None = None) -> str

def open_git_sync(config: AppConfig) -> GitSync
```

リポジトリ未作成でも動く。戻り値はすべて人間向けの日本語で、致命的でない限り例外にしない。
`push` は git 側に origin が無くても、config の `github.remote` が設定されていれば自動で登録する。
`commit` / `sync` に `paths` を渡すと、そのファイルだけを commit する。
連携先が普段使いの作業リポジトリの場合、`add -A` では人が編集中のファイルまで
巻き込むため、contextflow が書いたファイルだけを渡す。

## context/features.py

```python
def summarize_time(activities: Sequence[Activity]) -> TimeSummary
def compute_features(activities: Sequence[Activity], sessions: Sequence[Session],
                     config: AppConfig, *, now: datetime | None = None) -> WorkFeatures
```

## context/builder.py

```python
class ContextBuilder:
    def __init__(self, db: Database, config: AppConfig) -> None
    def build(self, target: date | None = None, now: datetime | None = None) -> CurrentState
    def save_json(self, state: CurrentState, path: Path | None = None) -> Path

def state_to_flat_dict(state: CurrentState) -> dict[str, Any]
```

`recent_changes` / `recent_decisions` は**案件名と日時つきの辞書**で渡す。

```json
"recent_changes":   [{"description": "API利用が不可と判明", "project": "sample_project",
                      "ts": "2026-09-23T16:16:06+09:00", "minutes_ago": 30}],
"recent_decisions": [{"decision": "CSV連携へ変更", "reason": "API不可", "project": "sample_project",
                      "ts": "2026-09-23T14:46:06+09:00", "minutes_ago": 120}]
```

| 項目 | 必要な理由 |
| --- | --- |
| `project` | 「その変化・判断が今の案件のものか」を判断するため。他の項目とも形が揃う |
| `ts` / `minutes_ago` | `recent()` は日をまたいで直近N件を返すため、時刻が無いと**古い変化をいつまでも「直近の変化」として扱う** |

`minutes_ago` の基準は `generated_at`（state を作った時刻）。`now()` ではないため、
同じ state からは何度呼んでも同じ値になる。未来の時刻（手入力で後の時刻を指定）は**負**になる。

`state_to_flat_dict` は Decision Engine へ渡す軽量な平坦 dict（生ログ・ウィンドウタイトルは含めない）。
キーは次の40個で固定する。

```
time, date, today_total_min, today_focus_min, by_type, by_project,
current_activity_type, current_project, current_task, task_elapsed_min,
open_tasks, blocked_tasks, recent_context_switches, deep_work_min,
longest_focus_min, active_min, idle_min, last_break_min_ago,
candidate_tasks, recent_changes, recent_decisions, constraints,
task_blocked, task_deadline_days, task_priority, task_remaining_steps,
past_days, past_total_min, past_by_type, past_by_project,
past_deep_work_min, past_context_switches, past_active_days,
past_change_count, past_decision_count,
upcoming_days, upcoming_planned_min, upcoming_by_type,
upcoming_items, upcoming_deadlines
```

`recent_context_switches`（当日=today の切り替え回数、既存）と
`past_context_switches`（過去 `past_days` 日ぶんの合計、新規）は別物。
新規追加時に同名衝突を避けるため、後者はこの名前にしている。

`recent_*` は `CurrentState.recent`（`PastSummary`）、`upcoming_*` は
`CurrentState.upcoming`（`UpcomingSummary`）から作る。対象日を含まない過去の傾向と、
`_clamp_now` が返す基準時刻から先の予定・締切をそれぞれ表す。

```json
"upcoming_items": [{"start": "2026-09-25T10:00:00+09:00", "end": "2026-09-25T11:00:00+09:00",
                    "activity_type": "meeting", "summary": "定例MTG", "project": "sample_project",
                    "duration_min": 60}],
"upcoming_deadlines": [{"title": "資料提出", "project": "sample_project",
                        "deadline_days": 3, "priority": 2, "blocked": false}]
```

`Planner._safe_state_payload` にも同じ内容を `"recent"` / `"upcoming"`（ネスト dict）として渡す。
予定の件名（`summary`）は渡すが、`current_activity.summary` / `detail`（生ログ）は渡さない。
`summary` は `[calendar] mask_subject` により取り込み時に既にマスク適用済み。

## config.py（運用方針）

```python
@dataclass
class ModeConfig:
    name: str
    description: str
    engines: list[str]      # 左から順に試し、失敗したら次へ退避
    planner: str            # "claude" | "offline"
    @property uses_llm_planner -> bool

DEFAULT_MODES: dict[str, ModeConfig]
def load_modes(config: AppConfig) -> dict[str, ModeConfig]
def resolve_mode(config: AppConfig, name: str | None = None) -> ModeConfig
```

優先順位は 引数 > `decision.mode` > `rule_first`。
`decision.engine` が非空のときは、そのエンジンを先頭に置いた一時的な方針を返す（末尾に `rule_based`）。

## decision/registry.py

```python
class FallbackEngine(DecisionEngine):
    name = "fallback"
    def __init__(self, engines: list[DecisionEngine]) -> None
    def ask(self, request) -> DecisionResponse   # ask を上書きする（_ask ではない）
    last_engine: str                              # 直近に成功したエンジン名

def create_engine_chain(config: AppConfig, db: Database | None = None,
                        mode: str | None = None) -> DecisionEngine
def create_engine(config: AppConfig, db: Database | None = None,
                  name: str | None = None) -> DecisionEngine
def available_engines() -> list[str]
def available_modes(config: AppConfig) -> list[str]
```

`FallbackEngine` が `_ask` ではなく `ask` を上書きするのは、基底の `ask` が confidence を校正するため。
子の校正済み confidence を親が生の値で上書きしないよう、子の応答を加工せず返す。

`create_engine_chain` はエンジンの生成に失敗したものをチェーンから落とす（警告のみ）。
0個なら `RuleBasedEngine`、1個ならそのまま、2個以上なら `FallbackEngine` で包む。

## decision/adapters/rule_based.py

```python
class RuleBasedEngine(DecisionEngine):   # name = 'rule_based'
    def __init__(self, calibrator=None, config: AppConfig | None = None) -> None
```

LLM 無しで動く既定エンジン。state の特徴量から素朴なルールで回答し、raw_confidence も返す。

## decision/adapters/claude_api.py

```python
class ClaudeDecisionEngine(DecisionEngine):   # name = 'claude'
    def __init__(self, config: AppConfig, calibrator=None) -> None

def build_output_schema(questions: Sequence[Question]) -> dict   # JSON Schema を組み立て
```

公式 SDK `anthropic` を遅延 import。`client.messages.create(...)` に
`output_config={"effort": ..., "format": {"type": "json_schema", "schema": ...}}` を渡して
JSON だけ返させる。model 既定値は `claude-opus-5`（config で変更可）。

## decision/adapters/openai_compat.py

```python
class OpenAICompatEngine(DecisionEngine):   # name = 'openai_compat'
```

`urllib.request` で `{base_url}/chat/completions` を叩く（Ollama 等の OpenAI 互換）。

## decision/adapters/jev.py

```python
class JevEngine(DecisionEngine):   # name = 'jev'
```

`{state, questions}` をそのまま POST し、`{key: {value, confidence}}` を受け取る薄い HTTP Adapter。

## decision/calibration.py

```python
class CalibrationModel:
    method: str; params: dict
    def apply(self, raw_confidence: float) -> float

class SqliteCalibrator:               # contracts.decision.Calibrator を満たす
    def __init__(self, db: Database) -> None
    def calibrate(self, engine: str, question_key: str, raw_confidence: float) -> float

def fit_isotonic(samples: Sequence[tuple[float, bool]]) -> CalibrationModel   # PAV
def fit_platt(samples: Sequence[tuple[float, bool]]) -> CalibrationModel
def fit_all(db: Database, *, min_samples: int = 20) -> list[dict]
```

外部ライブラリ不使用。サンプルが少ないときは恒等変換へフォールバック。

## planner/planner.py

```python
class Planner:
    def __init__(self, config: AppConfig) -> None
    def make_plan(self, state: CurrentState, decisions: DecisionResponse) -> str
    def parse_activity_text(self, text: str, base_date: date | None = None) -> Activity | None

def render_offline_plan(state: CurrentState, decisions: DecisionResponse) -> str
```

LLM が使えないときは `render_offline_plan` の文面を返す（例外にしない）。

## report/daily_markdown.py

```python
def render_daily(state: CurrentState, activities, changes, decisions) -> str
def write_daily(config: AppConfig, state, activities, changes, decisions) -> Path
```

出力は参照資料の `activity/daily/YYYY-MM-DD.md` 形式
（Time / Main activities / Context switches / Changes / Decisions）。

## ui/server.py, ui/api.py

```python
def create_server(config: AppConfig, *, host="127.0.0.1", port=8765,
                  token: str | None = None) -> tuple[ThreadingHTTPServer, str]
def serve(config: AppConfig, *, host="127.0.0.1", port=8765, open_browser=True) -> int

def handle(method: str, path: str, query: dict, body: dict,
           config: AppConfig) -> tuple[int, dict]
```

標準ライブラリのみ（`http.server`）。`127.0.0.1` 固定でバインドし、
書き込み要求は起動時トークン（`X-CF-Token`）と `Host` / `Origin` の検査を通す。
画面ファイルは `ui/static/`（フレームワーク・CDN 不使用）。

UI 層にロジックは置かない。既存の `ManualInput` / 各 Repository / `build_day` を呼ぶだけ。
API 契約と画面仕様は `docs/20260923_ui_design.md` が正。

## cli.py

```python
def main(argv: Sequence[str] | None = None) -> int
```

サブコマンド: `init` / `collect` / `sessionize` / `build` / `work` / `calendar` / `state` /
`gaps` / `decide` / `plan` / `ui` / `mode` / `change` / `decision` / `task` / `report` /
`feedback` / `calibrate` / `github`

`ui` はローカル GUI を手動起動する（`--port` / `--no-browser`）。

`mode` は現在の運用方針と選べる方針を表示する（変更は config.toml）。
`decide` / `plan` / `gaps` は `--mode` で一時的に上書きできる。
`github` のサブコマンドは `status` / `init-repo` / `pull` / `export [--push]` / `push [-m]`。

`gaps` は `find_gaps` で空白時間を洗い出し、予定の境界で切り分けてから `gap_fill` 質問セットへ渡す。
`--apply` を付けると、校正後 confidence が閾値以上の推定だけを
`layer=REPORTED` / `source=SYSTEM` で記録する（人の手入力と区別できるようにするため）。
