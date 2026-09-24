# contextflow 全体設計

作成日: 2026-09-23
出典: 非公開の検討資料（Windows の操作ログから判断材料を組み立てる設計案）を実装へ落としたもの

---

## 1. 設計の骨子

**「ログを集める仕組み」と「考えるLLM」を直接つながず、その間に “現在状態を構造化する層” を置く。**

- 生ログ（ウィンドウタイトル・URL 等）はローカル SQLite に閉じる
- LLM へ渡すのは、匿名化・集計済みの `Current State` のみ
- 判断（型付き・大量・高速）と説明（自然文・少数・低速）を別のコンポーネントに分ける
- `Jev` そのものに依存せず、`DecisionEngine` インターフェースを介して差し替える

---

## 2. 層の構成と役割

```
                ┌─ Windows Logger
                │   役割: PC上で何をしていたかを自動記録
                │   ・アプリ ・ウィンドウ ・操作時間 ・Idle時間
                │
                ├─ Calendar（classic Outlook COM / .ics）
                │   役割: 予定されていた活動を取得（予定≠実績）
                │   日次で直近1週間ぶんを取り直す
                │
                ├─ GitHub / Context Repo
                │   役割: 仕事上の長期コンテキストを保持
                │   ・Issue / Task ・Project状態 ・変更履歴 ・判断記録
                │
                └─ Manual Input
                    役割: 自動取得できない活動を補完
                    ・対面会議 ・相談 ・思考 ・紙作業 ・移動中の作業
                       │
                       ▼
                ┌─────────────────┐
                │ Activity Store  │ 役割: 実際の活動実績を一元管理
                │    SQLite       │ 開始/終了・種別・Project・Task・情報源・確信度
                └────────┬────────┘
                         ▼
                ┌─────────────────┐
                │ Context Builder │ 役割: 複数の情報源から「現在の状態」を構造化
                │                 │ 何をしているか・何分使ったか・進捗・未完了・Blocker
                └────────┬────────┘
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
      Activity        Change         Decision
     何をしたか      何が変わったか   何を決めたか
          └──────────────┼──────────────┘
                         ▼
                ┌─────────────────┐
                │  Current State  │ 役割: AIが判断するための現在地
                └────────┬────────┘
                         ▼
                ┌─────────────────┐
                │ Jev風 Decision  │ 役割: 小さな判断を構造化して行う
                │     Engine      │ yes/no・choice・score・confidence
                └────────┬────────┘
                         ▼
                ┌─────────────────┐
                │ Task / Priority │ 役割: 次の行動候補を絞り込む
                └────────┬────────┘
                         ▼
                ┌─────────────────┐
                │       LLM       │ 役割: 複雑な推論と人間向け説明
                │    (Planner)    │ 理由説明・計画作成・振り返り
                └────────┬────────┘
                         ▼
                       User         最終判断・実行
```

判断の担当分けは次のとおり。

| 判断 | 担当 |
| --- | --- |
| このタスクは blocked か | Decision Engine |
| 今日やるべきか | Decision Engine |
| research / coding / meeting のどれか | Decision Engine |
| 優先度 1〜5 | Decision Engine |
| 状況が昨日から変化したか | Decision Engine |
| なぜ予定より遅れたか分析 | LLM Planner |
| 今日の作業計画を作る | LLM Planner |
| 複数プロジェクト間のトレードオフ説明 | LLM Planner |

---

## 3. モジュール対応表

| 層 | 実装 |
| --- | --- |
| コントラクト（型・API） | `contracts/models.py`, `contracts/decision.py`, `contracts/serde.py` |
| 設定 | `config.py` + `app/source/config/*.toml` |
| 保存 | `storage/db.py`, `storage/repositories.py`, `storage/schema.sql` |
| 収集 | `collector/win32.py`, `collector/collector.py` |
| 加工 | `pipeline/p10_sessionizer.py` → `p20_classifier.py` → `p30_activity_builder.py` |
| 情報源 | `sources/manual.py`, `sources/calendar_outlook.py`, `sources/calendar_ics.py`, `sources/calendar_sync.py`, `sources/github_context.py`, `sources/git_sync.py` |
| 状態化 | `context/features.py`, `context/builder.py` |
| 判断 | `decision/registry.py`, `decision/adapters/*`, `decision/calibration.py` |
| 説明 | `planner/planner.py` |
| 出力 | `report/daily_markdown.py` |
| 入口 | `cli.py`, `app/cf.py` |

パイプラインは処理順が分かるよう `p10` / `p20` / `p30` と数値を振る。

### 名前が似ていて紛らわしい2つ

| 2節の図 | コマンド | 実装 | 作るもの |
| --- | --- | --- | --- |
| Activity Store への集約 | `build` | `pipeline/p30_activity_builder.py` | 確定 activity（DB） |
| **Context Builder** | **`state`** | `context/builder.py` | Current State（`state.json`） |

`build`（コマンド）と `Context Builder`（図の名前）は**別物**。
ファイル名も `p30_activity_builder.py` と `context/builder.py` で似ているため、
どちらを指しているかは「確定 activity を作るのか、Current State を作るのか」で判断する。

---

## 4. データモデル

### 4.1 Activity（PC作業・非PC作業を同一の形で扱う）

| 項目 | 内容 |
| --- | --- |
| start_at / end_at | 区間 |
| activity_type | coding / research / meeting / document / communication / thinking / planning / review / admin / break / other / unknown |
| layer | planned（予定） / observed（観測） / reported（本人入力） / confirmed（確定） |
| source | windows / manual / calendar / github / system |
| project / task | 紐づけ |
| confidence | 推定の確かさ |
| summary / detail | 補足 |

**`source` を必ず残す**のが要点。どこから得た情報かが分からないと、後から補正できない。

### 4.2 layer を分ける理由

カレンダーの予定を実績として扱わない。次のように別々に持ち、マージして確定値を作る。

```
予定   13:00-14:00 Meeting
観測   13:00-13:40 PC Idle / 13:40-14:00 PowerPoint
入力   13:00-13:40 Meeting
  ↓ merge_layers（reported > observed > planned）
確定   13:00-13:40 Meeting(manual) / 13:40-14:00 Document(windows)
```

予定しか無い時間帯は confidence を下げて採用する。「予定＝実績」にしないため。

### 4.2.1 カレンダーの取得

取得元は `contracts/calendar.py` の `CalendarProvider` だけを介する。

| 実装 | 位置づけ |
| --- | --- |
| `OutlookComProvider` | 既定。classic Outlook を COM で読む（pywin32） |
| `IcsFileProvider` | 退避先。`.ics` ファイルを読む |
| `GraphProvider` | 未実装。会社で Graph API が使えるようになったら追加 |

普段使いは新 Outlook のまま、classic Outlook を Python 用のアダプタとして残す構成。
新 Outlook には COM が無いが、同じメールボックスを見るため中身は同じになる。
日次実行はタスクスケジューラ（COM 自動化の制約により**ログオン中のみ実行**）。

取得範囲は既定で「前日〜7日後」。終日・辞退・キャンセル・空き時間扱いの予定は
実績の材料にすると集計が壊れるため除外する。詳細は `docs/20260923_outlook_calendar.md`。

### 4.3 Activity / Change / Decision / Task の分離

```
Meeting（Activity）
   ↓
「API利用不可が判明」（Change）
   ↓
「CSV連携方式に変更」（Decision）
   ↓
「CSV PoCを作成」（Task）
```

数か月後に「なぜこの設計になったのか」を復元できることが目的。

### 4.4 正の置き場所

| データ | 正 |
| --- | --- |
| raw_events / sessions / activities | ローカル SQLite |
| changes / decisions / tasks | GitHub（長期）。SQLite は同期キャッシュ |

GitHub には**生ログを入れない**。ウィンドウタイトルには顧客名・メール件名・社内システム名が入りうるため。

---

## 5. Current State

Context Builder が SQLite と長期コンテキストから 1 つの状態へまとめる。
Decision Engine へ渡すのは `state_to_flat_dict()` の平坦な dict のみで、生ログは含めない。

`CurrentState` は対象日（`today`）1日ぶんだけでなく、過去N日の傾向（`past`）と
今後N日の予定・締切（`upcoming`）も持つ。

| 範囲 | フィールド | 期間 | 読む層 |
| --- | --- | --- | --- |
| 当日 | `today` | 対象日 | 確定 → 観測 |
| 過去 | `past` | 対象日の前 `[context] past_days` 日（対象日を含まない） | 確定 → 観測（**日ごとに**判定） |
| 未来 | `upcoming` | 基準時刻から先 `[context] upcoming_days` 日 | **`planned` を直接読む** |

`upcoming` が `planned` を直接読むのは、確定層のフォールバック規則（確定→観測→手入力→予定）に
未来を混ぜると当日の時間集計が壊れるため。`past` を日ごとに読むのは、ある日は確定・別の日は
観測という混在を正しく扱うため。設定キーは `docs/20260923_config_reference.md` の `[context]` を参照。

主なキー。

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

`recent_context_switches`（当日の切替回数）と `past_context_switches`（過去N日の合計）は別物。
キーの全量・型は `docs/20260923_module_contract.md` を参照。

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

「VS Code を2時間使った」ではなく、`deep_work_min` `context_switches` のような
**判断に使える特徴量**へ変換するのが要点。

---

## 6. Decision Engine

### 6.1 インターフェース

アプリ側は次の3つだけを呼ぶ。

```python
engine.noul(state, key, instruction)         # yes / no
engine.choice(state, key, choices, ...)      # 選択
engine.score(state, key, min_value, max_value)  # スコア
```

複数の問いをまとめたいときは `DecisionRequest(state, questions)` を `ask()` へ渡す。
LLM には文章を書かせず、JSON だけ返させる（＝賢い if 文として使う）。

### 6.2 Adapter

| 名前 | 内容 |
| --- | --- |
| `rule_based` | 既定。LLM もネットワークも使わず、特徴量から素朴に判定 |
| `claude` | Anthropic 公式 SDK。`output_config.format` の JSON Schema で構造化出力 |
| `openai_compat` | OpenAI 互換 API（Ollama など） |
| `jev` | Jev ネイティブ形式の薄い HTTP Adapter |

### 6.3 運用方針（mode）とエンジンの退避チェーン

どの Adapter を使うかは、config の `[decision] mode` 1行で切り替える。

```
mode = "jev_first"
        ↓
[decision.modes.jev_first] engines = ["jev", "claude", "rule_based"]
        ↓
   jev を試す ── 失敗（APIキー無し・通信不可）
        ↓ 警告を出して退避
   claude を試す ── 失敗
        ↓ 警告を出して退避
   rule_based ── 必ず答える
```

`registry.create_engine_chain()` が `FallbackEngine` を組み立てる。
`engines` の**末尾は必ず `rule_based`** にしておき、判断が止まらないようにする。

`FallbackEngine` は基底の `ask()` を上書きし、子エンジンの応答を**加工せず**返す。
親で再度校正すると、子が校正済みの confidence を生の値で上書きしてしまうため。

方針は Planner の担当も決める（`planner = "claude" | "offline"`）。
`rule_first` では LLM を一切呼ばず、計画文もローカル生成する。

### 6.4 confidence の校正

一般の LLM の自己申告 confidence は過信しやすく、`0.93` をそのまま 93% として扱ってはいけない。

```
LLM判断 → raw_confidence → 実測（feedback）→ 校正 → 実用 confidence
```

`decision_feedback` に「予測 / 実際 / 正解だったか」を貯め、`calibrate` で
isotonic（PAV）または Platt を当てはめる。サンプルが少ない間は恒等変換のまま。
`confidence_threshold` 未満の判断は通知せず保留にする（集中を壊さないため）。

### 6.5 空白時間の補完

PC 操作が無い時間を「休憩」と断定しない。`app/cf.py gaps` が次の順で扱う。

```
確定 activity から「証拠のある区間」を取る
   ※ カレンダー由来は証拠ではないので除く
        ↓
find_gaps で空白を洗い出す
        ↓
予定の開始・終了時刻で空白を切り分ける
   ※ 長い空白を丸ごと1種別と断定しないため
        ↓
gap_fill 質問セットへ（渡すのは集計値と「予定の有無」だけ。予定名は渡さない）
        ↓
校正後 confidence >= 閾値 のものだけ
layer=REPORTED / source=SYSTEM で記録
```

`source=SYSTEM` としておくことで、AI推定であることが後から分かり、
本人の手入力（`source=MANUAL`）と区別できる。

---

## 7. プライバシー

- ウィンドウタイトルはマスク規則（メールアドレス・URL）を適用してから保存
- `privacy.drop_window_title = true` でタイトル自体を保存しない運用も可能
- LLM へ渡すのは集計値のみ。生ログは渡さない
- GitHub へ出すのは日次サマリ・変化・判断・タスクのみ

---

## 8. 実装の進め方（参照資料の順序）

1. Windows Logger（完了）
2. Sessionizer（完了）
3. Daily Summary（完了）
4. Git Context（ローカル Markdown まで完了。Issue 同期は token 設定で有効化）
5. Context Builder（完了）
6. Jev互換 Decision API（完了）
7. LLM Planner（完了）
8. Feedback / Calibration（完了）

今後の拡張候補。

- 空白時間の自動補完（`find_gaps` + `gap_fill` 質問セットの常時運用）
- 週次サマリ（`activity/weekly/`）
- 実際の Jev エンドポイントに合わせた `jev.py` の調整
- 通知 UI（confidence 閾値超え かつ 重要な状態変化があるときだけ出す）
