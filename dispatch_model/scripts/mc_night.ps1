# Overnight Monte-Carlo driver -- one night's slice of the 50-draw ensemble.
#
# Started at 20:00 by the `PriceModeling-MC50-Nightly` scheduled task. Being stopped in the morning is the
# NORMAL exit path: the ensemble is resumable at two levels, so a stop costs at most the projected year
# each worker had in flight (~20 min).
#
#   * a draw whose `.draw_complete` marker exists is skipped whole
#   * inside a draw, `run_projection_20y.py` skips any year whose `prices_<y>.parquet` exists, and that
#     file is written LAST and atomically, so its presence means the year's dispatch and flows landed too
#   * the draw's weather cube is reused if still on disk, so a resumed draw does not re-simulate weather
#
# THE WRAPPER STOPS THE RUN ITSELF, and that is the whole point of this rewrite. Letting the scheduled
# task's execution limit do it killed only the wrapper and the top-level python -- the `multiprocessing`
# POOL WORKERS survived as orphans, parented to a dead pid, and were still alive 14 hours later. Night 2
# then started while night 1's orphans still held memory: commit reached 35.6 GB against 15.7 GB of RAM,
# the machine thrashed, and twelve hours produced ZERO completed draws and ~100 CPU-seconds per worker.
# A whole night lost, silently, with the task reporting its usual "terminated by time limit".
#
# So: kill any survivors at startup (clean slate, whatever happened last night), and stop this night's run
# by killing the whole PROCESS TREE a little before the task limit would fire. The task's 12-hour limit
# stays as a backstop only.

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot          # ...\PriceModeling\dispatch_model
Set-Location $repo

$logDir = Join-Path $repo 'scratchpad\mc50_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("night_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
function Say([string]$m) {
  $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
  Write-Host $line
  Add-Content -Path $log -Value $line -Encoding utf8
}

# NEXT 07:45 STRICTLY AHEAD OF NOW. The old form was `tomorrow 07:45, or TODAY 07:45 if the hour is < 12`.
# The second branch is there for a start after midnight, which should stop the same morning -- but it also
# catches a MORNING start, and then the deadline is already in the past. Measured: `StartWhenAvailable`
# fired a catch-up at 09:26 after the machine missed its 20:00 slot, the deadline resolved to 07:45 THAT
# MORNING, and the wrapper started the Monte-Carlo and killed it in the same second. +0 draws, exit code 0,
# nothing in the task history to say the night was wasted.
$deadline = (Get-Date).Date.AddHours(7).AddMinutes(45)
while ($deadline -le (Get-Date)) { $deadline = $deadline.AddDays(1) }

# AND A DAYTIME CATCH-UP MUST NOT RUN AT ALL. `StartWhenAvailable` is worth keeping -- a machine that boots
# at 21:00 should still get its night -- but a catch-up at 09:26 would hold the CPU through the working day,
# which is the one thing this schedule exists to prevent. Skip, and say so, rather than starting a run that
# either steals the day or is killed on the spot.
$h = (Get-Date).Hour
if ($h -ge 8 -and $h -lt 19) {
  Say "=== mc50 skipped: started at $((Get-Date).ToString('HH:mm')), inside working hours (08:00-19:00) ==="
  Say "    this is a StartWhenAvailable catch-up for a missed 20:00 slot; waiting for the next one"
  exit 0
}

Say "=== mc50 night start (deadline $($deadline.ToString('yyyy-MM-dd HH:mm'))) ==="

# --- clean slate -------------------------------------------------------------------------------------
$stale = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -match 'spawn_main|run_montecarlo|run_projection_20y' })
if ($stale.Count -gt 0) {
  Say "killing $($stale.Count) leftover python process(es) from a previous night"
  foreach ($s in $stale) { Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 5
}

$outDir = Join-Path $repo 'reports\mc50'
function CompleteDraws {
  if (-not (Test-Path $outDir)) { return 0 }
  @(Get-ChildItem $outDir -Directory -Filter 'draw_*' -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName '.draw_complete') }).Count
}
$before = CompleteDraws
Say "draws complete before tonight: $before / 50"
$os = Get-CimInstance Win32_OperatingSystem
Say ("free RAM {0} GB, commit free {1} GB" -f [math]::Round($os.FreePhysicalMemory/1MB,1),
                                              [math]::Round($os.FreeVirtualMemory/1MB,1))

# --- run, redirecting to the log so no console buffering can swallow progress -------------------------
$mcLog = Join-Path $logDir ("mc_{0}.out" -f (Get-Date -Format 'yyyy-MM-dd'))
$p = Start-Process -FilePath 'python' `
        -ArgumentList '-u','-X','utf8','-W','ignore','scripts\run_montecarlo.py',
                      '--draws','50','--workers','3','--out','reports\mc50' `
        -WorkingDirectory $repo -PassThru -NoNewWindow `
        -RedirectStandardOutput $mcLog -RedirectStandardError ($mcLog + '.err')
Say "started run_montecarlo pid $($p.Id); stdout -> $(Split-Path -Leaf $mcLog)"

while (-not $p.HasExited -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 60 }

if (-not $p.HasExited) {
  Say "deadline reached -- killing process TREE of pid $($p.Id) (taskkill /T) so no pool workers survive"
  & taskkill.exe /PID $p.Id /T /F 2>&1 | ForEach-Object { Say "  $_" }
  Start-Sleep -Seconds 10
} else {
  Say "run_montecarlo exited on its own with code $($p.ExitCode)"
}

# belt and braces: nothing of ours may outlive this script
$left = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
          Where-Object { $_.CommandLine -match 'spawn_main|run_montecarlo|run_projection_20y' })
if ($left.Count -gt 0) {
  Say "force-stopping $($left.Count) remaining python process(es)"
  foreach ($s in $left) { Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue }
}

$after = CompleteDraws
Say "draws complete after tonight : $after / 50  (+$($after - $before))"
Say "=== mc50 night end ==="
