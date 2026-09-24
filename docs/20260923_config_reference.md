# 設定リファレンス

作成日: 2026-09-23
対象: `app/source/config/config.toml` / `categories.toml` / `questions.toml`

設定は TOML。Python 3.11 以降の標準ライブラリ `tomllib` で読むため、追加インストールは不要。

---

## config.toml

### [paths]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `database` | `app/data/contextflow.db` | ローカル SQLite。生ログの置き場所 |
| `secrets_file` | `.env` | APIキー等の置き場。相対パスはプロジェクトルート基準 |
| `state_json` | `app/data/state.json` | Context Builder の出力 |
| `export_dir` | `app/data/export` | 日次サマリ等の Markdown 出力先 |
| `context_repo` | `app/data/context_repo` | 仕事管理リポジトリの**場所**（中の構成は `[context_repo]` で指定） |
| `calendar_dir` | `app/data/calendar` | ここに置いた `.ics` を取り込む |

相対パスはプロジェクトルート基準。絶対パスも指定可能。

### 秘密情報の扱い

`AppConfig.secret("llm.claude.api_key_env")` のように、`api_key_env` /
`token_env` で指定した環境変数名から APIキー等の値を取得する。探す順は次のとおり。

1. **同名の環境変数**
2. **秘密情報ファイル**（`[paths] secrets_file`。既定はプロジェクトルート直下の `.env`）

環境変数を先に見るのは、一時的な上書き（別のキーで試す・CI で渡す）を効かせるため。
どちらも無ければ `None` を返す（**キーが無くても落とさない**。使えないエンジンは
警告を出して次へ退避する既存の挙動のまま）。

`api_key_env` / `token_env` の値は**環境変数名であり、秘密情報ファイル側のキー名でもある**
（`[llm.claude] api_key_env = "ANTHROPIC_API_KEY"` なら、環境変数も `.env` 内のキーも
`ANTHROPIC_API_KEY`）。

秘密情報ファイルの形式（`KEY=VALUE`）。

- `#` で始まる行と空行は無視する
- 先頭の `export ` は取り除く（シェル用の書き方をそのまま貼れる）
- 値を囲む `"` `'` は取り除く
- 名前・値の前後の空白は落とす
- `=` を含まない行は無視する
- ファイルが無い・読めない場合は空として扱う（落とさない）

値は**ログに出さない**。

テンプレートはリポジトリ直下の `.env.example`（コミット対象）。
`.env.example` を `.env` へコピーして使う。`.env` 自体は `.gitignore` で除外済み。

### [collector]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `interval_sec` | `5` | 前面ウィンドウの採取間隔 |
| `idle_threshold_sec` | `180` | これ以上無操作なら idle とみなし session を切る |
| `flush_every` | `12` | 何件たまったら SQLite へ書き込むか |
| `flush_max_wait_sec` | `10` | 件数がたまらなくても、この秒数を超えたら書き出す |

`flush_max_wait_sec` は、収集中でも画面の再読み込みで直近の生ログを見られるようにするための設定。
これが無いと、最大 `flush_every × interval_sec` 秒ぶん（既定で60秒）保存が遅れる。

### [privacy]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `drop_window_title` | `false` | `true` にするとウィンドウタイトル自体を保存しない |
| `mask_patterns` | メール・URL | 正規表現 → 置換文字列の配列 |

```toml
[[privacy.mask_patterns]]
pattern     = '[\w.+-]+@[\w-]+\.[\w.]+'
replacement = '<mail>'
```

正規表現は TOML のリテラル文字列（シングルクォート）で書く。バックスラッシュのエスケープが不要になる。

### [sessionizer]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `merge_gap_sec` | `60` | この秒数以内の間隔なら同じ session に連結 |
| `min_duration_sec` | `30` | これ未満の session は破棄 |
| `merge_by_process_only` | `false` | `true` ならタイトル違いも同一 session にまとめる |

### [features]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `deep_work_min_sec` | `1500` | これ以上続いた作業を deep work とみなす（25分） |
| `context_switch_min_sec` | `60` | これ未満の滞在はコンテキストスイッチに数えない |

### [context]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `past_days` | `7` | 過去何日ぶんの傾向を見るか（対象日は含まない） |
| `upcoming_days` | `7` | 今後何日ぶんの予定・締切を見るか |

`CurrentState` は対象日（`today`）だけでなく、対象日を含まない過去 `past_days` 日ぶんの傾向（`past`）と、
基準時刻から先 `upcoming_days` 日ぶんの予定・締切（`upcoming`）を持つ。

| 範囲 | フィールド | 読む層 |
| --- | --- | --- |
| 当日 | `today` | 確定 → 観測 |
| 過去 | `past` | 確定 → 観測（**日ごとに**判定） |
| 未来 | `upcoming` | **`planned` を直接読む** |

`upcoming` が `planned` を直接読むのは、確定層のフォールバック規則（確定→観測→手入力→予定）に
未来を混ぜると当日の時間集計が壊れるため。`past` を日ごとに読むのは、ある日は確定・別の日は
観測という混在を正しく扱うため。

### [decision]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `mode` | `rule_first` | 運用方針。`rule_first` / `llm_first` / `jev_first` |
| `engine` | `""` | 単一エンジンを直接指定したいときだけ使う（`mode` より優先） |
| `confidence_threshold` | `0.7` | 校正後 confidence がこれ未満なら保留扱い |
| `question_set` | `next_action` | 既定で使う質問セット名 |

**運用方針の切り替えはこの `mode` 1行**。現在の選択は `python app/cf.py mode` で確認できる。

### [decision.modes.*]

方針ごとに「判断エンジンの並び」と「Planner の担当」を持つ。

| キー | 内容 |
| --- | --- |
| `description` | 説明。`app/cf.py mode` に表示される |
| `engines` | 判断エンジンを左から順に試す。失敗したら次へ退避する |
| `planner` | 人間向けの説明・計画を誰が書くか。`offline` / `claude` / `openai_compat`（`PLANNER_PROVIDERS`）のいずれか |

`planner` に上記以外の値を書くと、`load_modes` / `resolve_mode` が
**その場で `ValueError`** になる（未知の提供元を黙って `claude` へ流さないため）。

```toml
[decision.modes.jev_first]
description = "Jev で小さな判断、LLM は説明・計画"
engines     = ["jev", "claude", "rule_based"]
planner     = "claude"
```

同梱の3方針。

| mode | engines | planner |
| --- | --- | --- |
| `rule_first` | `rule_based` | `offline` |
| `llm_first` | `claude` → `rule_based` | `claude` |
| `jev_first` | `jev` → `claude` → `rule_based` | `claude` |

方針を追加する場合も、`engines` の**末尾は必ず `rule_based`** にしておく。
そうすれば APIキーが無い・通信できない状況でも判断が止まらない。

`decision.engine` に値を入れると、`mode` より優先して単一エンジンを使う
（この場合も末尾に `rule_based` が自動で足される）。`--engine` オプションも同じ扱い。

### [llm.claude]

判断（Decision Engine、`decision/adapters/claude_api.py`）が使う接続情報。

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `model` | `claude-opus-5` | モデルID。日付サフィックスは付けない |
| `max_tokens` | `2000` | 出力上限 |
| `effort` | `low` | 判断は分類に近い小さな問いなので低 effort |
| `api_key_env` | `ANTHROPIC_API_KEY` | APIキーを読む環境変数名 |

### [llm.planner]

計画生成（Planner、`planner/planner.py` + `planner/llm_client.py`）が使う設定。
**提供元（claude / openai_compat）は `[decision.modes.*] planner` で決まる**
（このセクションでは選べない）。`max_tokens` / `effort` はここだけを見る
（`effort` は `claude` のときだけ使う）。

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `model` | `""` | モデルID。空なら `[llm.<provider>]` の値を使う |
| `max_tokens` | `4000` | 出力上限。計画文は判断より長くなるため多め |
| `effort` | `high` | 説明・計画は高 effort（`claude` のみ使う） |
| `base_url` | `""` | `claude` 以外を使うときの接続先。空なら `[llm.<provider>]` の値を使う |
| `api_key_env` | `""` | 同上。APIキーを読む環境変数名。空なら `[llm.<provider>]` の値を使う |

`model` / `base_url` / `api_key_env` の解決順は「`[llm.planner]` → `[llm.<provider>]`」。
`model` を空のままにしておくと、提供元を切り替えたときにモデル名も自動で追従する。
ここを Claude 用の値で固定すると、OpenAI互換の接続先へ `claude-opus-5` のような
無効なモデル名を送ってしまう。**解決できない場合は通信せず、オフライン生成へ退避する。**

例えば `planner = "openai_compat"` で `[llm.planner] base_url` が空なら
`[llm.openai_compat] base_url` を使う。

### [llm.openai_compat]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `base_url` | `http://localhost:11434/v1` | Ollama 等の OpenAI 互換エンドポイント |
| `model` | `qwen2.5:14b` | モデル名 |
| `api_key_env` | `OPENAI_API_KEY` | キーが不要なら未設定でよい |

### [llm.jev]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `base_url` | `https://api.typesafe.ai/v1` | Jev のエンドポイント |
| `model` | `jev-1` | モデル名 |
| `api_key_env` | `JEV_API_KEY` | APIキーを読む環境変数名 |

実際のエンドポイント仕様に合わせて `decision/adapters/jev.py` を調整する前提の薄い Adapter。

### [context_repo]

長期コンテキスト（仕事管理リポジトリ）の**構成**。既定値は、連携先の仕事管理リポジトリの構成に合わせている。

**場所は `[paths] context_repo`、構成はこのセクション。**
両者を合わせてパスが決まる。

```
C:/path/to/work-context / 10_projects / <prefix>_<yyyymm>_<suffix> / tasks.md
└─ paths.context_repo ─┘ └─ projects_dir ─┘                        └ tasks_file
```

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `layout` | `work_repo` | `work_repo`＝既存リポジトリに合わせる（フォルダを作らない） / `standalone`＝contextflow 専用構成を作る |
| `projects_dir` | `10_projects` | 案件フォルダ `<prefix>_<yyyymm>_<suffix>` の置き場所 |
| `knowledge_dir` | `20_knowledge` | ノウハウ領域 |
| `inbox_dir` | `00_inbox` | 一時メモ |
| `tasks_file` | `tasks.md` | 案件フォルダ内。**読み取り専用** |
| `decisions_file` | `decisions.md` | 案件フォルダ内。判断・変化の追記先 |
| `project_readme` | `README.md` | 案件の現在地 |
| `constraints_file` | `''` | 制約を書いたファイルの相対パス。空なら読まない |
| `project_key` | `suffix` | 案件キー。`suffix`（`pjt:` ラベルと同じ）/ `prefix` / `folder` |
| `write_daily` | `false` | 日次の時間集計をリポジトリへ書き出すか |
| `daily_dir` | `20_knowledge/90_daily_activity` | `write_daily = true` のときの出力先 |

`write_daily` が既定で false なのは、連携先が
「具体的な実施日時は Outlook のカレンダーで管理し、本リポジトリでは管理しない」方針のため。

連携の詳細は `docs/20260923_work_repo_integration.md`。

### [ui.question_set_labels] / [ui.choice_labels]

画面に出す質問セット名と、`choice` 型の選択肢の表示名。**保存値は変えない。**

```toml
[ui.question_set_labels]
next_action = "次の行動"

[ui.choice_labels.focus_project]
deadline = "締切が近い案件"
```

`[ui.choice_labels]` のキーは**質問の `key`**。ここに無い質問は `[ui.activity_labels]` を試し、
それも無ければ保存値をそのまま出す。`gap_activity_type` のように選択肢が種別そのものの質問は、
個別指定なしで日本語になる。

### [ui.activity_labels]

種別（`ActivityType`）の**表示ラベル**。キーは DB に保存される値（英語）。

```toml
[ui]
hidden_activity_types = ["planning"]   # 選択肢に出さない種別（既定値）

[ui.activity_labels]
coding   = "コーディング"
research = "情報収集"
meeting  = "会議"
```

`hidden_activity_types` は選択肢に出さない種別。**`ActivityType` からは消さない**ため、
過去データの値はそのまま読める。既定では `planning`（計画）を `thinking`（思考）へ集約し、
`planning` を選択肢から外している。`planning` の表示ラベルも `思考` にしてあるので、
過去に記録した `planning` も画面では `思考` と出る。

これは**既定値**であり、画面の設定で保存した表示/非表示があればそちらが優先される
（設定画面でチェックを戻せば、`planning` を再び選べる）。

**保存値は変わらない。** 表示だけを日本語にする設定。
画面から変更した内容は `app/data/ui_options.json` へ保存され、ここの値へ上書き適用される。

`config.toml` は同梱の既定値（Git 管理・読み取り専用）、画面が書き換える値は `app/data`（Git 管理外）。
画面の操作でリポジトリが汚れないよう分けている。

### [calendar]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `provider` | `outlook` | 取得元。`outlook`（classic Outlook COM）/ `ics`（`.ics` ファイル） |
| `fetch_days` | `7` | 今日から何日先までの予定を取るか |
| `fetch_days_back` | `1` | 何日前まで取り直すか（前日に入った変更の取りこぼし対策） |
| `skip_all_day` | `true` | 終日予定を除外する |
| `skip_declined` | `true` | 自分が辞退した予定を除外する |
| `skip_cancelled` | `true` | キャンセル済みの予定を除外する |
| `skip_free` | `true` | 予定表で「空き時間」扱いの予定を除外する |
| `mask_subject` | `true` | 件名と場所に `privacy.mask_patterns` を適用する |

`provider = "outlook"` は `pywin32` が必要。使えない場合は警告を出して `ics` へ退避する。
取得元の状態は `python app/cf.py calendar status` で確認できる。

`skip_*` はすべて「実績の材料にすると時間集計が壊れるもの」を除くための設定。
終日予定（祝日など）を 24 時間の会議として数えない、辞退した会議を参加時間に含めない、といった意図。

件名は日次サマリへ出力されるため、`mask_subject` は既定で有効にしてある。

セットアップ手順と取得仕様の詳細は `docs/20260923_outlook_calendar.md`。

### [github]

| キー | 既定値 | 内容 |
| --- | --- | --- |
| `enabled` | `false` | GitHub 連携の有効化 |
| `repo` | `''` | `owner/repo` 形式（Issue API 用） |
| `token_env` | `GITHUB_TOKEN` | トークンを読む環境変数名 |
| `remote` | `''` | push 先 URL（例 `git@github.com:owner/work-context.git`） |
| `branch` | `main` | push するブランチ |
| `auto_push` | `false` | `github export` のあと自動で commit & push するか |
| `issue_sync` | `true` | `repo` と token が揃っていれば Issue を取り込むか |
| `issue_labels.*` | `status:` 等 | Issue ラベルの接頭辞。連携先の体系に合わせる |

`[github.issue_labels]` は `status_prefix` / `when_prefix` / `priority_prefix` / `project_prefix` の4つ。
contextflow は Issue を**読むだけ**で、起票・更新・クローズは行わない
（Issue は `tasks.md` の写しであり、変更は承認制の同期処理が担うため）。

**リポジトリ未作成のうちは `enabled = false` のままでよい。**
その場合も長期コンテキストは `paths.context_repo` にローカル蓄積され、
`github pull` は `projects/*/tasks.md` からタスクを読む。

リポジトリ作成後は `enabled` / `repo` / `remote` を埋め、
`app/cf.py github init-repo` → `app/cf.py github push` で連携が始まる。
設定状況は `app/cf.py github status` で確認できる。

---

## categories.toml

プロセス名・ウィンドウタイトルから `activity_type` と `project` を推定するルール。
LLM を使わない素朴な分類で、評価順は **title_rules → process_rules → default**。

```toml
default_activity_type = "other"

[[title_rules]]
contains      = ["Teams 会議", "Zoom Meeting"]
activity_type = "meeting"

[[process_rules]]
match         = ["Code.exe", "devenv.exe"]
activity_type = "coding"

[[project_rules]]
contains = ["サンプル案件", "sample-project"]   # タイトル側のキーワード（表記は自由）
project  = "sample_project"               # 出力する案件名（案件フォルダの suffix に揃える）
```

| セクション | キー | 内容 |
| --- | --- | --- |
| `title_rules` | `contains` / `activity_type` | タイトル部分一致（大文字小文字を無視） |
| `process_rules` | `match` / `activity_type` | プロセス名の一致（大文字小文字を無視） |
| `project_rules` | `contains` / `project` | タイトル・プロセス名からプロジェクトを推定 |

`activity_type` に使える値: `coding` / `research` / `meeting` / `document` / `communication` /
`thinking` / `planning` / `review` / `admin` / `break` / `other` / `unknown`

### project_rules の `project` は案件フォルダの suffix に揃える

`contains`（一致させるキーワード）と `project`（出力する案件名）は別のフィールド。
ウィンドウタイトルには日本語の表示名が入るので `contains` は日本語のままでよいが、
**`project` は `10_projects/<prefix>_<yyyymm>_<suffix>` の suffix（＝`pjt:` ラベルと同じ値）に揃える。**

揃っていないと、同じ案件なのに次のように割れる。

```
by_project: {'サンプル案件': 60, 'sample_project': 45}     # PCログ由来とタスク由来が別集計
判断を書けない（案件 'サンプル案件' が見つからない）   # decisions.md へ追記できない
```

認識されている案件名は `python app/cf.py init` で確認できる。

---

## questions.toml

Decision Engine へ投げる質問セット。1回のリクエストに複数の問いをまとめられる。

```toml
[next_action]
description = "今この瞬間、作業を続けるべきかの判断"

[[next_action.questions]]
key         = "continue_current_task"
type        = "noul"
instruction = "現在のタスクを継続すべきか"
```

| キー | 内容 |
| --- | --- |
| `key` | 回答の識別子。フィードバック・校正もこの単位 |
| `type` | `noul`（yes/no） / `choice`（選択） / `score`（整数スコア） |
| `instruction` | 問いの文面 |
| `choices` | `choice` のときの選択肢（2つ以上） |
| `min` / `max` | `score` のときの範囲 |

### Decision Engine へ渡るキー

`state_to_flat_dict`（`context/builder.py`）が Decision Engine へ渡す平坦 dict のキーは
26個から40個に増えた。追加分は過去 `past_days` 日ぶんの傾向と、今後 `upcoming_days` 日ぶんの
予定・締切。

```
past_days, past_total_min, past_by_type, past_by_project, past_deep_work_min,
past_context_switches, past_active_days, past_change_count, past_decision_count,
upcoming_days, upcoming_planned_min, upcoming_by_type, upcoming_items, upcoming_deadlines
```

**注意**: `recent_context_switches` は**当日**の切替回数（既存）、`past_context_switches` は
**過去N日の合計**（新規）で別物。`recent_changes` / `recent_decisions` も「直近の数件」であって
期間集計ではない。取り違えやすいため明記する。

キーの全量・型・JSON の形は `docs/20260923_module_contract.md`（`context/builder.py`）を参照。

### 同梱の質問セット

| セット名 | 用途 | 問い |
| --- | --- | --- |
| `next_action` | 今の作業を続けるべきか | `continue_current_task` / `next_task_type` / `urgency` |
| `gap_fill` | PC操作が無い空白時間の推定 | `gap_activity_type` / `gap_is_work` |
| `task_triage` | タスク1件ごとの仕分け | `is_blocked` / `do_today` / `priority` |
| `week_outlook` | 今後の予定・締切からの見通し判断 | `capacity_is_tight` / `focus_project` / `week_risk` |
| `deadline_risk` | 直近の締切のリスク判断 | `deadline_at_risk` / `needs_reschedule` / `deadline_pressure` |

質問セットを追加した場合、`rule_based` エンジンは未知の key に対しても
低い confidence（0.3程度）で既定値を返すため、設定だけで壊れることはない。

`week_outlook` / `deadline_risk` は材料が無いときに断定しない。`upcoming_deadlines` が空・
`past_active_days` が 0 などの場合は、値を `None`（noul）や中央値（score）にし、
confidence を 0.3〜0.4 に抑え、`rationale` に「材料が無い」旨を書く。
**未来の判断は答え合わせ（`feedback`）が遅れて校正が効きにくい**ため。

実測。

```
材料が無い場合   capacity_is_tight = None  conf=0.30  保留
                 week_risk         = 3     conf=0.30  保留
予定が詰まり、
2日後の締切が待ち capacity_is_tight = True  conf=0.70
                 deadline_pressure = 4     conf=0.70
```
