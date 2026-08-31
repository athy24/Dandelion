# Overnight Monte-Carlo driver — one night's slice of the 50-draw ensemble.
#
# Registered as a Windows scheduled task that starts at 20:00 and is killed at 08:00 by the task's own
# 12-hour execution limit. Being killed is the NORMAL exit path, not a failure: the ensemble is resumable
# at two levels, so a hard stop costs at most the projected year each worker had in flight (~20 min).
#
#   * a draw whose `.draw_complete` marker exists is skipped whole
#   * inside a draw, `run_projection_20y.py` skips any year whose `prices_<y>.parquet` exists, and that
#     file is written LAST and atomically, so its presence means the year's dispatch and flows landed too
#   * the draw's weather cube is reused if still on disk, so a resumed draw does not re-simulate weather
#
# Nothing here needs to know how far the ensemble got: it just runs the same command every night and the
# runner works out what remains. When all 50 draws are done a night exits in seconds.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot          # ...\PriceModeling\dispatch_model
Set-Location $repo

$logDir = Join-Path $repo 'scratchpad\mc50_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("night_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))

"=== mc50 night start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Tee-Object -FilePath $log -Append

# Progress before this night, so the log says what was inherited rather than only what it added.
$outDir = Join-Path $repo 'reports\mc50'
$before = 0
if (Test-Path $outDir) {
  $before = @(Get-ChildItem $outDir -Directory -Filter 'draw_*' -ErrorAction SilentlyContinue |
              Where-Object { Test-Path (Join-Path $_.FullName '.draw_complete') }).Count
}
"draws complete before tonight: $before / 50" | Tee-Object -FilePath $log -Append

& python -u -X utf8 -W ignore scripts\run_montecarlo.py `
    --draws 50 --workers 3 --out reports\mc50 2>&1 | Tee-Object -FilePath $log -Append

$after = 0
if (Test-Path $outDir) {
  $after = @(Get-ChildItem $outDir -Directory -Filter 'draw_*' -ErrorAction SilentlyContinue |
             Where-Object { Test-Path (Join-Path $_.FullName '.draw_complete') }).Count
}
"draws complete after tonight : $after / 50  (+$($after - $before))" | Tee-Object -FilePath $log -Append
"=== mc50 night end $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Tee-Object -FilePath $log -Append
