# このサンプルの使い方

contextflow と併せて使う「仕事管理リポジトリ」の構成サンプル。
**そのまま contextflow から読み書きできる**ことを確認済み。

## 試す

1. このフォルダを、作業用の場所へコピーする（このサンプル自体は汚さない）

```powershell
Copy-Item -Recurse references\sample_work_repo C:\path\to\work-context
```

2. `app/source/config/config.toml` の2か所を書き換える

```toml
[paths]
context_repo = "C:/path/to/work-context"

[context_repo]
layout = "work_repo"
```

3. 読めるか確かめる

```powershell
python app\cf.py init          # 認識できた案件の一覧
python app\cf.py github pull   # tasks.md からタスクを取り込む
python app\cf.py task list     # 取り込み結果
```

4. 書き込みを試す

```powershell
python app\cf.py change   add "項目定義に不足が見つかった" --project sample_project
python app\cf.py decision add "項目を追加して再確認" --reason "手戻りを避けるため" --project sample_project
python app\cf.py github export
```

`10_projects/50c_202609_sample_project/decisions.md` の末尾へ、その日の節が追加される。

## このサンプルで確認できること

| 確認できること | 見る場所 |
| --- | --- |
| 案件フォルダ名の分解（`<prefix>_<yyyymm>_<suffix>`） | `10_projects/50c_202609_sample_project` → key は `sample_project` |
| `99_old/` が案件走査から除外される | `99_old/50c_202603_finished_project` が一覧に出ない |
| `tasks.md` の記法がタスクへ変換される | `@doing` → 進行中 / `!high` → 優先度1 / `## Waiting` → 待ち / `#12` → Issue 番号 |
| 案件が2つあるときの案件別集計 | `sample_project` と `another_project` |
| 判断・変化の追記先と書式 | `decisions.md`（日付の節に `- 変化:` / `- 判断:`） |
| 日次サマリを既定で書き出さないこと | `20_knowledge/90_daily_activity/README.md` |

## 動作確認の結果

このサンプルに対して contextflow を実際に向けて確認した。

```
案件      50c_202609_sample_project   key=sample_project
          51a_202608_another_project  key=another_project
          （99_old 配下は出ない）
タスク    12件（進行中1 / 未着手5 / 待ち1 / 完了3 / 中止1 ほか）
追記      変化・判断とも decisions.md へ。同じ内容は二重に入らない
          案件が特定できない場合は書かずに知らせる
```

## 注意

- `tasks.md` は **contextflow が書き換えない**（Issue 同期の正本のため）
- 実施日時はこのリポジトリで管理しない方針のため、日次の時間集計は既定で書き出さない
- 中身はすべて架空のサンプル。実在の案件名・人名は含まない
