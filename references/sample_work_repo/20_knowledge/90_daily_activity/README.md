# 90_daily_activity

contextflow の日次サマリの出力先。

**既定では書き出さない。** 連携先の方針が「具体的な実施日時はここでは管理しない」ため、
日次の時間集計はローカル（`app/data/export`）に留める。

出力したい場合は `config.toml` で有効にする。

```toml
[context_repo]
write_daily = true
daily_dir   = "20_knowledge/90_daily_activity"
```
