<#
.SYNOPSIS
    Outlook の予定を日次取得するタスクを Windows タスクスケジューラへ登録する。

.DESCRIPTION
    classic Outlook を COM 経由で読むため、必ず「ユーザーがログオンしているときのみ実行」で
    登録する。Office の COM 自動化は非対話セッション（SYSTEM など）での実行が
    Microsoft のサポート対象外のため。

    このスクリプトは登録だけを行い、タスクの実行はしない。
    登録内容を確認してから、タスクスケジューラで手動実行するか、翌日の定時実行を待つ。

.PARAMETER Time
    実行時刻（HH:mm）。既定は 08:30。

.PARAMETER TaskName
    タスク名。既定は contextflow-calendar-sync。

.PARAMETER PythonPath
    使用する python.exe のパス。既定は PATH 上の python。
    .venv を使う場合は .venv\Scripts\python.exe を指定する。

.PARAMETER Arguments
    cf.py へ渡す引数。既定は "calendar sync"。

.PARAMETER Unregister
    指定すると登録を解除する。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File app\scripts\register_calendar_task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File app\scripts\register_calendar_task.ps1 -Time 09:00 -PythonPath .\.venv\Scripts\python.exe

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File app\scripts\register_calendar_task.ps1 -Unregister
#>

param(
    [string]$Time = "08:30",
    [string]$TaskName = "contextflow-calendar-sync",
    [string]$PythonPath = "python",
    [string]$Arguments = "calendar sync",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

# このスクリプトは app\scripts\ にあるため、2階層上がプロジェクトルート
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$EntryScript = Join-Path $ProjectRoot "app\cf.py"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "登録を解除した: $TaskName"
    } else {
        Write-Host "そのタスクは登録されていない: $TaskName"
    }
    return
}

if (-not (Test-Path $EntryScript)) {
    throw "入口スクリプトが見つからない: $EntryScript"
}

# python の存在確認（PATH 上の python でも、絶対パス指定でも動くようにする）
$resolvedPython = $PythonPath
if ($PythonPath -eq "python") {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "python が PATH 上に見つからない。-PythonPath で絶対パスを指定する。" }
    $resolvedPython = $cmd.Source
} else {
    if (-not (Test-Path $PythonPath)) { throw "python が見つからない: $PythonPath" }
    $resolvedPython = (Resolve-Path $PythonPath).Path
}

$actionArgs = "`"$EntryScript`" $Arguments"

$action = New-ScheduledTaskAction -Execute $resolvedPython -Argument $actionArgs -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# COM 自動化のため、対話セッション（ログオン中）でのみ実行する
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "contextflow: classic Outlook から直近1週間の予定を日次取得する" `
    -Force | Out-Null

Write-Host "登録した: $TaskName"
Write-Host "  実行時刻      : $Time （毎日・ログオン中のみ）"
Write-Host "  実行コマンド  : $resolvedPython $actionArgs"
Write-Host "  作業ディレクトリ: $ProjectRoot"
Write-Host ""
Write-Host "動作確認（手動実行）:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "登録解除:"
Write-Host "  powershell -ExecutionPolicy Bypass -File app\scripts\register_calendar_task.ps1 -Unregister"
