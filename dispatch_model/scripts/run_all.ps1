# CONTINUOUS driver: the 50-draw Monte-Carlo to completion, then the 20-variant sensitivity set.
#
# Started once at 20:00 by `PriceModeling-RunAll`. Unlike `mc_night.ps1` and `sens_night.ps1` this does NOT
# stop in the morning and does NOT refuse to run in working hours -- it holds the machine until both stages
# are done, which is roughly three to four days. That is a deliberate instruction, not an oversight.
#
# WHAT IT KEEPS from the nightly wrappers, because each line is a bug that already cost a night:
#
#   * KILL THE PROCESS TREE, never a bare Stop-Process on the parent. Killing the top-level python leaves
#     the `multiprocessing` pool workers orphaned to a dead pid; measured, they survived fourteen hours and
#     the next run started against 35.6 GB of commit on a 15.7 GB machine, producing zero draws in twelve
#     hours. Anything this script starts, it also reaps.
#   * SWEEP AT STARTUP for survivors of whatever ran before, for the same reason.
#   * THE DAILY TRIGGER IS A RESTART, NOT A SECOND RUN. The task repeats daily with
#     MultipleInstances=IgnoreNew, so while this instance lives the 20:00 trigger is ignored; if the machine
#     reboots mid-ensemble, the next 20:00 picks up where it left off. Both stages are resumable, so a
#     restart costs at most the projected year each worker had in flight.
#
# RESUMABILITY, at three levels:
#   * a draw whose `.draw_complete` marker exists is skipped whole (marker written LAST, after the
#     deliverables, so it certifies everything it stands for)
#   * a variant whose `.sens_complete` marker carries its own fingerprint is skipped whole
#   * inside either, `run_projection_20y` skips any year whose `prices_<y>.parquet` exists -- written last
#     and atomically, so its presence means that year's dispatch and flows landed too
#
# ORDER MATTERS. The sensitivities run on REFERENCE_WEATHER_20Y, which was selected from the PREVIOUS
# ensemble's prices. Running the Monte-Carlo first means its result is available to re-select the
# most-typical year map before the sensitivities are read, even though this script does not re-select it.

param(
  # Which stages to run. 'both' is the original behaviour. 'mc' is what a rebuilt ensemble needs when the
  # sensitivity set has been deliberately deferred -- the two stages share the machine and 6 workers on
  # this box thrashes, so they are never concurrent.
  [ValidateSet('both','mc','sens')] [string] $Stages = 'both'
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot          # ...\PriceModeling\dispatch_model
Set-Location $repo

$logDir = Join-Path $repo 'scratchpad\runall_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'runall.log'
function Say([string]$m) {
  $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
  Write-Host $line
  Add-Content -Path $log -Value $line -Encoding utf8
}

$mine = 'run_montecarlo|run_sensitivities|run_projection_20y|build_projection_deliverables|spawn_main'

function SweepMine([string]$why) {
  $p = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -match $mine })
  if ($p.Count -gt 0) {
    Say "$why : killing $($p.Count) leftover python process(es)"
    foreach ($x in $p) { & taskkill.exe /PID $x.ProcessId /T /F 2>&1 | Out-Null }
    Start-Sleep -Seconds 5
  }
}

function CountMarkers([string]$dir, [string]$marker) {
  if (-not (Test-Path $dir)) { return 0 }
  @(Get-ChildItem $dir -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName $marker) }).Count
}

# SINGLE INSTANCE, enforced HERE rather than trusted to Task Scheduler. `MultipleInstances=IgnoreNew` did not
# hold: on 2026-09-11 at 22:00:12 the hourly trigger started a second driver while the first was alive and
# logging (last line 21:56:53), and the SweepMine below -- written to reap ORPHANS -- killed seven healthy
# solver processes mid-year. A sweep is only safe when no driver is alive, so a live holder of the lock makes
# this instance exit before touching anything. The holder is checked by pid AND command line, so a stale lock
# whose pid was reused by an unrelated process does not block a legitimate restart.
$lock = Join-Path $logDir 'runall.lock'
if (Test-Path $lock) {
  $holder = 0
  [void][int]::TryParse(((Get-Content $lock -TotalCount 1) -as [string]), [ref]$holder)
  if ($holder -gt 0 -and $holder -ne $PID) {
    $h = Get-CimInstance Win32_Process -Filter "ProcessId=$holder" -ErrorAction SilentlyContinue
    if ($h -and $h.CommandLine -match 'run_all\.ps1') {
      Say "driver pid $holder is alive and holds the lock - this instance (pid $PID) exits without touching anything"
      exit 0
    }
  }
}
Set-Content -Path $lock -Value $PID -Encoding ascii

Say "=== RUN-ALL start (continuous; stages: $Stages) ==="
Say "lock taken by pid $PID"
SweepMine "startup"

# TRUNCATED CUBES. `mc_weather.generate_cube` writes straight to its final path and `run_draw` reuses any
# cube that EXISTS, so a kill during weathergen leaves a partial file the restart would pick up. It cannot be
# fixed in mc_weather.py mid-ensemble: that file is part of the model fingerprint, and touching it would
# invalidate every completed draw. So it is guarded here, outside the hashed set. A full cube is 853,597,7xx
# BYTES and varies by a few bytes between draws; anything under 850,000,000 bytes is removed and regenerated
# from its seed. The threshold is written in bytes on purpose: PowerShell's `850MB` literal is 850 MiB =
# 891,289,600 bytes, which is LARGER than a full cube -- the first version of this guard used it and deleted
# every healthy cube on restart (harmless, since a cube regenerates bit-identical from its seed, but ~4 min each). This
# catches a kill during the write, not one in the last few kB -- that case fails loudly on open rather than
# pricing on wrong weather.
# Since 2026-09-14 a draw's cube lives one level down, in the fingerprinted work directory
# (scratchpad\mc\<ensemble>-<digest>\cube_<d>.nc), hence -Recurse; generate_cube also writes through a temp
# name now, so this guard is the second line of defence rather than the first.
foreach ($c in @(Get-ChildItem (Join-Path $repo 'scratchpad\mc') -Filter 'cube_*.nc' -Recurse -ErrorAction SilentlyContinue)) {
  if ($c.Length -lt 850000000) {
    Say ("removing truncated cube {0} ({1:N0} bytes) - it will be regenerated from its seed" -f $c.Name, $c.Length)
    Remove-Item -LiteralPath $c.FullName -Force -ErrorAction SilentlyContinue
  }
}

$cube = Join-Path (Split-Path -Parent $repo) 'archive\REFERENCE_WEATHER_20Y.nc'
if (-not (Test-Path $cube)) { Say "ABORT: reference weather absent ($cube)"; exit 1 }

$os = Get-CimInstance Win32_OperatingSystem
Say ("free RAM {0} GB, commit free {1} GB, disk free {2} GB" -f
     [math]::Round($os.FreePhysicalMemory/1MB,1), [math]::Round($os.FreeVirtualMemory/1MB,1),
     [math]::Round((Get-PSDrive C).Free/1GB,1))

# ---- STAGE 1: the 50-draw Monte-Carlo ----------------------------------------------------------------
$mcDir = Join-Path $repo 'reports\mc50'
# COUNTING MARKERS IS NOT COUNTING FINISHED DRAWS. A `.draw_complete` now carries the fingerprint of the
# model that wrote it, and `run_montecarlo` recomputes any draw whose marker names a different model. This
# progress count deliberately does NOT re-check that -- it is a progress line, and the authority is the
# python process. Reading it as a completion gate is exactly what reported 50/50 on a 9/41 mixture.
$before = CountMarkers $mcDir '.draw_complete'
Say "STAGE 1 - Monte-Carlo: $before / 50 draw marker(s) present (progress only; the run re-checks the model)"

$stage1Refused = $false
if ($Stages -ne 'sens') {
  # MIGRATE BEFORE ANY SOLVE. `run_montecarlo` keys its work directory on the SOLVE digest, so a change that
  # cannot touch a solved year (the deliverables builder, 2026-09-14) still renames that directory -- and a
  # restart would then re-solve the whole ensemble to reproduce parquets that were already correct. This step
  # runs here, with the lock held and orphans swept, i.e. with no solver alive: it renames the work directory
  # when every solve component is byte-identical to the recorded map, restamps the markers, and rebuilds stale
  # deliverables. Exit 2 means earlier work exists that the current solve model did NOT produce; starting
  # anyway would silently re-solve everything, so stage 1 does not start and a person decides.
  $mig = Join-Path $logDir 'migrate.out'
  $pm = Start-Process -FilePath 'python' `
         -ArgumentList '-u','-X','utf8','-W','ignore','scripts\migrate_ensemble.py','migrate','--out','reports\mc50' `
         -WorkingDirectory $repo -PassThru -NoNewWindow -Wait `
         -RedirectStandardOutput $mig -RedirectStandardError ($mig + '.err')
  foreach ($l in @(Get-Content $mig -ErrorAction SilentlyContinue)) { Say "  [migrate] $l" }
  foreach ($l in @(Get-Content ($mig + '.err') -ErrorAction SilentlyContinue | Select-Object -Last 5)) { Say "  [migrate:err] $l" }
  if ($pm.ExitCode -ne 0) {
    Say "ABORT STAGE 1: migrate_ensemble exited $($pm.ExitCode) - run_montecarlo NOT started (it would re-solve the ensemble). See migrate.out."
    $stage1Refused = $true
  } else {
    $out = Join-Path $logDir 'mc.out'
    $p = Start-Process -FilePath 'python' `
          -ArgumentList '-u','-X','utf8','-W','ignore','scripts\run_montecarlo.py',
                        '--draws','50','--workers','3','--out','reports\mc50' `
          -WorkingDirectory $repo -PassThru -NoNewWindow `
          -RedirectStandardOutput $out -RedirectStandardError ($out + '.err')
    Say "started run_montecarlo pid $($p.Id) -> $(Split-Path -Leaf $out)"
    while (-not $p.HasExited) {
      Start-Sleep -Seconds 300
      Say ("  ... Monte-Carlo {0} / 50 draws" -f (CountMarkers $mcDir '.draw_complete'))
    }
    Say "run_montecarlo exited with code $($p.ExitCode)"
  }
}
$after = CountMarkers $mcDir '.draw_complete'
Say "STAGE 1 done: $after / 50 draws complete"
SweepMine "between stages"

# A short-fall is NOT fatal: the sensitivities do not depend on the ensemble, and stopping here would
# waste the rest of the machine's time. It is reported loudly and stage 2 proceeds.
if ($after -lt 50) { Say "WARNING: Monte-Carlo finished short at $after / 50 - continuing to stage 2 anyway" }

# ---- STAGE 2: the 20-variant sensitivity set ---------------------------------------------------------
$sensDir = Join-Path $repo 'reports\sens'
$sbefore = CountMarkers $sensDir '.sens_complete'
Say "STAGE 2 - sensitivities: $sbefore / 20 variants already complete"

if ($stage1Refused) {
  # the sensitivities run on reference weather selected from the ensemble: when the ensemble could not be
  # migrated to the model on disk, running them anyway would price variants on another model's weather
  Say "STAGE 2 SKIPPED: stage 1 was refused, so the model on disk is not the one the reference weather came from"
} elseif ($Stages -ne 'mc') {
  $out2 = Join-Path $logDir 'sens.out'
  $p2 = Start-Process -FilePath 'python' `
        -ArgumentList '-u','-X','utf8','-W','ignore','scripts\run_sensitivities.py','--workers','3' `
        -WorkingDirectory $repo -PassThru -NoNewWindow `
        -RedirectStandardOutput $out2 -RedirectStandardError ($out2 + '.err')
  Say "started run_sensitivities pid $($p2.Id) -> $(Split-Path -Leaf $out2)"
  while (-not $p2.HasExited) {
    Start-Sleep -Seconds 300
    Say ("  ... sensitivities {0} / 20 variants" -f (CountMarkers $sensDir '.sens_complete'))
  }
  Say "run_sensitivities exited with code $($p2.ExitCode)"
}
Say "STAGE 2 done: $(CountMarkers $sensDir '.sens_complete') / 20 variants complete"

SweepMine "shutdown"
Say "=== RUN-ALL end ==="
if ((Test-Path $lock) -and ((Get-Content $lock -TotalCount 1) -eq "$PID")) { Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue }
