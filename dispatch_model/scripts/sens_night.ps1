# Overnight sensitivity driver -- one night's slice of the 20-variant set.
#
# Started at 20:00 by the `PriceModeling-Sens-Nightly` scheduled task. Same shape as `mc_night.ps1`, and
# for the same reasons -- every line below that looks defensive is a bug that already cost a night:
#
#   * KILL THE PROCESS TREE, do not let the task's execution limit do it. The limit kills the wrapper and
#     the top-level python; the CHILDREN survive, orphaned to a dead pid. Night 2 of the Monte-Carlo then
#     started while night 1's orphans still held memory (commit 35.6 GB against 15.7 GB of RAM) and twelve
#     hours produced zero completed draws. `taskkill /T` a quarter-hour before the limit, and sweep at
#     startup for whatever survived anyway.
#   * THE DEADLINE MUST BE STRICTLY AHEAD OF NOW. `tomorrow 07:45, or today 07:45 if the hour is < 12`
#     also catches a MORNING catch-up start, and then the deadline is already past: measured, a
#     StartWhenAvailable catch-up at 09:26 started the run and killed it in the same second, exit code 0.
#   * A DAYTIME CATCH-UP MUST NOT RUN AT ALL. `StartWhenAvailable` is worth keeping (a machine that boots
#     at 21:00 should still get its night), but a catch-up at 09:26 would hold the CPU through the working
#     day, which is the one thing this schedule exists to prevent.
#
# Being stopped in the morning is the NORMAL exit path. The set is resumable at two levels: a variant whose
# `.sens_complete` marker exists is skipped whole, and inside a variant any year whose `prices_<y>.parquet`
# exists is skipped -- that file is written LAST and atomically, so its presence means the year's dispatch
# and flows landed too. A stop costs at most the projected year each worker had in flight.

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot          # ...\PriceModeling\dispatch_model
Set-Location $repo

$logDir = Join-Path $repo 'scratchpad\sens_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("night_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
function Say([string]$m) {
  $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
  Write-Host $line
  Add-Content -Path $log -Value $line -Encoding utf8
}

$deadline = (Get-Date).Date.AddHours(7).AddMinutes(45)
while ($deadline -le (Get-Date)) { $deadline = $deadline.AddDays(1) }

$h = (Get-Date).Hour
if ($h -ge 8 -and $h -lt 19) {
  Say "=== sens skipped: started at $((Get-Date).ToString('HH:mm')), inside working hours (08:00-19:00) ==="
  Say "    this is a StartWhenAvailable catch-up for a missed 20:00 slot; waiting for the next one"
  exit 0
}

Say "=== sens night start (deadline $($deadline.ToString('yyyy-MM-dd HH:mm'))) ==="

# --- clean slate -------------------------------------------------------------------------------------
$stale = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -match 'run_sensitivities|run_projection_20y|run_montecarlo|spawn_main' })
if ($stale.Count -gt 0) {
  Say "killing $($stale.Count) leftover python process(es) from a previous night"
  foreach ($s in $stale) { Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 5
}

$outDir = Join-Path $repo 'reports\sens'
function CompleteVariants {
  if (-not (Test-Path $outDir)) { return 0 }
  @(Get-ChildItem $outDir -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName '.sens_complete') }).Count
}
$before = CompleteVariants
Say "variants complete before tonight: $before / 20"
$os = Get-CimInstance Win32_OperatingSystem
Say ("free RAM {0} GB, commit free {1} GB" -f [math]::Round($os.FreePhysicalMemory/1MB,1),
                                              [math]::Round($os.FreeVirtualMemory/1MB,1))

$cube = Join-Path (Split-Path -Parent $repo) 'archive\REFERENCE_WEATHER_20Y.nc'
if (-not (Test-Path $cube)) {
  Say "ABORT: reference weather absent ($cube). Run scripts/build_reference_weather.py first."
  exit 1
}

# --- run, redirecting to the log so no console buffering can swallow progress -------------------------
$outLog = Join-Path $logDir ("sens_{0}.out" -f (Get-Date -Format 'yyyy-MM-dd'))
$p = Start-Process -FilePath 'python' `
        -ArgumentList '-u','-X','utf8','-W','ignore','scripts\run_sensitivities.py','--workers','3' `
        -WorkingDirectory $repo -PassThru -NoNewWindow `
        -RedirectStandardOutput $outLog -RedirectStandardError ($outLog + '.err')
Say "started run_sensitivities pid $($p.Id); stdout -> $(Split-Path -Leaf $outLog)"

while (-not $p.HasExited -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 60 }

if (-not $p.HasExited) {
  Say "deadline reached -- killing process TREE of pid $($p.Id) (taskkill /T) so no workers survive"
  & taskkill.exe /PID $p.Id /T /F 2>&1 | ForEach-Object { Say "  $_" }
  Start-Sleep -Seconds 10
} else {
  Say "run_sensitivities exited on its own with code $($p.ExitCode)"
}

# belt and braces: nothing of ours may outlive this script
$left = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
          Where-Object { $_.CommandLine -match 'run_sensitivities|run_projection_20y|spawn_main' })
if ($left.Count -gt 0) {
  Say "force-stopping $($left.Count) remaining python process(es)"
  foreach ($s in $left) { Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue }
}

$after = CompleteVariants
Say "variants complete after tonight : $after / 20  (+$($after - $before))"
Say "=== sens night end ==="
