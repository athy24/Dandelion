# CONTINUOUS driver: the 50-draw Monte-Carlo to completion, then the REFERENCE scenario and the 20-variant
# sensitivity set on it.
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
# ORDER MATTERS. The sensitivities run on the REFERENCE scenario: each year y taken from the ensemble draw d(y)
# that priced it at the ensemble's centre (archive/reference_20y/ref_weather_map.json, selected on draws 0-29 by
# scripts/select_reference_weather.py; see scripts/reference_scenario.py). Its weather cube has to regenerate 15
# draw cubes (4.5-6 GB of RAM each), so stage 2 starts only once the Monte-Carlo has released the machine. This
# script never re-selects the map: a map is a decision, taken by a person on a named set of draws. The one change it
# makes on its own is adjust_reference_map.py's, year by year, on the baseline gate's measurement and only among the
# draws the map was selected on (draws beyond them need --allow-other-draws, which this driver does not pass).
#
# STAGE 2 IS A CHAIN, every step idempotent and verified, stopped at the first failure:
#   build_reference_weather              regenerate the map's draw cubes, prove them the draws' own, stitch
#   build_reference_availability         stitch availability_mc/wdraw=901 from the draws' own partitions
#   run_reference_20y --measure-demand   FR demand of the reference scenario (the demand variants' base)
#   make_sensitivities                   the 20 variant workbooks + manifest (not rebuilt when current)
#   gate_sensitivities                   every workbook variant moves the input it names
#   run_sensitivities                    the BASELINE first, alone, then the baseline gate, then the variants
# A failed step is logged with the tail of its output and the chain stops; the next start of the driver
# retries it (each step skips what it has already verified).
#
# THE BASELINE GATE (user decision 2026-09-17). The sensitivities are launched only if the re-run baseline lands, in
# EVERY year, within 1 EUR/MWh of the ensemble's FR annual mean (scripts/reference_gate.py). run_sensitivities exits 4
# when it does not; this driver then runs adjust_reference_map.py (which replaces the draw of the failing years),
# rebuilds the reference through the chain above and re-runs the baseline -- at most 1 + 3 passes, the adjuster's own
# cap. Exit 5 (baseline incomplete or unchecked) and an adjuster refusal stop stage 2 without launching the set.
#
# A STOP IS A STOP. The task repeats hourly, so a stage 2 that failed would otherwise be retried every hour for the
# rest of the weekend, sweeping any manual run of these scripts each time. `reports/sens/.stage2_halted.json` records
# failures against the current year map: an adjuster refusal halts at once; a failed chain step, a baseline that
# could not be completed or checked (exit 5) or a set left incomplete (exit 3) halts at the third failure for the
# same map. While it names the current map, the driver exits at start without sweeping. Delete it (or change the
# map) to let the driver try again; a complete set removes it.

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

$mine = 'run_montecarlo|run_sensitivities|run_projection_20y|build_projection_deliverables|spawn_main|' +
        'run_reference_20y|build_reference_weather|build_reference_availability|make_sensitivities|' +
        'gate_sensitivities|check_reference_weather'

function SweepMine([string]$why) {
  $p = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -match $mine })
  if ($p.Count -gt 0) {
    Say "$why : killing $($p.Count) leftover python process(es)"
    foreach ($x in $p) { & taskkill.exe /PID $x.ProcessId /T /F 2>&1 | Out-Null }
    Start-Sleep -Seconds 5
  }
}

# One stage-2 step, to completion: python <args> in the repo, output to <name>.out, its tail into the log.
function RunStep([string]$name, [string[]]$pyArgs) {
  $o = Join-Path $logDir "$name.out"
  $ps = Start-Process -FilePath 'python' -ArgumentList (@('-u','-X','utf8','-W','ignore') + $pyArgs) `
          -WorkingDirectory $repo -PassThru -NoNewWindow -Wait `
          -RedirectStandardOutput $o -RedirectStandardError ($o + '.err')
  foreach ($l in @(Get-Content $o -Tail 25 -ErrorAction SilentlyContinue)) { Say "  [$name] $l" }
  foreach ($l in @(Get-Content ($o + '.err') -Tail 5 -ErrorAction SilentlyContinue)) { Say "  [${name}:err] $l" }
  Say "$name exited with code $($ps.ExitCode)"
  return $ps.ExitCode
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
# this instance exit before touching anything. The lock holds the driver's pid AND its process start time: a live
# process with both is the holder whatever its command line (a driver started inside an existing PowerShell host
# does not name the script on its own), and a pid reused by an unrelated process has another start time. A lock of
# the older pid-only format falls back to the command-line test.
$lock = Join-Path $logDir 'runall.lock'
$myStart = (Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks
function LockIsMine { (Test-Path $lock) -and ((Get-Content $lock -TotalCount 1) -eq "$PID|$myStart") }
if (Test-Path $lock) {
  $raw = ((Get-Content $lock -TotalCount 1) -as [string])
  $parts = @($raw -split '\|')
  $holder = 0
  [void][int]::TryParse($parts[0], [ref]$holder)
  if ($holder -gt 0 -and $holder -ne $PID) {
    $alive = $false
    $hp = Get-Process -Id $holder -ErrorAction SilentlyContinue
    if ($parts.Count -ge 2) {
      $alive = [bool]($hp -and $hp.StartTime.ToUniversalTime().Ticks -eq [int64]$parts[1])
    } else {
      $h = Get-CimInstance Win32_Process -Filter "ProcessId=$holder" -ErrorAction SilentlyContinue
      $alive = [bool]($h -and $h.CommandLine -match 'run_all\.ps1')
    }
    if ($alive) {
      Say "driver pid $holder is alive and holds the lock - this instance (pid $PID) exits without touching anything"
      exit 0
    }
  }
}
Set-Content -Path $lock -Value "$PID|$myStart" -Encoding ascii

Say "=== RUN-ALL start (continuous; stages: $Stages) ==="
Say "lock taken by pid $PID"

# A FINISHED SET IS NOT RE-RUN. The task repeats hourly for as long as it stays enabled; once run_sensitivities has
# written `.set_complete` (every variant complete under its own fingerprint) each start would otherwise re-verify the
# whole chain and, worse, SweepMine would kill any manual run of these scripts. Delete the file to run the set again.
$setDone = Join-Path $repo 'reports\sens\.set_complete'
if ($Stages -ne 'mc' -and (Test-Path $setDone)) {
  Say "the sensitivity set is complete ($setDone) - nothing to do; exiting without sweeping (delete it to re-run)"
  if (LockIsMine) { Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue }
  exit 0
}

$haltFile = Join-Path $repo 'reports\sens\.stage2_halted.json'
$metaFile = Join-Path (Split-Path -Parent $repo) 'archive\reference_20y\ref_weather_map.meta.json'
function MetaSha { if (Test-Path $metaFile) { (Get-FileHash $metaFile -Algorithm SHA256).Hash.ToLower() } else { 'ABSENT' } }
function HaltState {
  if (-not (Test-Path $haltFile)) { return $null }
  try { return (Get-Content $haltFile -Raw | ConvertFrom-Json) } catch { return $null }
}
function RecordFailure([string]$reason, [bool]$terminal) {
  $st = HaltState
  $sha = MetaSha
  $n = 1
  if ($st -and $st.map_meta_sha256 -eq $sha) { $n = [int]$st.failures + 1 }
  $halted = $terminal -or ($n -ge 3)
  $rec = [ordered]@{ halted = $halted; failures = $n; reason = $reason; map_meta_sha256 = $sha
                     utc = (Get-Date).ToUniversalTime().ToString('s') }
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $haltFile) | Out-Null
  ($rec | ConvertTo-Json) | Set-Content -Path $haltFile -Encoding utf8
  if ($halted) {
    Say "STAGE 2 HALTED: $reason - the driver will not retry until $haltFile is deleted or the year map changes"
  } else {
    Say "stage 2 failure $n of 3 for this year map ($reason) - retried at the next start"
  }
}
$st0 = HaltState
if ($Stages -ne 'mc' -and $st0 -and $st0.halted -and $st0.map_meta_sha256 -eq (MetaSha)) {
  Say "stage 2 is halted ($($st0.reason), $($st0.utc) UTC) - nothing to do; exiting without sweeping (delete $haltFile to retry)"
  if (LockIsMine) { Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue }
  exit 0
}
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
$chainOk = $false
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
    $null = $p.Handle                  # PS 5.1: without a handle taken at start, ExitCode reads back empty
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

# A short-fall is NOT fatal: the reference scenario needs only the draws its year map names (each step verifies
# them), and stopping here would waste the rest of the machine's time. It is reported loudly and stage 2 proceeds.
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
  $chain = @(
    @('refweather', @('scripts\build_reference_weather.py')),
    @('refavail',   @('scripts\build_reference_availability.py')),
    @('refdemand',  @('scripts\run_reference_20y.py', '--measure-demand')),
    @('makesens',   @('scripts\make_sensitivities.py')),
    @('gatesens',   @('scripts\gate_sensitivities.py'))
  )
  $maxPasses = 4                       # the first baseline + the adjuster's 3 rounds
  for ($pass = 1; $pass -le $maxPasses; $pass++) {
    Say "STAGE 2 - pass $pass of at most $maxPasses"
    $chainOk = $true
    foreach ($step in $chain) {
      Say "STAGE 2 - $($step[0]): $($step[1] -join ' ')"
      $rc = RunStep $step[0] $step[1]
      if ($rc -ne 0) {
        Say "ABORT STAGE 2: $($step[0]) exited $rc - the sensitivity set is NOT started. See $($step[0]).out"
        RecordFailure "$($step[0]) exited $rc" $false
        $chainOk = $false
        break
      }
      SweepMine "after $($step[0])"
    }
    if (-not $chainOk) { break }

    $out2 = Join-Path $logDir 'sens.out'
    $p2 = Start-Process -FilePath 'python' `
          -ArgumentList '-u','-X','utf8','-W','ignore','scripts\run_sensitivities.py','--workers','3' `
          -WorkingDirectory $repo -PassThru -NoNewWindow `
          -RedirectStandardOutput $out2 -RedirectStandardError ($out2 + '.err')
    $null = $p2.Handle                 # PS 5.1: without a handle taken at start, ExitCode reads back empty
    Say "started run_sensitivities pid $($p2.Id) -> $(Split-Path -Leaf $out2)"
    while (-not $p2.HasExited) {
      Start-Sleep -Seconds 300
      Say ("  ... sensitivities {0} / 20 variants" -f (CountMarkers $sensDir '.sens_complete'))
    }
    # the next start overwrites sens.out: what the runner said about the baseline, its gate, refusals and PART
    # variants is kept here
    foreach ($l in @(Get-Content $out2 -ErrorAction SilentlyContinue | Where-Object { $_ -match '^\[sens\]' -and $_ -notmatch 'FR frames for' })) { Say "  $l" }
    foreach ($l in @(Get-Content ($out2 + '.err') -Tail 10 -ErrorAction SilentlyContinue)) { Say "  [sens:err] $l" }
    $rcS = $p2.ExitCode
    Say "run_sensitivities exited with code $rcS (0 = whole set complete, 3 = some variants incomplete, 4 = baseline gate failed, 5 = baseline incomplete or unchecked)"
    SweepMine "after run_sensitivities"

    if ($rcS -eq 4) {
      $rcA = RunStep 'adjustref' @('scripts\adjust_reference_map.py')
      if ($rcA -eq 0) {
        Say "BASELINE GATE FAILED - the year map was adjusted; rebuilding the reference and re-running the baseline"
        continue
      }
      Say "ABORT STAGE 2: the baseline gate failed and the map was not adjusted (adjust_reference_map exit $rcA) - the sensitivities are NOT launched"
      RecordFailure "baseline gate failed and the year map could not be adjusted (adjust_reference_map exit $rcA)" $true
      break
    }
    if ($rcS -eq 0) {
      if (Test-Path $haltFile) { Remove-Item -LiteralPath $haltFile -Force -ErrorAction SilentlyContinue }
    } elseif ($rcS -eq 5) {
      Say "ABORT STAGE 2: the baseline could not be completed or checked - the sensitivities are NOT launched"
      RecordFailure "baseline could not be completed or checked (run_sensitivities exit 5)" $false
    } else {
      RecordFailure "run_sensitivities exited $rcS" $false
    }
    break
  }
}
Say "STAGE 2 done: $(CountMarkers $sensDir '.sens_complete') / 20 variants complete"

SweepMine "shutdown"
Say "=== RUN-ALL end ==="
if (LockIsMine) { Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue }
