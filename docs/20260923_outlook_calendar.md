# Outlook 予定の日次取得

作成日: 2026-09-23
出典: 非公開の検討資料（新 Outlook と classic Outlook の併用方法）

---

## 1. 方針

**普段使いは新 Outlook のまま。classic Outlook は「Python 用の予定取得アダプタ」としてだけ残す。**

新 Outlook には COM / Outlook Object Model が無いため、Python からは読めない。
classic Outlook には引き続き COM があり、同じ Microsoft 365 メールボックスを見るので、
新 Outlook で登録・変更した予定も同期後に読める。

```
              Microsoft 365 / Exchange
                       │
        ┌──────────────┴──────────────┐
        ▼                             ▼
   新 Outlook                  Outlook (classic)
   普段の操作用                  Python 連携専用
                                      │ COM (pywin32)
                                      ▼
                        タスクスケジューラ（日次・ログオン中）
                                      ▼
                          python app/cf.py calendar sync
                                      ▼
                      Activity（layer=PLANNED / source=CALENDAR）
```

Python は新 Outlook に一切触れない。classic Outlook の画面も基本的に触らない。

---

## 2. 取得元の差し替え（将来の Graph API 移行を見据える）

取得元は `contracts/calendar.py` の `CalendarProvider` インターフェースだけを介する。

```python
class CalendarProvider(abc.ABC):
    name: str
    def check(self) -> ProviderStatus            # 利用可否と理由
    def fetch(self, start, end) -> list[CalendarEvent]
```

| 実装 | 状態 | 位置づけ |
| --- | --- | --- |
| `OutlookComProvider`（`sources/calendar_outlook.py`） | 実装済み | 既定。classic Outlook の COM |
| `IcsFileProvider`（`sources/calendar_ics.py`） | 実装済み | 退避先。`.ics` を置いて読む |
| `GraphProvider` | 未実装 | 会社で Graph API が使えるようになったら追加する |

Graph へ移る場合も、`CalendarProvider` を実装して `calendar_sync.create_provider` に
1行足すだけで済む。`Activity` への変換より後ろは一切変わらない。

COM 方式と Graph 方式の比較（参照資料 §7）。

| | classic Outlook COM | Microsoft Graph |
| --- | --- | --- |
| Entra アプリ登録 | 不要 | 通常必要 |
| 管理者承認 | 原則不要 | 環境次第 |
| Windows ログイン | 必要 | 不要 |
| 完全バックグラウンド | 不可 | 可 |
| PoC 向き | ◎ | ○ |

classic Outlook は Microsoft の案内で**少なくとも 2029 年までサポート予定**。
当面 COM で運用し、長期的には Graph へ移せる構造にしてある。

---

## 3. セットアップ手順

### 3.1 classic Outlook を使える状態にする

新 Outlook 右上のトグルだけで切り替えると、classic Outlook を起動しても
新 Outlook へリダイレクトされる状態になることがある。
Microsoft の案内どおり、一度 classic Outlook へ戻したうえで
**Outlook** と **Outlook (classic)** をそれぞれスタートメニューにピン留めし、
サイドバイサイドで使える状態にする。

classic Outlook 側にも、予定を取りたい**同じ会社アカウント**を設定しておく。
初回は画面が開くので、アカウント設定と初回同期だけ済ませておく。

### 3.2 pywin32 を導入する

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` の `pywin32` が該当。基本機能は標準ライブラリだけで動くため、
Outlook 連携を使わないなら導入不要。

### 3.3 取得できるか確認する

```powershell
python app\cf.py calendar status
```

`outlook : 利用可` と出れば準備完了。出ない場合は表示されたメッセージに従う。

```powershell
python app\cf.py calendar sync
```

初回は classic Outlook が起動する（起動済みならそのインスタンスに接続する）。

### 3.4 日次実行を登録する

```powershell
powershell -ExecutionPolicy Bypass -File app\scripts\register_calendar_task.ps1
```

既定で毎日 08:30 に `python app/cf.py calendar sync` を実行するタスクを登録する。

| オプション | 内容 |
| --- | --- |
| `-Time 09:00` | 実行時刻 |
| `-PythonPath .\.venv\Scripts\python.exe` | 仮想環境の python を使う |
| `-Arguments "calendar sync --days 14"` | 渡す引数を変える |
| `-Unregister` | 登録を解除する |

**このスクリプトは登録だけ行い、タスクは実行しない。** 動作確認は次で行う。

```powershell
Start-ScheduledTask -TaskName contextflow-calendar-sync
```

#### ログオン中のみ実行にする理由

Office の COM 自動化は、SYSTEM アカウントなどの非対話セッションからの実行を
Microsoft がサポートしていない。そのためタスクは
**「ユーザーがログオンしているときのみ実行」**で登録している
（`-LogonType Interactive`）。「ログオンしているかどうかにかかわらず実行」にはしない。

---

## 4. 取得の仕様

### 4.1 取得範囲

既定で **前日 00:00 〜 7日後 00:00**（`[calendar] fetch_days_back = 1` / `fetch_days = 7`）。

- 先 7 日 … 直近1週間の予定。計画・判断に使う
- 前 1 日 … 前日に入った変更・追加の取りこぼしを防ぐため取り直す

`calendar sync --days 14 --days-back 3` のように都度変更もできる。

### 4.2 COM 取得時の注意点（実装で対処済み）

| 論点 | 対処 |
| --- | --- |
| 定期予定が展開されない | `Items.Sort("[Start]")` → `IncludeRecurrences = True` の順で設定する（順序が逆だと効かない） |
| `Restrict()` の日時書式が地域設定に依存 | 英語圏書式 → 日本語環境書式の順で試し、両方失敗したら全件走査へ退避 |
| 英語圏書式の AM/PM が実行時ロケールで変わる | `%p` を使わず自前で組み立てる（日本語ロケールでは `%p` が「午後」を返し、Restrict が解釈できない） |
| プロパティが無い・例外を投げる項目がある | 属性ごとに防御的に読み、既定値へ落とす |
| COM の初期化 | `pythoncom.CoInitialize()` を試行（無くても続行） |

### 4.3 取り込まない予定

実績の材料にすると時間集計が壊れるものを除く（`[calendar]` の `skip_*` で制御）。

| 除外対象 | 理由 |
| --- | --- |
| 終日予定 | 祝日・「〇〇週間」などが 24 時間の会議として入ってしまう |
| 辞退した予定 | 参加していない |
| キャンセル済みの予定 | 実施されていない |
| 「空き時間」扱いの予定 | 予定表の飾り（リマインダー等） |

除外した件数は `calendar sync` の出力に理由別で表示される。

### 4.4 保存先

**ローカル SQLite（`paths.database`）の `activities` テーブル。** ファイルとしては残らない。

| 項目 | 値 |
| --- | --- |
| layer | `planned`（予定であって実績ではない） |
| source | `calendar`（どこから得た情報かを残す） |
| activity_type | 既定 `meeting`。画面で変更可 |
| summary | 件名（`mask_subject` 適用後） |
| detail | 場所・主催者・UID・busy/response 状態 |

`paths.calendar_dir`（既定 `app/data/calendar`）は `.ics` の**入力**置き場で、出力先ではない。
仕事管理リポジトリには書かない（「実施日時はリポジトリで管理しない」方針のため）。

### 4.5 再取得したときの挙動

取得範囲の予定を、**日ごとに丸ごと入れ替える**（差分更新ではない）。
その日の `planned` を削除してから入れ直すため、古い予定が残らない。

| Outlook 側の変更 | 挙動 |
| --- | --- |
| 時刻・件名の変更 | 新しい内容に置き換わる |
| 予定の削除 | 消える |
| 予定の追加 | 増える |
| 予定が0件になった日 | その日の `planned` が空になる |
| **取得範囲の外** | **触らない**。遡って直すなら `--days-back` を広げる |

**`calendar sync` だけでは画面・集計に反映されない。** 反映には `build` が要る。

```
9/23 に取得 → build
  planned  : 10:00-11:00 定例会議
  confirmed: 10:00-11:00 定例会議（conf 0.5）

（Outlook 側で 11:00 へ移動・改名、別の予定を削除）

9/24 に取得（build はまだ）
  planned  : 11:00-12:00 定例会議（時間変更）   ← 新しい
  confirmed: 10:00-11:00 定例会議               ← 古いまま

build 再実行後
  confirmed: 11:00-12:00 定例会議（時間変更）
```

日次実行（タスクスケジューラ）に登録されるのは `calendar sync` のみ。
その日の作業を見る前に `build` を実行する。

### 4.6 予定に種別を付ける

取り込み時の `activity_type` は既定で `meeting` 固定。実際は「情報収集」「思考」など内容が異なるため、
ローカル GUI の「予定の分類」から `uid` ごとに種別を付けられる（詳細は `docs/20260923_ui_design.md`）。

付けた種別は新テーブル `calendar_labels`（`uid` を主キー）へ保存する。`calendar sync` は予定を
日ごとに丸ごと入れ替えるため、予定の行に直接種別を持たせると再取得で消える。`uid` をキーにした
別テーブルへ保存し、`events_to_activities`（取得後・`Activity` 化の時点）で当てることで、
**再取得しても種別が消えない**。

分類は `build`（確定層へのマージ）と `gaps`（空白時間の種別推定）にも効く。

`uid` が無い予定（一部の `.ics` など）には種別を付けられない。次の取得と対応付けできないため。

### 4.7 予定は実績ではない

取り込んだ予定は `layer=PLANNED` に入るだけで、そのまま実績にはならない。

```
予定   13:00-14:00 定例会議（Outlook）
観測   13:40-14:00 PowerPoint（PCログ）
入力   13:00-13:40 会議（手入力）
  ↓ merge_layers（reported > observed > planned）
確定   13:00-13:40 meeting(manual) / 13:40-14:00 document(windows)
```

予定しか無い時間帯は confidence を 0.5 へ下げて採用する。
また `cf.py gaps` は、PC 操作の無い空白時間に予定が重なっているかを手掛かりに種別を推定する。

### 4.8 プライバシー

件名は日次サマリ（`github export` の出力先）へ出る。
`[calendar] mask_subject = true`（既定）のとき、`[privacy] mask_patterns` の正規表現を
**件名と場所にも適用**する。既定ではメールアドレスと URL をマスクする。

顧客名などを一切出したくない場合は、`mask_patterns` に規則を足すか、
`[privacy]` と同様の運用ルールを決める。

---

## 5. うまくいかないとき

| 症状 | 対処 |
| --- | --- |
| `pywin32 が未導入` | `pip install pywin32` |
| `Outlook.Application が未登録` | classic Outlook が入っていない。新 Outlook だけでは COM は使えない |
| classic Outlook を開くと新 Outlook に飛ぶ | トグルではなく、Microsoft 案内の手順でサイドバイサイド構成にする |
| 予定が 0 件 | classic Outlook 側にアカウントが設定され、同期が終わっているか確認する |
| 定期予定が出ない | `IncludeRecurrences` が効いていない。`calendar status` で取得元を確認し、ログの書式再試行を見る |
| 取得が極端に遅い | `Restrict()` が2書式とも失敗して全件走査へ退避している可能性がある。予定表が大きいと遅くなる。書式の問題であれば `format_restrict_datetime` の調整で解決する |
| タスクが動かない | ログオン中のみ実行のため、ログオフ中は動かない仕様。時刻を在席時間帯にする |
| 時刻がずれる | `.ics` 経由の場合は `pip install tzdata` で TZID が正しく解決される |
