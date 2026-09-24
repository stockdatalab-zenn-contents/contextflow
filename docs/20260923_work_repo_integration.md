# 仕事管理リポジトリとの連携

作成日: 2026-09-23
出典: 連携先である「仕事管理リポジトリ」の README と運用ガイド（非公開）

---

## 1. 前提

連携先は既に運用中の「仕事管理リポジトリ」。contextflow 側がその構成・記法・運用ルールに合わせる。
contextflow が独自のフォルダを作ることはしない。

```toml
[paths]
context_repo = "C:/path/to/work-context"   # リポジトリの場所（ローカルクローン）

[context_repo]
layout = "work_repo"    # その中の構成。work_repo | standalone
```

`[paths]` が「どこにあるか」、`[context_repo]` が「その中がどういう構成か」を決める。

```
C:/path/to/work-context / 10_projects / <prefix>_<yyyymm>_<suffix> / tasks.md
└─ paths.context_repo ─┘ └─ context_repo.projects_dir ─┘          └ context_repo.tasks_file
```

`standalone` は仕事管理リポジトリが無いときの単独動作用（contextflow 専用の構成を自分で作る）。

---

## 2. 守っているルール

連携先の「絶対ルール」に対応する contextflow 側の制約。

| 連携先のルール | contextflow の対応 |
| --- | --- |
| タスクの正本は `tasks.md` | **読み取りのみ。書き換えない** |
| Issue は `tasks.md` の写し・一方向同期 | **Issue の起票・更新・クローズを行わない**（読み取りのみ） |
| Issue 操作は plan 承認後 | 承認フローに一切介入しない |
| 人が作成した変更を推測で削除・上書きしない | 追記のみ。既存行は消さない |
| `99_old/` は参照しない | 案件走査から除外 |
| 具体的な実施日時はリポジトリで管理しない | **日次の時間集計は既定で書き出さない** |

書き込む先は**案件の `decisions.md` だけ**（判断・変化の追記）。それ以外は読むだけ。

---

## 3. 読み取り

### 3.1 案件フォルダ

`10_projects/<prefix>_<yyyymm>_<suffix>/` を走査する。`99_old` と `.` 始まりは除外。

| 要素 | 例 | 用途 |
| --- | --- | --- |
| prefix | `50c` | CLI での案件指定に使われる値 |
| period | `202609` | 期間 |
| suffix | `sample_project` | `pjt:` ラベルと同じ値 |

contextflow の `project` として使うキーは `[context_repo] project_key` で選ぶ。
既定は `suffix`（ラベルと一致するため）。

**案件名は、PCログ由来の活動とも揃える必要がある。**
`app/source/config/categories.toml` の `project_rules` は、`contains` でタイトルのキーワードに
一致させ、`project` で案件名を出力する。この `project` を suffix に揃えておく。

```toml
[[project_rules]]
contains = ["サンプル案件", "sample-project"]   # タイトルに入る表記（日本語でよい）
project  = "sample_project"               # 案件フォルダの suffix
```

揃っていないと `by_project` が `{'サンプル案件': 60, 'sample_project': 45}` のように割れ、
`decisions.md` への追記も案件不一致で失敗する。

### 3.2 tasks.md

セクションとタグから `Task` を組み立てる。**記法は連携先の定義に従う。**

| セクション | TaskStatus | blocked |
| --- | --- | --- |
| `## Next` | `OPEN` | False |
| `## Later` | `OPEN` | False |
| `## Waiting` | `BLOCKED` | True |
| `## Done` | `DONE` | False |
| `## Cancelled` | `CANCELED` | False |

| タグ | 反映先 |
| --- | --- |
| `@doing` | status を `IN_PROGRESS` |
| `@today` | 期限の目安＝当日 |
| `@this-month` | 期限の目安＝当月末 |
| `!high` / `!low` | priority 1 / 5（無指定は 3） |
| `!skip` | **Task として取り込まない** |
| `#12` | `github_issue = 12` |

`## Next` の既定の期限目安は今週末。`## Later` と `## Waiting` は期限なし。

> **注意**: contextflow の `deadline` は、`when:*` 相当の情報を期限の目安へ写した**近似値**。
> 実際の締切ではない。優先順位づけの材料として使う。

### 3.3 Issue（任意）

`[github] repo` と token が設定されているときだけ、Open Issue を読む。ラベルの対応は次のとおり。

| ラベル | 反映先 |
| --- | --- |
| `status:todo` / `doing` / `waiting` / `done` / `cancelled` | TaskStatus（`waiting` は blocked 扱い） |
| `priority:high` / `normal` / `low` | priority 1 / 3 / 5 |
| `when:today` / `this-week` / `this-month` | 期限の目安 |
| `pjt:<suffix>` | project |

`repo` が未設定なら `tasks.md` からの取り込みへ退避する。

### 3.4 制約

`[context_repo] constraints_file` にパスを設定すると、その箇条書きを `Current State` の
`constraints` として読む。既定は空（読まない）。

---

## 4. 書き込み

### 4.1 判断・変化 → 案件の decisions.md

```
python app/cf.py change add "先方のAPI利用が不可と判明" --project sample_project
python app/cf.py decision add "CSV連携方式に変更" --reason "API利用不可のため" --project sample_project
python app/cf.py github export
```

`10_projects/50c_202609_sample_project/decisions.md` に次の形で追記される。

```markdown
## 2026-09-23
- 変化: 先方のAPI利用が不可と判明
- 判断: CSV連携方式に変更（理由: API利用不可のため）
```

- 同じ行が既にあれば追記しない（何度実行しても重複しない）
- 案件が特定できない場合は**書かずに知らせる**（`--project` の指定漏れが分かる）
- 既存の内容は消さない

### 4.2 日次の時間集計（既定で無効）

連携先は「具体的な実施日時は Outlook のカレンダーで管理し、本リポジトリでは管理しない」方針。
そのため日次サマリは既定でリポジトリへ書き出さず、ローカル（`app/data/export`）に留める。

必要なら有効にできる。

```toml
[context_repo]
write_daily = true
daily_dir   = "20_knowledge/90_daily_activity"
```

有効時、同じ日に再実行すると最新の内容で置き換える（contextflow 自身の生成物のため）。

### 4.3 commit / push

```powershell
python app/cf.py github export --push
```

- **contextflow が書いたファイルだけを commit する**（`git add -A` は使わない）。
  普段使いのリポジトリで、人が編集中の無関係なファイルを巻き込まないため
- `[github] auto_push = true` で `export` 後に自動実行。既定は false
- push 先は `[github] remote`。未設定なら commit までで止まる

> 連携先は「ブランチに検討過程、`main` に検討結果」という運用。
> 自動 push を有効にする場合は、push 先ブランチの扱いを決めてから設定する。

---

## 5. 対応していないこと

| 項目 | 理由 |
| --- | --- |
| `tasks.md` への書き込み | 正本であり、Issue 同期の承認境界を壊すため |
| Issue の起票・更新・クローズ | 同上。承認制の同期処理が担う |
| `README.md` / `ROADMAP.md` の更新 | 人が管理する現在地のため |
| `20_knowledge/` への知識追加 | 既存カテゴリと目的に従う必要があり、自動化の対象外 |
| `.sync/` 配下の操作 | 同期処理の管理領域のため |

---

## 6. 確認コマンド

```powershell
python app/cf.py init            # 認識できた案件の一覧を表示
python app/cf.py github pull     # tasks.md（または Issue）からタスクを取り込む
python app/cf.py task list       # 取り込み結果
python app/cf.py github export   # 判断・変化を decisions.md へ追記
python app/cf.py github status   # git と Issue 連携の設定状況
```
