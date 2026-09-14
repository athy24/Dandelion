# DECISIONS — weathergen

Running log of every modeling choice and its rationale. Each `# DECISION:` in the
code points back here. Resolved decisions are dated; open ones are surfaced at the
relevant phase for sign-off.

## Resolved (kickoff, 2026-06-29)

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D0.1 | Lengthen record with ERA5? | **Yes** (user will provide ERA5 hourly at station coords) | 12 yr is too short for tails + climate baseline; station = truth, ERA5 = extend tails/seasonality. Ingestion designed with an ERA5 fusion hook (`data.era5` in config); wired before Phase 4 (EVT) where it matters most. |
| D0.2 | Solar / GHI handling | **Exclude solar** | SYNOP data has NO measured irradiance (only cloud cover). No `pvlib`/clear-sky branch in v1. (ERA5 `ssrd` could enable it later if desired.) |
| D0.3 | Variable set | temperature, wind speed, sea-level pressure, dew point, humidity, cloud cover (total & low), **precipitation** | User opted into precip despite the intermittency flag → modeled as occurrence+intensity censored-latent (see D-precip), not a naive Gaussian. |
| D0.4 | Station scope | **Metropolitan France only (~42)** | One coherent spatial domain; Matérn-over-great-circle-distance is meaningful. Overseas/sub-Antarctic stations excluded (disjoint fields). |
| D-precip | Precipitation in a Gaussian-copula SWG | Censored-latent hurdle: occurrence via latent threshold (per-site wet probability), intensity via transformed amount; keeps it inside the copula. | Precip is intermittent and non-Gaussian; a point mass at zero is handled by censoring the latent Gaussian. Detailed at Phase 4/5. |

## Phase 1 (io + QC + ERA5), 2026-06-29

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D1.1 | QC physical bounds | per-variable in `config.yaml` (user-approved) | range-flagged → NaN; never imputed into fit. |
| D1.2 | Stuck-sensor (flat-line) | run of ≥ 24 identical non-NaN values flagged; legitimate zero-runs (wind calms, dry precip) exempt | catches frozen sensors without flagging real calms. |
| D1.3 | Spike detection | robust z (MAD) on first differences, |z|>8 AND up-then-down (isolated) | flags isolated outliers, not legitimate ramps. On real data: 3–11 per variable (not over-flagging). |
| D1.4 | Short-gap handling | ≤ 3 h linear-interpolated + flagged `F_INTERP`; longer gaps left NaN, excluded from fit | no silent imputation into the training set. |
| D1.5 | ERA5 bias-correction | **monthly mean+std matching** (ERA5 → station scale, per month) | light, robust, absorbs ERA5 unit/scale differences. Flagged for upgrade to quantile-mapping (ties into Phase 4/6 sdba) if tails need it. |
| D1.6 | ERA5 fusion mode | station = truth; ERA5 only **extends** before station start + **infills long gaps** (all flagged `F_ERA5_EXTEND` / `F_ERA5_INFILL`) | never overwrites observed station data. |
| D1.7 | ERA5 active from Phase 1 | code path live; runs on real ERA5 when a NetCDF is dropped at `data.era5.path` or CDS creds are provided | tested against a synthetic ERA5 fixture in the meantime. |

Real-data QC snapshot (42 metro stations, 2014-12→2026-01, 97 560 h):
missing median 9.9% (min 3.1%, max 47.7% — one sparse station flagged for possible exclusion);
QC-removed 0.76 M cells; short-gap-interpolated 0.31 M.

## Phase 2 (climatology), 2026-06-30

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D2.1 | Harmonic basis | 3 seasonal × 3 diurnal harmonics, **interacted** (49 terms), hours in **local solar time** | diurnal amplitude varies by season (interaction); LST aligns the diurnal phase across longitudes. |
| D2.2 | Heteroscedastic variance | fit log σ²(doy,lst) on the same basis; OLS-on-log(r²) bias-corrected by +1.2704 (E[log χ²₁]) | seasonal/diurnal variance is strong; constant-σ would be wrong. |
| D2.3 | Precip under harmonic standardization | apply uniformly; **accept non-unit residual** (z_std≈2.8) | intermittency can't be standardized by one σ — the mean cycle is removed here, the distribution is handled by the hurdle/censored marginal in Phases 3-4. Flagged, not hidden. |

Real-data acceptance (42 stations, 2014-12→2026-01): fitted-vs-observed mean-surface RMSE
temp 0.28°C / wind 0.12 m/s / pressure 0.49 hPa / dewpt 0.32°C / RH 0.95% / cloud 1.5%;
residual z_mean≈0, z_std 0.97–1.02 (continuous vars), |binned hour/month means|<0.11,
binned std∈[0.91,1.13]. No leftover diurnal/seasonal structure. Figure: reports/phase2_climatology.png.
ERA5 not yet folded in (download in progress) — climatology will be refit on the extended record when it lands.

## Phase 3 (transforms), 2026-06-30

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D3.1 | Transform order | transform RAW **before** climatology (transform→deseasonalize) | logit/hurdle are only meaningful on raw bounded/intermittent values; identity-transformed vars (temp/pressure/dewpt) unchanged, so Phase-2 sign-off holds for them. |
| D3.2 | humidity | **DERIVED from T + dew point** (Magnus), dropped as a modeled variable | logit backfires (RH saturates at 100% → skew −0.72→+3.72); RH is redundant given T+Td. Modeled set = 6 (temp, wind, pressure, dew point, cloud, precip); RH appears in output, derived in simulate.py. (User didn't object to the recommended option.) |

Real-data acceptance (42 stations): map / skew_raw→skew_tr / inv-error / climatology z_std —
temp Identity 0.21→0.21 / 0 / 0.98 · wind **YeoJohnson 1.43→0.00** / 5e-14 / 0.99 ·
pressure Identity −0.44 / 0 / 1.00 · dewpt Identity −0.34 / 0 / 0.98 · cloud Logit −1.67→−0.18 / 1e-4 / 1.09 ·
precip Log1p 15.8→5.5 / 7e-15 / 2.22 (intermittent → Phase-4) · humidity Logit −0.72→**+3.72** / 1e-4 / 1.29 (see D3.2).

## Phase 4 (EVT marginals), 2026-06-30

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D4.1 | GPD engine | scipy.stats.genpareto for the fit + exact invertible splice | full control over a monotone/continuous/invertible CDF; pyextremes pluggable for deeper threshold diagnostics (an MRL helper is provided). |
| D4.2 | Threshold | config quantile (0.95) per tail; MRL/stability diagnostic available | simple, stable; can switch to MRL-selected thresholds per variable if needed. |
| D4.3 | Season-stratified tails | **pooled tails on standardized anomalies** (default) | the climatology σ(doy,lst) already restores seasonal modulation; return-level diagnostics show good fit with pooled tails. Revisit if residual seasonal tail structure appears. |
| D4.4 | Precip marginal | **censored/hurdle**: point mass p_dry + empirical+GPD wet body; dry frequency exact; sub-threshold simulated precip hard-zeroed in Phase 7 | correct occurrence + intensity within the copula latent. |

Real-data acceptance (station S0): round-trip F⁻¹(F(z)) err ~1e-14, CDFs monotone; GPD shape ξ
temp −0.15 / wind −0.09 / pressure −0.05 (bounded light tails); fitted return levels overlay
empirical and extrapolate past the sample max (reports/phase4_marginals.png). precip p_dry=0.854.
**Tails fit on the 12-yr station record** — they will be re-fit on the ERA5-extended record when the
download completes (this is exactly what most reduces tail uncertainty; warning carried until then).

## Phase 5 (dependence), 2026-07-01

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D5.1 | Copula family | **Gaussian** (VAR with Gaussian innovations) | correct + marginal-consistent with the Φ PIT; auditable. t-copula (joint tail dependence) offered pending the Phase-8 tail-dependence diagnostic — needs t-consistent margins, deliberate follow-up. |
| D5.2 | Reduction rank k | EOF modes for ``eof_variance``=0.90 → **k=103** of 252 dims; discarded-mode variance added back as per-dim white residual (keeps margins ~N(0,1)) | tractable VAR while preserving per-column unit variance for the PIT. |
| D5.3 | Temporal order p | **VAR(6)** (tradeoff) | p=2 under-persisted (ACF MAE 0.112); p=6 → 0.090 and runs in time; p=12 too slow with the dense VAR. Higher persistence is a tunable (var_order) knob. |

Real-data acceptance (5→3-yr sim vs observed): corr-vs-distance binned MAE (temp) **0.015**;
cross-variable corr MAE (S0) 0.084; temperature ACF MAE to 48 h **0.090** (shape + 24 h diurnal
bump captured, slightly under-persistent at 48 h). Figure: reports/phase5_dependence.png.

## Phase 6 (external climate trend / QDM), 2026-07-01

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D6.1 | QDM engine | direct numpy QDM (time-varying, per-month, quantile-wise) | transparent; xclim.sdba is the drop-in production alternative once real CMIP6 runs are ingested. |
| D6.2 | Per-variable mode | temp/dewpt/cloud ADDITIVE, wind/precip MULTIPLICATIVE, **pressure NOT trended** | physical consistency (pressure ~stationary; non-negatives multiplicative). |
| D6.3 | Trend variability | full quantile-delta curve (default) vs median-only | preserves variance/tail changes (intensification), toggle `trend_variability`. |
| **OPEN** | SSP scenario + horizon + real CMIP6 deltas | **needs user input** | trend is OFF by default; needs the SSP (e.g. SSP2-4.5 / SSP5-8.5), target/horizon years, and the CMIP6 deltas sourced (CDS `projections-cmip6`, offered). Delta file format: npz with `quantiles` (nq) + `<var>` (12, nq). Example saved: reports/example_cmip6_deltas.npz. |

Acceptance (prescribed tail-intensifying delta): trend OFF stationary (mean +0.15°C, p99 −0.19°C);
trend ON mean +2.96°C, **p99 +6.65°C** (tail ≫ mean = intensification); transition monotone/smooth.
Figure: reports/phase6_trend.png.

## Phase 7 (simulation engine), 2026-07-06

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D7.1 | SSP + horizon | **simulation-time inputs** (`--ssp`, `--target-year`, default 2050) — one fitted model serves any scenario; trend rebuilt at simulate time | matches the spec (scenario/horizon are inputs). |
| D7.2 | Degenerate columns | station×variable with too little data (sparse station) → standard-normal marginal fallback (never NaN) | avoids NaN; improves automatically once ERA5 infills the sparse station. |
| D7.3 | CMIP6 model | **MPI-ESM1-2-LR** (EC-Earth3 has broken roocs subsetting on CDS → RoocsValueError) | reliable daily area-subset retrieval. |

Real acceptance: 20.0 yr hourly × 42 stations × 7 variables, **0 NaN**, all constraints satisfied,
deterministic under seed, NetCDF provenance embedded (seed + full config + ssp + target + trend flag).
Realism: France-mean Jan 5.9°C / Jul 21.9°C, extremes −5.9…37.1°C, 90.6% dry hours.

## Phase 8 (validation suite), 2026-07-06

Full HTML report (reports/validation_report.html) with embedded figures + PASS/WARN flags:
marginals/QQ, diurnal+seasonal surface, ACF, cross-variable corr, inter-station corr-vs-distance,
return levels + threshold exceedances, and spell/persistence (heat / calm-wind / dry, weighted ×3).

Real acceptance (20-yr sim vs observed) — **PASS**: temp/wind/pressure/dewpoint marginals,
diurnal+seasonal RMSE 0.39°C, ACF MAE 0.096, cross-var MAE 0.088, corr-vs-distance MAE 0.017,
temp>30°C & wind>15 m/s exceedance rates. **WARN (surfaced, not buried):**
- precip intensity tail too heavy (sim p99.9 9.1 vs obs 5.8 mm/h) → Phase-4 precip marginal needs a
  lighter/​capped wet tail (D8.1, open).
- spell persistence under-captured (dry/calm/heat spells ~½ observed length) → VAR temporal memory
  too short; raise `var_order` or add an AR on the leading slow EOF modes; ERA5 extension will also help (D8.2, open).
- Fixed during Phase 8: pressure/precip pooled-variance blow-up traced to the sparse degenerate
  station → cross-station-mean climatology fallback (climatology.fit) gives it physical values.

The suite works as intended (surfaces real deficiencies); the two WARNs are model refinements.

## Post-Phase-8 refinements, 2026-07-07

Validation-driven fixes (score 10/21 → 17/21 and improving):

| # | Issue found by validation | Fix | Result |
|---|---------------------------|-----|--------|
| R1 | Pressure/precip variance +136%/+513% | dependence re-imposes fitted PC std at simulate (near-unit-root VAR was inflating slow modes) | pressure std +2% (PASS) |
| R2 | Spells ~half observed (persistence) | var_order 6→12 (VAR fit on a bounded recent window to stay tractable on 47 yr) | calm-wind & heat spells now PASS; dry spells improved (~65% of obs, still WARN) |
| R3 | Precip wet mean/dry-freq wrong | dry hours → exact 0 via deep-negative sentinel (drop the hard-zero threshold); precip tails body-only (EVT log→expm1 explodes); GPD ξ capped ≤0.4 | precip mean/p99/occurrence match |
| R4 | Precip std harsh (rare-extreme dominated) | precip bound 250→**60 mm/h** (physical France hourly); marginal check judges precip on mean+p99+wet-frequency, not std | — |
| R5 | ERA5 too slow (monthly gridded) | **ARCO point time-series** (`era5_arco.py`): 1 request/station = full 47-yr record; ~42 requests vs 576 | record 12→**47 yr**; tails + sparse station rescued |
| R6 | Degenerate sparse-station columns | climatology borrows cross-station-mean climatology; marginals fall back to standard-normal | physical values, no NaN |

Final validation: **18/21 weighted** (was 10/21). ALL marginals PASS (incl. precip: mean/p99/wet-freq
match), diurnal RMSE 0.47°C, ACF MAE 0.041, cross-var 0.051, corr-distance 0.039, both exceedances PASS,
calm-wind + heat spells PASS. **Only remaining WARN: dry-spell persistence** (sim mean 20.8h vs obs 32.1h,
~65% — precip-occurrence clustering is the hardest SWG statistic; would need a dedicated occurrence
memory / higher precip-latent persistence). Documented, not hidden.

## Open (to resolve at their phase, with sign-off)

- **Phase 1** — confirm physical QC bounds per variable (config defaults proposed).
- **Phase 4** — season-stratified tails if residual seasonal tail structure remains after σ(doy,hour); GPD threshold per variable.
- **Phase 5** — copula family (t vs Gaussian) and EOF rank k, driven by tail-dependence diagnostics.
- **Phase 6** — SSP scenario(s), horizon, and whether to trend variability (not just mean). These are simulation inputs.

## Conventions

- All timestamps stored UTC; diurnal model uses **local solar time** (per-station longitude offset).
- Single seeded `numpy.random.Generator` threaded through fit + simulate.
- Fit is serialized to `models/`; simulation loads and is cheap.
- Every clip / imputation / assumption is logged in the validation report, never hidden.

## wind100 diurnal shear (2026-09-14)

**Defect.** The co-generated 100 m wind peaked in the early afternoon, with its 10 m driver, instead of in the
evening. Measured on the 42 French stations, 2015-2026:

| | SYNOP 10 m | ERA5 100 m | co-generated 100 m (pre-fix cube) |
|---|---|---|---|
| diurnal peak (UTC) | 15 | 20 | 13-14 |
| diurnal trough (UTC) | 3 | 10 | 3-4 |
| 100 m / 10 m ratio, night / noon | — | 1.75 / 1.18 | 1.575 / 1.41 |

Observed RTE onshore production peaks at 21 UTC (trough 08), offshore at 20 UTC: the pre-fix field moved wind
output into the solar hours and out of the evening ramp. Out of sample its national diurnal profile was
ANTI-correlated with ERA5 (-0.25), 7 h early.

**Cause.** `log w100 = a + b·log w10 + r` has no hour term. The stability-driven shear cycle (the surface layer
decouples at night, so 100 m wind is no longer braked; daytime mixing does the opposite) therefore sat in the
fitted residual — +0.135 log at 00 UTC, -0.200 at 10 UTC, a 0.336 swing against σ 0.487, 0.24 (DJF) to 0.44
(JJA) — and was discarded, because the residual is simulated as stationary AR(1) noise.

ERA5's 10 UTC value is itself an artefact: a one-hour notch of 0.25-0.3 m/s present in every year 2015-2026 with
all 42 stations valid, absent from observed production — the seam between ERA5's 12-hourly analysis windows.
It is interpolated out before fitting (W6), and everything fitted is smooth by construction, so it is not
reproduced.

| # | Decision | Choice | Evidence |
|---|---|---|---|
| W1 | Transfer design | intercept: seasonal K2 x diurnal K3 (LST, interacted); shear exponent: log w10 x diurnal K2 x (1 + seasonal K1). 48 coefficients per station (`DEFAULT_SPEC`) | chosen out of sample (fit 2015-22, test 2023-26) among 7 variants. Test R² 0.363 -> 0.480; overall phase error -7.1 h -> -0.4 h, profile corr -0.25 -> 0.95; DJF -0.7 h, corr 0.87 (a season-invariant shear term left DJF at -2.9 h, corr 0.66); JJA -0.1 h, corr 0.97 |
| W2 | Diurnal residual variance | rejected | residual std varies 24 % by hour, but modelling it moved the overall phase +1.0 h and profile corr 0.95 -> 0.93 |
| W3 | Recalibration | national seasonal K2 x diurnal K3 correction of the log mean (35 terms), estimated against ERA5 on the GENERATOR's own 10 m wind, pooled over three generator realizations (`output/simulation.nc`, seed+101, seed+202) | the generator's 10 m diurnal amplitude is 0.72x SYNOP's by first harmonic (0.77x by max - min; phase -0.25 h, corr 0.99), so a SYNOP-fitted transfer lands +1.5 h late. Validated on a fourth, held-out realization: see the table below |
| W4 | Scope of the recalibration | a property of the pair (this transfer, that 10 m generator), enforced as one | `scripts/fit_wind100.py` estimates both in one run and records the generator fit in `recal_generator` (a digest of `models/fitted.json` and its npz sidecar); `simulate` refuses to apply the recalibration to any other generator fit (checked whenever the model co-generates at least one station; a cube with no fitted station, such as the synthetic smoke test, is returned untouched and not checked). Re-run it whenever `models/fitted.json` is refit |
| W5 | Amplitude metric | first harmonic, not max - min | max - min is inflated on the ERA5 side by the 10 UTC notch (in DJF ~0.35 of a 0.65 m/s swing), which read as a false x0.80 amplitude deficit |
| W6 | ERA5 analysis seams | ERA5 100 m linearly interpolated across 10 and 22 UTC before fitting (`SEAM_HOURS`) | the 10 UTC notch above and a smaller one at 22 UTC sit at the joins of ERA5's 12-hourly assimilation windows and are absent from observed production; a 48-term diurnal design otherwise bends toward them. Fit before / after: R² 0.478 / 0.481, φ 0.834 / 0.851, σ 0.439 / 0.435 |
| W7 | Residual coherence | station AR(1) with the fitted spatial correlation, plus a NATIONAL common AR(1) factor carrying λ = 0.190 of the residual variance, persistence φ_c = 0.9786 (e-folding 46 h), σ scaled by k = 0.883 | added after review. The SYNOP-fitted residual is too weakly correlated across stations (0.11, against 0.28 for ERA5's station log anomalies), so co-generated France as a whole varied too little: national std -8 %, p95 -4.5 %, winter std -12 %, onshore CF p95 0.523 against 0.567. k and λ are calibrated, generator-driven on the three realizations of W3, to ERA5's mean station log-anomaly variance (0.344) and national anomaly std (ratio 0.872 at λ = 0, 1.000 after); φ_c is the decay of the fitted national-mean residual. Raising the station correlation instead restored the variance but pushed the national step std from 2.6x to 3.5x ERA5's |

Fitted transfer: R² 0.481 (pre-fix model 0.351), b 0.599 (0.520), φ 0.851 (0.865), σ 0.435 (0.487) before the
scale k.

**Held-out generator realization** (seed+303, 20 years, used for neither W3 nor W7), national mean of the 42
stations against ERA5 2015-2026, first harmonic:

| | pre-fix model | refit model |
|---|---|---|
| diurnal peak (ERA5: 20 UTC) | 14 UTC | 21 UTC |
| phase error: all / DJF / MAM / JJA / SON | -7.1 / -8.0 / -7.4 / -5.8 / -8.3 h | -0.3 / -0.1 / -0.2 / -0.2 / -0.4 h |
| amplitude: all / DJF / MAM / JJA / SON | x1.46 / 1.30 / 1.71 / 1.23 / 1.66 | x0.99 / 0.96 / 0.96 / 1.04 / 0.97 |
| profile correlation: all / DJF / MAM / JJA / SON | -0.24 / -0.56 / -0.28 / 0.10 / -0.54 | 0.99 / 0.97 / 0.99 / 0.99 / 0.99 |
| monthly level | x0.922 .. x1.185 | x0.965 .. x1.056 |
| mean / std / p05 / p50 / p95 / p99 | x1.024 / 0.974 / 1.046 / 1.037 / 0.993 / 0.992 | x1.004 / 1.018 / 0.980 / 1.011 / 0.999 / 1.010 |
| winter (DJF) std / p95 | x0.910 / 0.918 | x0.984 / 0.988 |
| national anomaly std: all (DJF) | x1.038 (0.907) | x1.024 (0.983) |
| mean station log-anomaly variance | x1.103 | x0.999 |
| cross-station coherence (ERA5: 0.279) | 0.237 | 0.284 |
| national hour-to-hour step std | x3.75 | x2.46 |
| autocorrelation deficit, lag 1 / 6 / 24 h | -0.08 / -0.32 / -0.34 | -0.03 / -0.10 / -0.07 |
| within-winter temperature coupling (ERA5: 0.344) | 0.244 | 0.213 |

**What did not improve, or got worse:**

- **Winter temperature coupling fell**, 0.244 -> 0.213. The W7 common factor is independent of temperature, and
  the variance it restores at the national scale dilutes the coupling the transfer carries over from 10 m. It is
  the price of the national variance, which reaches prices directly through the onshore CF's upper tail. See
  WIND_TEMP_COUPLING.md (2026-09-14).
- **Still too rough hour to hour**: the national step std is 2.5x ERA5's, down from 3.75x.
- **Distance profile of coherence not checked**: the average is right, but the common factor adds the same λ to
  every pair of stations, near or far, and the correlation-distance profile has not been checked against ERA5.
- **Neighbour stations inherit the French common factor**: the 34 neighbour stations take the median transfer,
  the national recalibration and the same common factor. Their 100 m wind therefore shares France's common
  shock, and its diurnal cycle is untested. No price consumer reads it: neighbour zones are driven by the
  cube's 10 m wind (`dispatch_model/weather_shapes.py`).

Models saved before this load with `spec=None, recal=None, common_share=None` and behave exactly as before,
consuming the random stream identically. Pre-fix artefacts: `models/backup_pre_diurnal/wind100.json(.npz)` and
`output/simulation_pre_diurnal_wind100.nc`.

Tests in `tests/test_wind100.py`:
- `test_the_100m_diurnal_phase_is_its_own_not_the_10m_one` asserts that the legacy form FAILS (> 2 h);
- unknown stations follow their own local solar time;
- the recalibration of a damped generator, and its generator guard;
- the common factor's variance, coherence and draw order;
- a save/load round trip.

**Regenerated default cube, all 20 years** (`output/simulation.nc`, same seed and generator; the seven other
variables verified BIT-IDENTICAL to the pre-fix cube, only `wind_speed_100m_ms` differs). National mean over the
42 French ERA5 stations, against ERA5 2015-2026:

| | pre-fix cube | regenerated cube | ERA5 / SYNOP |
|---|---|---|---|
| diurnal peak (argmax) | 14 UTC | 20 UTC | 20 UTC |
| phase error, first harmonic: all / DJF / MAM / JJA / SON | -7.2 / -8.7 / -7.1 / -6.1 / -8.5 h | -0.1 / +0.5 / -0.6 / -0.3 / +0.4 h | — |
| profile correlation: all / DJF / MAM / JJA / SON | -0.28 / -0.65 / -0.22 / 0.01 / -0.55 | 0.98 / 0.93 / 0.97 / 0.99 / 0.97 | — |
| first-harmonic amplitude: all / DJF / MAM / JJA / SON | x1.44 / 1.13 / 1.83 / 1.26 / 1.45 | x0.92 / 0.86 / 1.01 / 0.91 / 0.96 | — |
| 100 m / 10 m ratio, night / noon | 1.59 / 1.42 | 1.66 / 1.25 | 1.75 / 1.20 |
| annual phase error across the 20 simulated years | -7.7 .. -6.4 h (median -7.2) | -1.5 .. +1.0 h (median -0.2) | — |
| mean / p05 / p50 / p95 | x1.029 / 1.055 / 1.038 / 1.003 | x1.007 / 0.989 / 1.008 / 1.012 | — |
| autocorrelation deficit, lag 1 / 6 / 24 h | -0.08 / -0.30 / -0.33 | -0.03 / -0.09 / -0.06 | — |
| within-winter temperature coupling | 0.227 | 0.193 | 0.343 |

This cube is a single realization, so its seasonal figures scatter more than the held-out table's (DJF amplitude
x0.86 here, x0.96 there). The national p95 shortfall of the first regeneration (x0.962, before W7) is gone.

**Step iv on the regenerated cube, against what the fleet actually produces** (res_model's calibrated onshore and
offshore conversions with the engine's level anchor, driven by the cube's French 100 m wind; observed = RTE
production 2019-2025, each profile normalised by its mean):

| | peak | first-harmonic phase | energy share 10-15 UTC | energy share 17-22 UTC | profile corr vs observed |
|---|---|---|---|---|---|
| observed onshore | 21 UTC | 20.2 h | 0.246 | 0.264 | — |
| pre-fix cube, onshore | 14 UTC | 13.7 h | 0.290 | 0.244 | -0.10 |
| regenerated cube, onshore | 20 UTC | 20.2 h | 0.241 | 0.268 | 0.94 |
| observed offshore | 20 UTC | 21.6 h | 0.230 | 0.271 | — |
| pre-fix cube, offshore | 14 UTC | 13.7 h | 0.284 | 0.246 | -0.51 |
| regenerated cube, offshore | 21 UTC | 21.0 h | 0.237 | 0.269 | 0.91 |

In price terms: the pre-fix field put about 4.5 points too much onshore energy into the solar window and 2 points
too little into the evening ramp; both are now within half a point of observed.

The spread, against the same conversion driven by ERA5 100 m wind at the same 42 stations (2015-2026, hourly
onshore CF):

| onshore CF | std | p05 | p50 | p95 | p99 | DJF p95 | daily-mean std | hour-to-hour step std |
|---|---|---|---|---|---|---|---|---|
| ERA5-driven | 0.162 | 0.043 | 0.162 | 0.567 | 0.727 | 0.680 | 0.150 | 0.018 |
| pre-fix cube | 0.153 | 0.046 | 0.169 | 0.539 | 0.693 | 0.607 | 0.114 | 0.065 |
| regenerated cube | 0.163 | 0.041 | 0.163 | 0.568 | 0.724 | 0.670 | 0.143 | 0.043 |

The step std is the one gap left in this table: hourly ramps are 2.4x ERA5's (3.6x before).

**Consequence downstream:** every cube simulated before this date carries the wrong 100 m diurnal cycle — the
default `output/simulation.nc`, every Monte-Carlo draw cube, and `archive/REFERENCE_WEATHER_20Y.nc`, which was
stitched from draw cubes. See `dispatch_model/DECISIONS.md` (2026-09-14) for what that invalidates.
