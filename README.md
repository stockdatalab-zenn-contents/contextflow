# contextflow

Windows の操作ログと、判断・状況変化の長期コンテキストを束ね、
Jev風の型付き判断（yes/no・選択・スコア）で「次に何をすべきか」を支援する仕組み。

---

## セットアップ

### 1. 前提

- Windows 11
- Python 3.12 以上（**標準ライブラリのみで基本機能が動く**。追加インストール不要）

### 2. 初期化

プロジェクトルートで実行する。

```powershell
python app/cf.py init
```

`app/data/` に SQLite（`contextflow.db`）・出力先（`export/`）・カレンダー取り込み先（`calendar/`）が作られる。

長期コンテキストの置き場所は**作られない**。既定の `layout = "work_repo"` は
「既存の仕事管理リポジトリに合わせ、独自のフォルダを作らない」設定のため。
`init` は認識できた案件名を表示するので、ここで案件が出なければ **5.** を設定する。

この時点で `collect` 以降の基本機能は動く。3〜6 はすべて任意。

### 3. 運用方針を選ぶ

`app/source/config/config.toml` の `[decision] mode` **1行**で切り替わる。

```toml
[decision]
mode = "rule_first"   # rule_first | llm_first | jev_first
```

| mode | 判断エンジン（左から順に試し、失敗したら次へ退避） | Planner | 向いている場面 |
| --- | --- | --- | --- |
| `rule_first` | `rule_based` | オフライン | 既定。費用ゼロで運用し、feedback を貯めてから LLM へ移行する |
| `llm_first` | `claude` → `rule_based` | Claude | 判断の質を優先。校正データが貯まるまで confidence は参考値 |
| `jev_first` | `jev` → `claude` → `rule_based` | Claude | Jev で小さな判断、LLM は説明・計画（参照資料の二段構え） |

現在の選択は `python app/cf.py mode` で確認できる。
一時的に試すだけなら `python app/cf.py decide --mode llm_first` のように上書きできる。

各方針の中身（エンジンの並び・Planner の担当）は `[decision.modes.*]` で調整可能。

### 4. LLM を使う場合（`llm_first` / `jev_first` のとき）

`rule_first` のままなら不要。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

APIキーを環境変数に設定する。

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."   # llm_first / jev_first の Claude 用
setx JEV_API_KEY "..."                # jev_first のとき
```

キーが無くても落ちない。使えないエンジンは警告を出して次へ退避する。

### 5. 仕事管理リポジトリと連携する場合（任意・後からでよい）

リポジトリ未作成でも、長期コンテキストは `app/data/context_repo` にローカル蓄積される。

既存の仕事管理リポジトリがある場合は、そのクローンを指すだけで構成に合わせて動く。
**手元に無い場合は `references/sample_work_repo/` を丸ごとコピーして使える**（`HOWTO.md` に手順）。

```toml
[paths]
context_repo = "C:/path/to/work-context"   # リポジトリの場所（クローン先）

[context_repo]
layout       = "work_repo"   # 既定。既存リポジトリの構成に合わせ、独自フォルダを作らない
projects_dir = "10_projects" # 場所の直下にある案件フォルダの名前
```

**`[paths]` は「どこにあるか」、`[context_repo]` は「その中がどういう構成か」。**
両者を合わせてパスが決まる。

```
C:/path/to/work-context / 10_projects / <prefix>_<yyyymm>_<suffix> / tasks.md
└─ paths.context_repo ─┘ └─ context_repo.projects_dir ─┘          └ context_repo.tasks_file
```

フォルダ名が違うリポジトリでも、`[context_repo]` 側の名前を変えれば合わせられる。

- `10_projects/*/tasks.md` からタスクを読む（`## Next` `@doing` `!high` `#番号` などの記法に対応）
- 判断・変化は案件の `decisions.md` へ追記する
- **`tasks.md` と GitHub Issue は読み取りのみ**（Issue の起票・更新・クローズはしない）
- 日次の時間集計は既定で書き出さない（リポジトリの管理方針に合わせるため）

詳細は `docs/20260923_work_repo_integration.md`。

Issue 連携・push を使う場合は設定を埋める。

```toml
[github]
enabled   = true
repo      = "owner/work-context"                      # Issue 取り込み用
remote    = "git@github.com:owner/work-context.git"   # push 用
auto_push = true                                      # export 後に自動で commit & push
```

```powershell
setx GITHUB_TOKEN "ghp_..."
python app/cf.py github status      # 設定状況と不足しているものを表示
python app/cf.py github push        # commit して push
```

既存リポジトリのクローンを指している場合、`github init-repo` は不要
（既に git リポジトリのため何もしない）。リポジトリが無い状態から始める場合のみ使う。

```toml
[context_repo]
layout = "standalone"   # リポジトリが無いとき。contextflow 専用の構成を自分で作る
```

### 6. Outlook の予定を日次取得する場合（任意）

普段使いは新 Outlook のまま、**classic Outlook を Python 用の予定取得アダプタとして使う**。
新 Outlook には COM が無いため、classic Outlook 側から読む（同じメールボックスを見るので中身は同じ）。

```powershell
pip install pywin32                    # requirements.txt に含まれる
python app\cf.py calendar status       # 取得できる状態か確認
python app\cf.py calendar sync         # 直近1週間ぶんを取得

# 日次実行をタスクスケジューラへ登録（登録のみ。実行はしない）
powershell -ExecutionPolicy Bypass -File app\scripts\register_calendar_task.ps1
```

classic Outlook 側に同じアカウントを設定しておくこと。
COM 自動化の制約により、タスクは**ログオン中のみ実行**で登録される。

#### 取得した予定の保存先

**ローカル SQLite（`app/data/contextflow.db`）の `activities` テーブル**。ファイルとしては残らない。

```
区間  : 2026-09-24 10:00 〜 11:00
種別  : meeting / layer=planned / source=calendar
件名  : A社打合せ <mail>          ← privacy.mask_patterns 適用後
detail: 場所・主催者・UID・busy/response 状態
```

- `layer=planned` … **予定であって実績ではない**ことを示す。`build` で実績とマージされる
- `app/data/calendar/` は `.ics` の**入力**置き場。取得結果の出力先ではない
- **仕事管理リポジトリには書かない**（「実施日時はリポジトリで管理しない」方針のため）

#### 再取得したときの挙動

取得範囲（既定で**前日〜7日後**）の予定を、**日ごとに丸ごと入れ替える**。差分更新ではない。

| Outlook 側の変更 | 挙動 |
| --- | --- |
| 時刻・件名の変更 | 新しい内容に置き換わる（古い方は残らない） |
| 予定の削除 | 消える |
| 予定の追加 | 増える |
| **取得範囲の外（2日前より古い日）** | **触らない**。遡って直すなら `--days-back 30` のように範囲を広げる |

**`calendar sync` だけでは画面・集計に反映されない。**
タイムライン・日次サマリ・判断材料が見ているのは `build` が作る確定層のため。

```
calendar sync 直後
  planned  : 11:00-12:00 定例会議（時間変更）   ← 新しい
  confirmed: 10:00-11:00 定例会議               ← 古いまま

build 実行後
  confirmed: 11:00-12:00 定例会議（時間変更）
```

日次実行に登録されるのは `calendar sync` のみ。その日の作業を見る前に `build`
（画面では `[タイムラインを作り直す]`）を実行する。

手入力した実績は予定より優先されるため、予定を取り直しても消えない。

予定の種別は既定で「会議」固定。ブラウザ画面（`ui`）の「予定の分類」から `uid` ごとに種別を付けられ、
再取得しても消えない。

手順の詳細・取得仕様・つまずいたときの対処は `docs/20260923_outlook_calendar.md`。

---

## 使い方

### 基本の流れ

```
collect      操作ログを貯める
   ↓
sessionize   5秒ログを「VS Code 42分」のような区間へ圧縮
   ↓
build        予定・観測・手入力をマージして確定 activity にする   ← 図の Activity Store
   ↓
state        Current State（判断用の現在地）を組み立てる          ← 図の Context Builder
   ↓
decide       Jev風 Decision Engine に小さな判断をさせる
   ↓
plan         判断結果をもとに人間向けの説明・計画を作る
```

`build` は「処理の流れ」の図の **Activity Store** にあたる工程で、図の **Context Builder** は
`state` が担う。名前が似ているが別物。

### 1日の典型的な使い方

以下は CLI での操作。**日中の記録と確認はブラウザからも行える**（→「ブラウザから操作する」）。

`--project` に渡す値は、仕事管理リポジトリの案件フォルダ
`10_projects/<prefix>_<yyyymm>_<suffix>` の **suffix**（`pjt:` ラベルと同じ値）。
認識されている案件名は `python app/cf.py init` で確認できる。

```powershell
# 朝：ログ収集を開始（別ウィンドウで動かしたままにする。Ctrl+C で終了）
python app/cf.py collect

# 予定を取り込む（タスクスケジューラへ登録済みなら不要）
python app/cf.py calendar sync

# タスクを取り込む（tasks.md または GitHub Issue）
python app/cf.py github pull
```

日中は、PCに残らない作業と、状況の変化・判断を記録する。

```powershell
# 会議・思考・相談など
python app/cf.py work start meeting --project sample_project --task 要件整理
python app/cf.py work stop

# 後から補完することもできる
python app/cf.py work add 13:00 13:45 meeting --project sample_project --summary "関係者と相談"

# 状況の変化・判断を残す（ここが長期コンテキストの中身になる）
python app/cf.py change add "先方のAPI利用が不可と判明" --project sample_project
python app/cf.py decision add "CSV連携方式に変更" --reason "API利用不可のため" --project sample_project
```

止め忘れて日付をまたいだ場合、`work stop` は**日ごとに分けて記録する**
（前日 23:50 開始 → 当日 00:10 終了なら 2件に分かれる）。1日ぶんの集計が崩れないようにするため。

夕方に集計する。**`build` は予定・観測・手入力をマージする工程なので、
予定の取り込みと空白時間の推定より後に実行する**。

```powershell
python app/cf.py sessionize        # 生ログを区間へ圧縮
python app/cf.py build             # 予定・観測・手入力をマージ

python app/cf.py gaps              # 操作ログが無い時間帯の種別を推定（確認のみ）
python app/cf.py gaps --apply      # 納得したら記録する
python app/cf.py build             # 推定を確定へ反映するため、もう一度マージ

python app/cf.py state             # Current State を組み立て（画面の [今の状態をまとめる] でも実行可）
python app/cf.py decide            # 型付き判断（画面の「判断・計画」でも実行可）
python app/cf.py plan              # 説明・計画（画面の「判断・計画」でも実行可）

python app/cf.py report --show     # 日次サマリ（ローカル）
python app/cf.py github export     # 変化・判断を案件の decisions.md へ追記
```

`gaps --apply` を使わない日は、`build` は1回でよい。

### ブラウザから操作する

CLI を打たずに、確認と記録をブラウザで行える。

```powershell
python app/cf.py ui
```

`http://127.0.0.1:8765` が開く。できること。

| 画面 | 内容 |
| --- | --- |
| タイムライン | **上部に俯瞰図**（縦=案件・種別、横=時間、濃さ=占有分数。刻みは1時間/30分/5分、並びは設定順/合計時間順）。その下に `案件 / 種別 / タスク / 情報源` で一覧。**同じ内容が続く行は開始〜終了をまとめて1行**。固定の高さでスクロール。**空白時間をクリックすると、時刻が埋まった補完フォームが開く** |
| 予定の分類 | **カレンダーの予定に種別を付ける**（既定は「会議」）。付けた種別は再取得しても残り、`build` にも反映 |
| 作業の記録 | `[記録を開始]` / `[記録を停止]` のボタン1つで操作（`work start` / `work stop` 相当）。記録中の内容は状態表示に出る |
| 変化・判断 | `時刻　案件　内容` で一覧。追加・編集・削除（`change add` / `decision add` 相当） |
| 日付ナビゲーション | **過去日の閲覧・編集にも対応** |
| 現在の状態 | **`state` の実行と参照**（`python app/cf.py state` 相当）。過去日は保存済みのスナップショットを参照。**当日に加えて過去7日の傾向・今後7日の予定と締切も出す** |
| 判断・計画 | **`decide` / `plan` の実行**（`python app/cf.py decide` / `plan` 相当）。質問セットは5種（`next_action` / `gap_fill` / `task_triage` / `week_outlook` / `deadline_risk`）から選んで実行し、保留（confidence が閾値未満）を明示 |
| 状態表示 | **生ログ収集・作業記録の両方を、内容つきで画面上部に常時表示**（作業記録は `案件 / 種別 / タスク / 開始 / 経過`。経過は30秒ごとに更新） |
| 収集の開始・停止 | **`collect` と同等の収集をボタンで操作**（この画面を閉じると止まる） |
| 生ログ | **既定では非表示**。明示的に開いたときだけ表示（ウィンドウタイトルに顧客名等が入りうるため）。固定の高さでスクロール。収集中でも再読み込みで最新を参照できる |
| 設定 | **案件・種別のラベル（日本語）・タスク候補を画面から編集**（並び順・表示/非表示も） |
| 生ログの紐づけ | **行を選んで案件・種別・タスクと紐づけて記録**（Shift+クリックで範囲選択） |

作業記録は経過8時間以上で止め忘れの警告を出す。収集・記録は常に**今日**に対して動くため、
過去日を表示中は「今日の状態」であることと、開始・停止すると今日に記録される旨を添えて出す。

`[判断する]` / `[計画を作る]` は押すたびに「現在の状態」をまとめ直してから判断するため、
先に `[今の状態をまとめる]` を押す必要はない。外部へ問い合わせる運用方針（`jev_first` / `llm_first`）
のときだけ確認を求める。

案件と種別はドロップダウンから選ぶ（自由入力にしないことで、案件名の表記ゆれを防ぐ）。
入力欄・一覧・俯瞰図とも **案件・種別・タスクの順**にそろえている。

編集できるのは手入力・AI推定の活動のみ。PCログ由来・予定由来は `build` の生成物なので編集できない。
編集後は画面の `[タイムラインを作り直す]` を押す（予定の取り込み・生ログの圧縮のあとも同じ）。

「現在の状態」は確定したタイムラインを材料にするため、記録を足したら
`[タイムラインを作り直す]` → `[今の状態をまとめる]` の順で押す。

収集はこの画面のプロセス内で動くため、**画面を閉じると収集も止まる**。
終日動かすなら、従来どおり別ウィンドウで `python app/cf.py collect` を使う。
CLI で収集中のときは、画面側からは開始・停止できない（生ログの重複を防ぐため）。

手動起動。`127.0.0.1` 固定で待ち受けるため、他のPCからは触れない。Ctrl+C で終了。

詳細は `docs/20260923_ui_design.md`。

### コマンド一覧

| コマンド | 内容 |
| --- | --- |
| `init` | DB とディレクトリを初期化 |
| `collect [--duration SEC] [--once]` | 前面ウィンドウ・idle を収集 |
| `sessionize [--date]` | 生ログを session へ圧縮 |
| `build [--date]` | 予定・観測・手入力をマージして確定 activity を作る |
| `work start/stop/add/status/say` | 非PC作業の手入力（`say` は自然文をLLMで構造化） |
| `calendar sync/import/status` | 予定の取得（既定は classic Outlook / `.ics` も可） |
| `state [--json]` | Current State を組み立てて保存 |
| `gaps [--apply]` | PC操作が無い空白時間を洗い出し、種別を推定 |
| `ui [--port] [--no-browser]` | ブラウザから確認・記録（ローカル起動） |
| `mode` | 現在の運用方針と選択肢を表示 |
| `decide [--set] [--mode] [--engine]` | 型付き判断（noul / choice / score） |
| `plan` | 判断結果から説明・計画を生成 |
| `change add/list` | 状況の変化を記録 |
| `decision add/list` | 人が下した判断を記録 |
| `task add/set/list` | タスク管理 |
| `report [--show]` | 日次サマリ Markdown を出力 |
| `github status/init-repo/pull/export/push` | 設定確認・git 初期化・Issue 取り込み・書き出し・push |
| `feedback KEY ACTUAL` | AI判断の答え合わせを記録 |
| `calibrate` | 蓄積した答え合わせで confidence を校正 |

各コマンドの詳細は `python app/cf.py <command> --help`。

---

## 何をするものか

単なる時間計測ツールではなく、**判断に必要な文脈を復元できること**を目的にする。

- 生ログ（ウィンドウタイトル等）はローカル SQLite だけに置き、外へ出さない
- LLM へ渡すのは、匿名化・集計済みの構造化データ（Current State）のみ
- 判断（Decision Engine）と説明（LLM Planner）を分離する
- PC操作が無い時間を「何もしていない」と判定しない（会議・思考・紙作業も活動として扱う）
- 対象日1日だけでなく、過去N日の傾向と今後N日の予定・締切も材料にし、
  今後の見通し・締切リスクといった**未来のことも判断・計画できる**

活動を次の4種類に分けて残すのが中核。

| 種類 | 問い | 正本 |
| --- | --- | --- |
| Activity | 何をしていた？ | ローカル SQLite |
| Change | 何が変わった？ | 案件の `decisions.md` |
| Decision | 何を決めた？ | 案件の `decisions.md` |
| Task | 次に何をする？ | 案件の `tasks.md`（**contextflow は読むだけ**） |

---

## 処理の流れ

```
Windows Logger ─┐
Outlook / .ics ─┤
Manual Input ───┼→ Activity Store (SQLite) → Context Builder → Current State
仕事管理リポジトリ┘   （タスク・制約を読む）                          │
                       ↑ build が作る            ↑ state が作る
                                                    ┌────────────────┴───────────────┐
                                                    ▼                                ▼
                                          Jev風 Decision Engine              LLM Planner
                                          小さな判断を型付きで              理由説明・計画作成
                                                  ↑ decide                  ↑ plan
                                                    └────────────────┬───────────────┘
                                                                     ▼
                                                        仕事管理リポジトリへ追記
                                                        （判断・変化を decisions.md へ）
                                                          ↑ github export
```

図の名前とコマンドの対応。**`build` と `Context Builder` は別物**なので注意する。

| 図の名前 | コマンド | 実装 | 作るもの |
| --- | --- | --- | --- |
| Activity Store への集約 | `build` | `pipeline/p30_activity_builder.py` | 確定 activity（DB） |
| **Context Builder** | **`state`** | `context/builder.py` | Current State（`state.json`） |

詳細は `docs/20260923_architecture.md`。

---

## 動作環境

| 項目 | 内容 |
| --- | --- |
| OS | Windows 11（収集は Win32 API を ctypes で利用） |
| Python | 3.12 以上 |
| 必須ライブラリ | なし（標準ライブラリのみ） |
| 任意ライブラリ | `anthropic`（Claude API）/ `pywin32`（Outlook 予定取得）/ `tzdata`（.ics の TZID 解決） |
| データ保存先 | `app/data/contextflow.db`（ローカル SQLite） |

---

## ディレクトリ

```
app/
├─ cf.py                入口スクリプト
├─ scripts/             タスクスケジューラ登録など、環境構築用の資材
├─ source/
│   ├─ contextflow/     実装（contracts / storage / collector / pipeline /
│   │                   sources / context / decision / planner / report / ui）
│   └─ config/          設定（config.toml / categories.toml / questions.toml）
└─ data/                DB・state.json・画面の設定・出力（Git 管理外）
docs/                   設計ドキュメント
tests/                  テスト（python -m unittest discover -s tests -t .）
references/
└─ sample_work_repo/    併せて使う「仕事管理リポジトリ」の構成サンプル
```

コマンドはプロジェクトルートから `python app/cf.py <command>` で実行する。

| 知りたいこと | 参照先 |
| --- | --- |
| 設定キーの一覧 | `docs/20260923_config_reference.md` |
| 全体設計 | `docs/20260923_architecture.md` |
| 仕事管理リポジトリとの連携 | `docs/20260923_work_repo_integration.md` |
| Outlook 予定取得の手順 | `docs/20260923_outlook_calendar.md` |
| ローカル GUI の設計・API | `docs/20260923_ui_design.md` |
| モジュールの公開API | `docs/20260923_module_contract.md` |
| 連携先リポジトリの構成サンプル | `references/sample_work_repo/HOWTO.md` |
