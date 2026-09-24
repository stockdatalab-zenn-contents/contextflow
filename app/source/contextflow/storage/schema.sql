-- contextflow ローカルストア（SQLite）
-- activities はローカルを正とする。changes / decisions / tasks は GitHub を長期的な正とし、
-- ここには同期用のキャッシュとして持つ。

PRAGMA journal_mode = WAL;

-- 5秒程度の間隔で採取した前面ウィンドウの生ログ
CREATE TABLE IF NOT EXISTS raw_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,          -- ISO8601（タイムゾーン付き）
    process       TEXT    NOT NULL,
    window_title  TEXT    NOT NULL DEFAULT '',
    idle_sec      INTEGER NOT NULL DEFAULT 0,
    host          TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_raw_events_ts ON raw_events (ts);

-- raw_events を連続区間へ圧縮したもの
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    start_at      TEXT    NOT NULL,
    end_at        TEXT    NOT NULL,
    process       TEXT    NOT NULL,
    window_title  TEXT    NOT NULL DEFAULT '',
    duration_sec  INTEGER NOT NULL DEFAULT 0,
    idle_sec      INTEGER NOT NULL DEFAULT 0,
    sample_count  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (start_at, process, window_title)
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions (start_at);

-- PC作業・非PC作業を同じ形で持つ活動実績
CREATE TABLE IF NOT EXISTS activities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    start_at      TEXT    NOT NULL,
    end_at        TEXT    NOT NULL,
    activity_type TEXT    NOT NULL DEFAULT 'unknown',
    layer         TEXT    NOT NULL DEFAULT 'observed',  -- planned/observed/reported/confirmed
    source        TEXT    NOT NULL DEFAULT 'windows',
    project       TEXT,
    task          TEXT,
    confidence    REAL    NOT NULL DEFAULT 1.0,
    summary       TEXT    NOT NULL DEFAULT '',
    detail        TEXT    NOT NULL DEFAULT '{}',        -- JSON
    UNIQUE (start_at, end_at, layer, source, activity_type)
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities (start_at);
CREATE INDEX IF NOT EXISTS idx_activities_layer ON activities (layer);

-- 状況の変化
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    project     TEXT,
    description TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_changes_ts ON changes (ts);

-- 人が下した判断
CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    project    TEXT,
    decision   TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'manual',
    change_id  INTEGER REFERENCES changes (id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions (ts);

-- タスク
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL,
    project         TEXT,
    status          TEXT    NOT NULL DEFAULT 'open',
    priority        INTEGER NOT NULL DEFAULT 3,
    deadline        TEXT,
    github_issue    INTEGER,
    blocked         INTEGER NOT NULL DEFAULT 0,
    remaining_steps INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT,
    UNIQUE (title, project)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);

CREATE TABLE IF NOT EXISTS projects (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL UNIQUE,
    title  TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    note   TEXT NOT NULL DEFAULT ''
);

-- Decision Engine への問い合わせ履歴（校正の材料）
CREATE TABLE IF NOT EXISTS decision_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    engine         TEXT    NOT NULL,
    question_key   TEXT    NOT NULL,
    question_type  TEXT    NOT NULL,
    value          TEXT,
    raw_confidence REAL    NOT NULL DEFAULT 0.5,
    confidence     REAL    NOT NULL DEFAULT 0.5,
    latency_ms     INTEGER NOT NULL DEFAULT 0,
    state_json     TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_decision_logs_key ON decision_logs (engine, question_key);

-- 実際どうだったか（AI判断の答え合わせ）
CREATE TABLE IF NOT EXISTS decision_feedback (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT    NOT NULL,
    decision_log_id  INTEGER REFERENCES decision_logs (id),
    engine           TEXT    NOT NULL,
    question_key     TEXT    NOT NULL,
    predicted        TEXT,
    actual           TEXT,
    correct          INTEGER NOT NULL DEFAULT 0,
    raw_confidence   REAL    NOT NULL DEFAULT 0.5,
    note             TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_feedback_key ON decision_feedback (engine, question_key);

-- 校正モデル（isotonic / platt のパラメータを JSON で保持）
CREATE TABLE IF NOT EXISTS calibration_models (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    engine       TEXT NOT NULL,
    question_key TEXT NOT NULL,
    method       TEXT NOT NULL,
    params       TEXT NOT NULL DEFAULT '{}',
    sample_count INTEGER NOT NULL DEFAULT 0,
    fitted_at    TEXT NOT NULL,
    UNIQUE (engine, question_key)
);

-- Context Builder が生成した state のスナップショット
CREATE TABLE IF NOT EXISTS state_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    target_date  TEXT NOT NULL,
    state_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state_snapshots_date ON state_snapshots (target_date);

-- スキーマ版などの内部メモ
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 予定（カレンダー）に人が付けた種別。uid をキーにして、再取得で消えないようにする
CREATE TABLE IF NOT EXISTS calendar_labels (
    uid           TEXT PRIMARY KEY,
    activity_type TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
