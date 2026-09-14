# Phase 0 — Pre-integration codebase review (read-only)

Scope: weathergen (ii), demand_model (iii), res_model (iv), availability_model (v), dispatch_model (vi,
in progress), pricemodeling (shared ETL/DB). Read-only inventory + findings. **No code changed.**
Golden-test harness (§3) must be built before any refactor PR.

## 1. Repository map

| package | modules | LOC | tests | role | entry point |
|---|---|---|---|---|---|
| pricemodeling | 20 | 2018 | 0 | ETL: RTE/SYNOP/ENTSO-E/ERA5 → SQLite `master_hourly` + `entsoe_*` | `python -m pricemodeling` |
| weathergen | 26 | 2974 | 9 | stochastic multi-site weather cube (`simulation.nc`) | `weathergen` CLI |
| demand_model | 28 | 2090 | 7 | FR hourly demand | `demand-model` CLI |
| res_model | 36 | 2705 | 8 | FR PV/wind/ROR potential | `res-model` CLI |
| availability_model | 31 | 2006 | 8 | FR dispatchable availability | `avail-model` CLI |
| dispatch_model | 26 | 1345 | 8 | 7-zone dispatch → prices | `dispatch-model` CLI |
| **total** | **167** | **~13.1k** | **40** | | |

**Dependency graph.** The four models are independent packages coupled only through the SQLite DB and the
weathergen `simulation.nc` cube (no direct cross-package imports — good). BUT they re-implement the same
cross-cutting code (below), and dispatch_model re-implements availability_model's fleet/capacity query
(`io/fr_fleet.py` ≈ `availability_model/io/fleet.py`). No circular deps. **pricemodeling has 0 tests.**

## 2. Duplication census (→ becomes `core/`)

| duplicated concern | copies | evidence |
|---|---|---|
| config loading | **5** `config.py` | one per model, each re-doing pydantic/yaml + `resolve()` |
| metadata/hash stamping | **≥3** `meta.py` (res, availability, dispatch) | identical `_git_hash`/`_file_hash`/`run_metadata` |
| weather-cube loader | **3** (demand `projection/weather.py`, res `io/loaders.py`, avail `io/weather.py`) | each re-implements `_ENSEMBLE_DIMS`, `open_dataset`, national-mean reduction |
| p99.9 capacity scan | **2** (availability, dispatch) | same window-function SQL |
| disk cache (mtime-keyed) | **2** (`availability_model/io/cache.py`, `dispatch_model/io/cache.py`) | near-identical |
| RNG seeding | scattered | ad-hoc `default_rng(seed + draw*1000 + salt)`; `np.random.seed` in weathergen |

## 3. Interface / naming audit — findings

- **F1 (result-neutral, pervasive): three names for the UTC timestamp.** `ts_utc` (DB + pricemodeling,
  33×), `timestamp_utc` (demand/res/avail, 83×), `time` (weathergen cube dim, 35×). §4 glossary says
  `timestamp_utc` everywhere. Boundary shims currently rename ad-hoc.
- **F2: long-schema `value` columns.** DB tables use generic `series_key/sub_key/label/value` (no units in
  name); newer model code uses `_mw`/`_mwh` suffixes. Glossary rule violated at the DB boundary.
- **F3: verb semantics inconsistent.** `load_*`/`build_*` mostly consistent in newer code; weathergen uses
  `simulate/fit`; res uses `apply_*`. Needs the §4 verb table enforced.

## 4. Randomness audit

- Pattern is `np.random.default_rng(config.seed + draw*K + salt)` (availability/res/dispatch) — deterministic
  and non-overlapping in practice, but **not** the spec's `SeedSequence.spawn` scheme. **Risk (F4,
  result-changing if parallelised):** if draws are ever run across processes with the current additive
  scheme, no collision today, but there is no single RNG authority and no draw_id→child-seed contract.
- **F4 CORRECTION (verified 2026-07):** weathergen's *package* code is already clean — it uses
  `np.random.default_rng` (Generator) with an explicit `rng` threaded through `simulate()` ("same seed +
  config ⇒ identical output"). The global `np.random.seed` is only in a **diagnostics script + tests**, not
  the pipeline. So weathergen needs **no** cube-regenerating RNG change; F4's real targets are the
  downstream additive per-draw seeds and that one script. The canonical fix lives in `powersim_core.rng`
  for the downstream adoption (a future gated re-baseline).

## 5. Time-handling audit

- **F5 (result-changing, latent): the weather cube has NO leap days.** `simulation.nc` time = 175200 h =
  20×8760 exactly; Feb-29 is dropped for leap years (2028/32/36/40/44). Consumers index off the cube so
  they are *internally* consistent, but any join to a real-calendar series (RTE demand incl. 2020/2024
  Feb-29; ENTSO-E) is silently misaligned by one day after each leap day. No single leap-day policy.
- DST/UTC: internal grids are tz-aware UTC hourly (good); the DB stores ISO-UTC strings. 8784 (leap) vs 8760
  handled inconsistently across steps.

## 6. Classic-bug checklist (per §5) — first pass

| check | status |
|---|---|
| merges without `validate=` | only 2 merges total, both unguarded — trivial to fix |
| **pickle for persistence** | **8 files** save calibrated models as `.pkl` (avail/demand/res/weathergen) — spec: *no pickle ever* (non-portable/unsafe) → migrate to a safe format (F6) |
| `print()` in package code | **52** — CLIs legitimately print, but no `logging`/`structlog` anywhere (F7) |
| pandas chained-assignment / index-alignment | needs per-file manual pass (deferred to review PRs) |
| DST 23/25 h, leap day | see F5 |

## 7. Tooling / infra gaps (§5, §6, §8)

- **No monorepo / workspace**: 6 sibling packages, only `requirements.txt` (no `uv.lock`); only demand &
  weathergen have a `pyproject.toml`; **no root pyproject, no CI, no pre-commit, no ruff/mypy config**.
- **No unified data layer**: storage is a single SQLite DB + a NetCDF cube + per-model Parquet under ad-hoc
  paths; no catalog, no run ledger, no central pandera registry (schemas are per-package).
- **No scenario workbook consolidation**: each model has its own `assumptions_*.xlsx` + bespoke loader.

## 8. Severity-ranked findings log

| id | severity | finding | proposed fix | re-baseline goldens? |
|---|---|---|---|---|
| F5 | **result-changing** | weather cube drops leap days → misalignment vs real-calendar joins | decide leap policy (drop everywhere vs 8784) + `assert_canonical_grid` | yes (if policy changes) |
| F4 | result-changing (parallel) | no central RNG authority; `np.random.seed` global in weathergen | `core` `SeedSequence.spawn`, draw_id→child seed | yes for weathergen |
| F6 | result-neutral | pickle model persistence (unsafe/non-portable) | serialise params to Parquet/JSON | no (byte-check the reload) |
| F1/F2/F3 | cosmetic (pervasive) | timestamp/column/verb naming drift | glossary + rope renames, one PR/pkg | no |
| F7 | cosmetic | `print` not logging | `core` logging setup | no |
| dup | cosmetic | 5 config / 3 meta / 3 weather loaders | extract to `core/` | no (pure move) |

None are **blockers** to correctness of results shipped so far; F5 and F4 are the two that can change
numbers and must be fixed under the golden harness with quantified deltas.

## 9. Answers to §10 questions (from having built the code)

- **Known result-affecting items already suspected:** yes — the open backlog tasks: demand↔RES winter
  correlation −0.15 vs hist −0.34 (#39); HP-COP-vs-temperature scalar derate (#40); availability inferred
  from production not REMIT (#41, feed now available). These are *modelling* backlog, out of scope per §9,
  but relevant to golden baselining.
- **Irreproducible raw extracts:** the DB is rebuildable from RTE API + SYNOP archives + ENTSO-E token +
  ERA5 cache, but re-pulls are **not bit-stable** (RTE revises published history; ENTSO-E likewise). ⇒ the
  current `data/pricemodeling.db`, the `era5_cache/`, and `simulation.nc` should be archived read-only with
  checksums *now*, before anything else (they are effectively one-off).
- **Hourly canonical grid:** confirmed — hourly tz-aware UTC is the canonical output grid for all steps
  incl. late-horizon years; 15-min only as a derived layer (step-vi decision).

## 11. Refactor progress (2026-07)

- **Golden harness** (`tools/golden.py` + `golden/baseline.json`) — 13 model outputs + the cube frozen by
  numerical stat-digest; `check` gates every refactor.
- **`powersim_core`** installed editable: `glossary`, `time_grid` (F5 leap-day ADR), `rng` (F4 SeedSequence),
  `units`, `meta`, `weather_cube`. 9 tests green. `CONVENTIONS.md` + `docs/ADR.md` written.
- **Weather-loader consolidation DONE** (the "3 → 1" dup): `availability/io/weather.py`,
  `demand/projection/weather.py`, `res/io/loaders.py::load_weather_synthetic` all delegate to
  `powersim_core.weather_cube`. Each verified **bit-identical** to the old inline logic (availability diff
  0.0; demand 7.36M×6 rows equal; res equal incl. wind_100m). Golden green.
- **`meta.py` hashing primitives DONE** — res/avail/dispatch now import `_git_hash`/`_file_hash` from
  `powersim_core.meta` (each keeps its own `run_metadata` assembly, so the metadata contract + tests are
  unchanged; verified: keys identical, smoke tests green, golden identical).
- **`io/cache.py` DONE** — availability + dispatch delegate to `powersim_core.cache` (`disk_cached` +
  `mtime_key`); cache key format byte-identical so warm caches stay valid. Golden green.
- **Duplication now removed:** weather loader (3→1), meta hashing (3→1), disk cache (2→1). **Remaining
  dup:** 5 `config.py` (each genuinely package-specific — lower value), the p99.9 capacity scan (2 copies,
  bound to different tables), RNG (F4 downstream adoption — a gated re-baseline).
- **Tooling gate stood up (§4/§5):** root `pyproject.toml` with **ruff** (pycodestyle/pyflakes/isort/
  pep8-naming/pyupgrade/bugbear/comprehensions), domain style accepted (E702 compact `;`, N806/N803 math
  vars, line-length 120); `.pre-commit-config.yaml` (ruff + hygiene hooks, blocks large-file commits).
  Lint census: 508 → 194 after style-accept → **145 auto-fixes applied** (import sort/clean, modernizations)
  → 63 cosmetic remaining (F841 unused vars, C408, E701 — backlog). Verified: all 6 packages import clean,
  fast suites green (powersim_core 9 + dispatch 15 + weathergen 23), **golden identical**.
- **Column-name audit + dispatch normalization DONE.** Finding: **output schemas are already glossary-
  clean** — no artifact carries `ts_utc`/`time` (they use `available_mw`/`load_mw`/zone names, timestamp as
  index). So F1 needed **no output re-baseline**. The one real inconsistency (dispatch carried `ts_utc`
  internally and `schemas.py` mixed both names) is fixed: `entsoe_hist._to_hourly` + `fr_history` emit
  `timestamp_utc`, all 8 consumers + the pandera schema updated; DB raw column stays `ts_utc`, aliased at
  the boundary (ADR-3). Verified: neighbour tests pass, a 2022 backtest runs end-to-end, **golden
  identical**. `res` era5 DDL and `demand` loader read the DB `ts_utc` at the raw boundary — left per ADR-3.
- **Pickle killed (F6) DONE.** All **8** fitted models now persist as portable JSON (+ `.npz` array
  sidecar) via the new `powersim_core.serialize` (`save_params`/`load_params` for param bags;
  `save_dataclass`/`load_dataclass` for weathergen's nested `FittedModel`) — see ADR-6. Converted +
  migrated + **field-by-field deep-equal verified identical**: availability `CalibratedAvailability`,
  res `CalibratedRes` / stochastic `ResidualModel` / transfer bundle, demand `CalibratedModel` /
  residual `ResidualModel`, weathergen `Wind100Model` / `FittedModel` (1.55 GB → json+npz, round-trip
  identical). Key-type hazards handled in the model layer (tuple keys `pv_bias`; int keys
  `seasonality`/`seasonal_profile_week`/`monthly_clim`/`feat_clim` — a str-key miss would have silently
  changed sampling). `.pkl` files deleted; all call sites + tests moved to `.json`. Verified: core
  serialize tests 4/4, weathergen 23, res 30, demand 18 green; **golden identical**. No `pickle`/`.pkl`
  left in any package source.
- **Data layer (§6) DONE — Parquet lake + DuckDB catalog + schema registry.** New `powersim_core.lake`
  (`data/lake/{layer}/{dataset}/…/part.parquet`, zstd, order-preserving, `index=` passthrough, optional
  validate-on-write, `POWERSIM_LAKE`-overridable) is the sole output-I/O authority; every writer + cache
  migrated (availability×4, demand×3, res×4, dispatch×1) off ad-hoc `to_parquet`. `powersim_core.catalog`
  builds `data/powersim.duckdb` (one view/dataset, Hive partitions projected, `_catalog` + `runs` ledger).
  `powersim_core.schemas` centralises the pandera `validate` (the **4 duplicated copies removed** — dup
  census closed) + shared column builders. Existing outputs moved into the lake, golden repointed →
  **numerically identical**; availability re-run through the new writer (n_draws=2, matching the frozen
  ref) → still identical, proving writer fidelity + projection determinism. duckdb+pandera pinned. Verified:
  core 16 (+lake/catalog), availability 5, demand 18, res 30 green; **golden identical**. ADR-4 → DONE.
- **Scenario workbook (§7) DONE — one hand-edited `scenarios.xlsx`.** The 3 per-model `assumptions_*.xlsx`
  merged into a single repo-root workbook (28 tabs, prefixed by model); `powersim_core.scenario` is the one
  read path (`load_model_sheets`/`load_sheet`, prefix-stripping, legacy-tolerant) + `snapshot()` for
  immutable Parquet+manifest provenance. All 7 `read_excel` sites + 4 config `workbook:` entries repointed to
  `../scenarios.xlsx`; `dispatch_commodities` added as a real tab (seeded from the old code defaults → numbers
  unchanged, now editable); per-model `init-workbook` redirected to `_template.xlsx` so it can't overwrite the
  live file. Loaders proven byte-identical (values+dtypes) to the originals; availability re-run through the
  merged workbook → **golden identical**. Verified: core scenario 4, demand 18, dispatch commodities 4, res
  transfer+io 7, availability calibration 6 green. ADR-5 → DONE. Old workbooks now inert (user may delete).
- **F4 RNG adoption DONE — single authority + deliberate re-baseline.** The ad-hoc additive-salted seeds
  (`default_rng(seed + draw*1000 + salt)`) are gone; all per-draw randomness now comes from
  `powersim_core.rng` (`substream(seed, draw, label)` / `draw_rng(seed, draw)`, SeedSequence-keyed and
  collision-free across processes). Converted: availability ×4 (`common_mode`/`forced`/`interconnectors`/
  `planned`), dispatch commodity-OU + the unused additive `Config.rng` helper, and res/demand residual
  `simulate()` now take an `rng=` from the authority (threaded from the engines; `seed=` kept as a legacy
  fallback). **This is the one intentional result-changing fix**, so the golden baseline was re-captured
  (13 artifacts) after backing up the old one.
  - *Blast radius (as predicted):* only the stochastic draws moved — availability `by_tech` (mean
    −0.51 %), `nuclear_units` (−0.74 %), `interconnectors` (−0.09 %) and res `production` (38 fields).
    **Unchanged:** availability `reservoir_budget` (deterministic), all demand + dispatch artifacts (their
    goldens are the deterministic core / deterministic backtest) and the res caches.
  - *Acceptance = still valid, not identical:* shifts are sub-1 % Monte-Carlo resampling noise on a 2-draw
    ensemble, not a defect. Nuclear **Kd 0.755 [0.748, 0.762]** (was 0.760–0.762) — still inside the
    0.72–0.80 historical band; res trajectories intact (PV 38→118 TWh, onshore 52→92, ROR 41) and the PV
    double-count identity still balances. Suites green: availability projection 5, res projection+validation
    7, demand 18, dispatch commodities 4, core 20. ADR-2 (RNG scheme) → adopted.
- **Refactor backlog complete.** All of §3–§7 landed: golden harness, `powersim_core`, naming, tooling gate,
  de-duplication, no-pickle (ADR-6), Parquet lake + DuckDB catalog (ADR-4), single scenario workbook (ADR-5),
  RNG authority (F4/ADR-2). Remaining open items are **modelling** backlog, not hardening: #39 demand–RES
  winter correlation, #40 HP COP-vs-temperature, #41 REMIT feed, plus the IT/ES under-pricing + SMC→spot
  markup which are step-(vii)'s job.

## 10. Open — needs the team (see chat)

Team size / concurrency on the repo, and whether any external consumer already reads current output paths
(deprecation-shim requirement) — these set how aggressive the monorepo + path migration can be.

## 12. Phase-2 review — step-vii code (2026-07-19)

Scope: everything written since §11 closed (#63–#83, ~2 500 new lines), reviewed against CONVENTIONS.md +
the §5 classic-bug list, plus a repo-wide hygiene sweep. Rules: golden gate after every package; only
result-neutral fixes applied; result-changing findings reported, not silently fixed.

### Baseline correction (before any change)

The golden gate was **red before the review started**: 62 diffs, all traced to *signed-off* completed work
(#78/#81 REMIT recalibration: `source residual_anchored → remit_share`, new `duration_mult`; #68/#69/#71:
the evolved 2019 backtest with the ES 0-floor and RES bid stack) — the baseline had simply never been
re-captured after those tasks. Re-captured with backup
`golden/baseline_pre_phase2_review_20260719.json.bak`. Pre-review suites: **234 tests green** across the
7 packages (24+24+24+30+33+69+30).

### Findings log (severity · status)

| id | sev | finding | status |
|---|---|---|---|
| AR-19 | defect | dispatch CLI dead: all 4 commands hit `pipeline.py` `NotImplementedError` stubs while the real engines shipped in `rolling/` | **fixed** — pipeline wired to `run_backtest`/`project_trajectory`/`assemble_window`; `build-inputs` CLI verified live |
| AR-1 | infra | 3 cross-package import sites relied on caller-arranged `sys.path` (weather_shapes hack) | **fixed** — pyproject.toml ×4 added, all 7 packages installed editable, hack removed; ADR-8 |
| AR-10 | efficiency | `reconstruct_daily_availability` O(units×365) per-day loop, "slow" by its own docstring | **fixed** — vectorized (interval→day explode + stable-max); **row-identical** on FR-19/FR-22/DE-19/BE-23/ES-19 real data, ~60–95× faster |
| AR-12 | latent bug | `_eic_plant_type` GROUP BY let SQLite pick an arbitrary label for the 17 units with drifting plant_type | **fixed** — deterministic majority vote (dominant label; ties alphabetical). Affects only the opt-in #80 stats path, no shipped output |
| AR-8 | robustness | GB import tranches priced **by row position** (`s[-2:] = [52, 110]`) in 2 files — silent mispricing if the stack ever reorders | **fixed** — `GB_IMPORT_TRANCHES` constants + `price_gb_tranches` by unit_id |
| AR-4 | structure | projection imported backtest's *privates* (`_fr_window`/`_nb_window`/`_fr_stack_base`) | **fixed** — shared assembly moved to `rolling/windows.py` (pure move), both engines + markup import the public names |
| AR-17 | robustness | projection crashed opaquely if every weekly LP failed | **fixed** — explicit error naming the year |
| AR-13 | naming | driver frames used `ts` for the UTC timestamp (glossary: `timestamp_utc`) across markup/projection/weather_shapes | **fixed** — renamed at every site incl. tests (in-memory only → golden-safe) |
| AR-11 | naming | `weather_shapes` emitted a bare `temp` column (glossary violation) | **fixed** — `temperature_c`, boundary alias per ADR-3 |
| AR-3 | dup | markup `_features`/`_driver_bounds` duplicated the tightness/res-share computation | **fixed** — single `_ratios()` |
| AR-9 | dead code | `av[i,:] = f if np.isscalar(f) else f` (both branches identical) ×2 files | **fixed** |
| AR-6/7/16 | stale docs | projection docstring predated #76/#77; `res_beta` docstring described fields that don't exist; `_interp` said "extrapolate" but clamps | **fixed** |
| AR-18 | standards | year-interpolated SQL strings (ints — safe, but non-standard) ×2 | **fixed** — parametrized |
| AR-2 | structure | 7 flat step-vii root modules vs the original subpackage layout | **no change, deliberate** — each is small (62–256 ln), single-purpose, well-documented; a regroup = churn without benefit |
| AR-14 | style | 7 `print()` in ETL download/ingest progress paths | **kept** — legitimate operational output; full F7 `logging` migration remains an optional follow-on, not done silently |
| AR-15 | dup | `series._do` / `unavailability._do_unavail` share an error-classification block | **noted only** — extraction not worth the coupling |
| — | hygiene | 79 ruff findings repo-wide (35 E501, 11 F841 — each F841 inspected individually: all dead assignments, no hidden bugs) | **fixed** — repo at **0 findings**; N818 exception rename, E402 noqa'd with reason |

### Test-gap closure

Every step-vii module already had a test file (incl. the `_outage_type` UNPLANNED-substring regression).
Real gaps closed: `zone_availability_stats` / `tech_unavailable_mw` / `installed_by_tech` /
`_eic_plant_type` majority rule (4 new tests, `tests/test_entsoe_unavailability.py`). One doc-vs-code
mismatch surfaced by the new tests: an outage-free tech gets availability 1.0 (a no-op multiplier), it is
not "skipped" — docstring corrected, behaviour characterized as-is.

### Verification (the gate)

- **Live-behaviour proof**: a 2-week 2019 backtest captured *before* any dispatch edit and re-run after —
  **byte-identical** (336 h × 7 zones, `DataFrame.equals` = True). The lake write was patched out in the
  harness so the golden 2019 artifact could not be clobbered (the #66 lesson).
- **Golden**: `tools/golden.py check` green after every package and at the end.
- **Suites**: powersim_core 24, weathergen 24, demand 24, res 30, availability 33, pricemodeling 34 (+4
  new), dispatch **69/69** (full suite, final gate) — all green post-fix; **238 tests total**, golden
  green at close.
- AR-10 equivalence additionally proven on 5 real zone-years (row-identical incl. dtypes).

### Residual backlog (honestly stated)

- F7 logging migration (print → logging repo-wide) — optional, cosmetic.
- `series._do`/`_do_unavail` dup — accepted.
- The five per-model `config.py` remain separate (assessed twice now: genuinely package-specific).
- Neighbour weather models stay reduced-form and IT_NORTH markup R² is still negative — **modelling**
  backlog (documented in STEP_VII_METHODOLOGY.md), out of scope for a code review.

## 13. Golden re-baseline — the 100 m wind refit, and two projections the baseline had never re-seen (2026-09-14)

The gate went red when the wind100 refit regenerated `weathergen/output/simulation.nc`. It was re-captured after
regenerating, from that cube, the realization-0 projections the harness freezes (`res-model project`,
`demand-model project`). Backup: `golden/baseline_pre_wind100_20260914.json.bak`.

Against that backup, 5 of 14 artefacts moved. Unchanged: availability ×4, both dispatch backtests,
`res/hist_modelled_cf` and the two step-v reports. Not all of the delta is the wind; it is decomposed below as
far as it was measured.

| artefact | what moved | cause |
|---|---|---|
| cube `wind_speed_100m_ms` | mean 5.577 → 5.489, std 3.830 → 3.551 (all 76 stations); the seven other variables bit-identical | the refit (`weathergen/DECISIONS.md`, "wind100 diurnal shear") |
| `res/proj_drivers`, `demand/projection_features` (realization 0) | `w100_nat` 5.669 → 5.566; `temp_nat` / `T_nat` 13.205 → 13.163 °C; CDD mean 0.331 → 0.271 | the refit, AND a different cube. Temperature is bit-identical between the pre-fix and refit cubes, so the old partitions were built from another one. Realization 0 was shared by the default cube, REFERENCE_WEATHER_20Y and Monte-Carlo draw 0 under an mtime-only freshness test, now replaced by `source_cube.json` (`dispatch_model/DECISIONS.md`, 2026-09-14) |
| `res/production` (reference, realization 0) | PV total +14.7 %, onshore +9.9 %, offshore +1.6 %, national total +8.1 % | NOT the refit. The engine anchors each technology's CF mean, and the anchored onshore / offshore means are identical on the pre-fix and refit cubes (0.213 / 0.418). The frozen partition dated from 2026-07-15 and was never regenerated after later committed workbook and code changes. Not decomposed by commit |
| `demand/projection_hourly` (reference) | load mean 71,143 → 58,725 MW (−17.5 %); 2027-2046 energy now 443.5 → 567.1 TWh | NOT the refit: the workbook. The baseline was captured in 96e40bd (2026-08-12) on the PPE3 demand tabs of 8be1a62. Commit aba6a5d (2026-08-20) then carried a growth moderation of `demand_macro`, `demand_mobility`, `demand_new_large_loads` and `demand_residential_tertiary` to 590 TWh in 2050: λ 0.456587 on GDP / industry / data centres / electrolysis, 0.518555 on heat pumps and EV fleets (`dispatch_model/scratchpad/solve_balanced.py`, 2026-08-17). Its commit message does not mention it |

**Checking the demand attribution:**
- **Tab hashes:** the four tabs' contents are identical from aba6a5d to the working tree and differ only between
  aba6a5d~1 and aba6a5d.
- **Recomputation:** a read-only recomputation of `deterministic_net` on today's features, swapping only those tabs:
  - PPE3 tabs: 71,323.5 MW (the +0.25 % against the old golden is the weather);
  - live tabs: 58,724.655 MW, exactly the regenerated partition;
  - economic cut alone: −7,421 MW; electrification cut alone: −5,177 MW;
  - 2046 energy: 760.0 TWh on the PPE3 tabs, against 567.1 TWh on the live ones.

**Consequence for prices.** The Monte-Carlo reads the same tabs (`weather_shapes.fr_draw` → `project_trajectory`),
so the old and new ensembles price the same moderated demand. Whether 590 TWh is the intended reference is a
scenario question; it is not changed here.

Gate at recapture: `tools/golden.py check` identical. Suites: powersim_core 29, weathergen 35, demand 24, res 30,
availability 33, dispatch 380.
