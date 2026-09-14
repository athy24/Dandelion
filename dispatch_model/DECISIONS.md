# DECISIONS — dispatch_model (step vi)

Multi-zone economic dispatch → hourly zonal marginal prices. 7 bidding zones (FR unit-resolved; DE-LU,
BE, GB, CH, IT-North, ES aggregated), NTC-coupled. Consumes steps (ii)–(v) + a commodity module. Feeds
step (vii) which maps system marginal cost → day-ahead spot via a calibrated markup layer.

## Data reconnaissance (2026-07-14) — what the DB had vs what step vi needs

**Had (FR side):** demand (iii), RES (iv), unit availability (v), `rte_ntc` (French borders, 2021+),
`rte_physical_flows`, French weather cube (42 FR stations).

**Missing → provisioned via ENTSO-E:** the DB had **no usable spot prices** (`rte_market_prices` = 96 rows
of RTE last-resort/imbalance prices for 28-29 Jun 2026, €15k/MWh — not EPEX day-ahead), **no neighbour
history**, **no commodities**. The user supplied an **ENTSO-E Transparency token** (in `.env`,
gitignored); token verified live (FR day-ahead 2024-01-15 = 77.7, 72.1, 66.8… €/MWh).

## Blocking decisions

| # | Question | Choice |
|---|----------|--------|
| E1 | Backtest/neighbour data | **Ingest from ENTSO-E** (prices, load, generation-per-type, flows) for the 7 zones via `entsoe-py` → long-schema DB tables (`entsoe_*`). |
| E2 | Foreign weather anchor points | **Ship fallback** neighbour demand/RES regressions on FR-station predictors in v1 (quantify correlation loss for ES/GB); step-(ii) anchor-point extension is a later refinement. |
| E3 | Commodities | ENTSO-E does **not** provide TTF/EUA/API2/Brent → commodity module is **workbook-driven** (annual trajectories + monthly shape), stochastic OU layer behind a flag. History source TBD (deferred). |
| E4 | Build order | **Degraded 1-zone mode first** (FR unit-level + parametric border supply curves) end-to-end, then expand to 7 zones. Codebase zone-agnostic (`run.mode: single_zone | multi_zone`). |
| E5 | Optimisation | **Linear** dispatch (relaxed commitment) via **linopy + HiGHS**, not MILP. Prices = duals of zonal balance; scarcity priced with DSR tranches + VoLL slack + curtailment bids so negative/scarcity prices are endogenous. |
| E6 | Hydro | Start with **guide curves + weekly energy budgets** (option b); keep interface for SDDP water-values (option a). |
| E7 | Time resolution | **Hourly LP**; 15-min shaping deferred to step (vii). |
| E8 | Reserves | Static reserve-margin deduction per zone (v1); co-optimisation is a v2 refinement. |

Toolchain installed & verified: `entsoe-py 0.8.0`, `linopy 0.8.0`, `highspy`.

## Phase 1 (ENTSO-E ingestion) — in progress

`pricemodeling/entsoe/series.py` (extends the pre-existing `prices.py`): ingests day-ahead prices, load,
generation-per-type, and cross-border flows for the 7 zones into long-schema tables via the shared db
helpers. Yearly chunks + `ingest_log` = idempotent/resumable; per-chunk retry with backoff on 503/5xx/429
(ENTSO-E is flaky) and graceful continue-on-error. Backfill years: 2019 / 2022 / 2023 / 2024 (normal /
crisis / high-RES). Verified on Jan 2024: FR load 51.6 GW, 12 PSR types, DE_LU at native 15-min, flows
both directions. `dispatch_model/io/entsoe_hist.py` reads these back (resampled hourly, PSR→tech mapped).

## Phase 0 (scaffold) — done

`dispatch_model/` package: zone-agnostic `config.py`/`config.yaml` (7 zones + coupling graph + single-zone
mode), `meta.py` (hashes), `io/schemas.py` (zonal prices/dispatch/flows contracts), `io/entsoe_hist.py`
loaders, `cli.py` (build-inputs | run | backtest | validate), pipeline stubs.

## Phase 1 (ENTSO-E ingestion) — done

Backfilled prices / load / generation (5.3M rows) / flows for 2019/2022/2023/2024. Final log: **155 ok,
0 error, 9 nodata**. Every remaining gap is **GB** — post-Brexit, Great Britain is no longer published on
ENTSO-E Transparency (moved to Elexon/BMRS). **Decision:** model **GB as a border supply/demand curve**
(import/export tranches with a post-Brexit friction), not a full unit-modelled zone — consistent with GB
being non-coupled anyway. Sourcing GB from BMRS is a possible later refinement. The other 6 zones are
fully ingested.

> **SUPERSEDED.** The "later refinement" was taken: `pricemodeling.elexon` sources GB generation, demand
> and prices from BMRS, `pricemodeling.fx` converts BMRS's GBP to the lake's EUR via the ECB reference
> series, and GB is now a modelled zone with a balance and four borders. The trigger was not GB itself but
> the Netherlands: per-border NTCs left NL over-priced by €17.6/MWh because the model gave it two borders
> where reality has five, and BritNed could not be added until GB existed. The import tranches this
> decision created (`GB_IMP1`/`GB_IMP2`, 4000 MW on the FR stack) were removed in the same change — they
> are exactly the FR-GB NTC, and keeping both would have doubled the Channel.


### GB's two feeds are measured on different boundaries — and must be reconciled

Promoting GB exposed a defect no ENTSO-E zone can have. Every other zone's load and generation come from
the same TSO submission on the same boundary, so they balance by construction. GB's do not: Elexon's
`demand/outturn` reports **ITSDO**, demand at the *transmission* boundary and therefore already net of
distribution-connected plant, while FUELINST and AGWS meter *transmission-connected* plant. Measured on
2024 the balance is short **5851 MW — a fifth of GB demand**.

Left uncorrected this is not a cosmetic error: GB ran out of plant and hit VoLL in **612 hours against 36
observed**, and because BritNed couples it directly to a tight Netherlands, NL was dragged to 15 000
EUR/MWh in **97** of them, taking its mean error from −1.4 to **+29.0 EUR/MWh** — while Belgium and Germany
sat at 126–300 EUR/MWh in the same hours. One defect, one zone, not a distribution-wide drift.

**Decision:** reconstruct the residual hour by hour and net it off GB demand (`io/gb_embedded.py`), as a
month × hour-of-day median. It decomposes by measurement into an exact **solar double-count** (AGWS reports
national solar, which GB metering has already removed from ITSDO: regression coefficient −1.25, and
corr(residual + solar, solar) = −0.065) plus a flat **~7.3 GW embedded firm block** — CHP, waste, small
gas. Both are fixed by the same subtraction, since `(load + solar) − (residual + solar) ≡ load − residual`.

Netted off **demand** rather than added as **supply**, deliberately: as must-take supply at zero it would
manufacture negative prices GB does not have, whereas heat-led embedded CHP does not respond to price
anyway. Two consequences are accepted and recorded: GB's own stack adequacy is no longer tested by the
model, and the term absorbs Elexon's evolving feed coverage as well as Britain's embedded fleet (the
residual is 8.4–29.7 % of demand depending on year, and 2025's low reading tracks an AGWS wind revision
rather than a physical change). Both are acceptable because GB exists in this model to be a correct
**neighbour** for FR/BE/NL, not to have its adequacy assessed.

## Phase 3 (neighbour modules) — backtest mode done

`neighbours/blocks.py`: per foreign zone, `build_neighbour_stack` (aggregated tech blocks; thermal split
into efficiency sub-blocks for supply-curve slope; capacity = p99 of observed generation) + `neighbour_
netload` (load − must-take RES, from ENTSO-E actuals). Verified: DE_LU 65 GW (lignite 16.8 / coal 15.7 /
gas 11.8 / nuclear 9.5), ES gas-heavy 36.5 GW; net loads in the right bands; and **German fuel-switching
emerges endogenously** — 2019 coal≈gas≈lignite (~€45), 2022 gas €340 »  coal €147 / lignite €159. Tests
green. **Remaining:** projection-mode modelling (demand weather-regression on FR-station fallback, RES CF
transfers, workbook TYNDP/ERAA capacity trajectories) — needed for 2027-2046, not for the backtest.

## Phase 2 (commodities) — done

`commodities/model.py`: gas/CO2/coal/oil monthly generator. Deterministic = annual level (interpolated)
× seasonal shape (gas winter premium); stochastic = correlated OU log-deviations behind a flag, seeded
per draw. Backtest-year levels seeded from public annual averages (2022 gas €123, EUA €81 …). One source
of truth for FR + neighbour stacks. Monthly commodity history is a documented refinement. Tests green.

## Phase 4 (FR unit-level stack) — done

`io/fr_fleet.py` (FR dispatchable units + p99.9 capacity, disk-cached) + `stacks/costs.py` (SRMC =
fuel/eff + CO2·intensity/eff·EUA + VOM; per-unit efficiency dispersion → mid-merit slope) + `stacks/
fr_stack.py`. 168 units / 92.4 GW. Verified merit order and — critically — **endogenous fuel switching**:
under the 2022 gas shock the order flips to coal €140 < oil €231 < gas €339. Tests green.

## Phase 5 (LP core, single-zone) — done

`lp/single_zone.py` (linopy/HiGHS): least-cost dispatch vs net load; price = **dual of the balance
constraint**. Scarcity priced inside the LP (DSR tranches as high-SRMC units, VoLL slack, RES res_bid,
over-gen dump at the floor) so negative/scarcity prices are endogenous. Validated across all regimes:
floor −500 (deep oversupply) · −10 (RES curtailment) · 7 (nuclear) · 60 (gas) · 200 (oil) · 1000 (DSR) ·
15000 (VoLL); exact energy balance. Multi-zone NTC coupling extends this in the 7-zone step.

## Milestone — first real FR prices end-to-end (2026-07-14)

Ran the full chain on FR Jan-2019: `master_hourly` demand + must-take RES → net load → FR stack (SRMC
from 2019 commodities) → single-zone LP. First pass (autarky) blew the mean to €632 via false VoLL spikes
(FR imports in tight winter hours). Adding a **border supply curve** (import tranches as high-SRMC pseudo-
units — the spec's 1-zone-mode remedy) fixed it:

| metric | model | observed |
|---|---|---|
| mean | 37.6 | 61.2 |
| P50 | 43.7 | 62.8 |
| P95 | 48.0 | 83.9 |
| corr (hourly) | **0.712** | — |

The residual ~€20 level gap is fairly parallel across quantiles → the systematic **SMC→spot markup that
step (vii) calibrates** (uplift/ramping/bidding), not a model error. Confirms the chain (steps iii→iv→
stack→LP→duals) is sound. Border supply curve is now a required feature of single-zone mode; availability
here used a documented proxy (nuclear from rolling-max actual output, thermal 0.9, reservoir water-value
€40) pending REMIT (task #41).

## Phase 5b (multi-zone NTC-coupled LP) — done

`lp/multi_zone.py`: N zonal energy balances linked by NTC-bounded **directed** flows (fwd/bwd + tiny
gross-flow ε to kill loop flows). Each zone's price = its balance dual; spreads form endogenously. Per-
zone hydro energy caps + water values carried through. Validated: NTC-binding → decoupled prices + spread
+ flow pinned at NTC (cheap→expensive); ample NTC → prices converge to one system marginal. Zone-agnostic
(works for any {zone: stack, netload} set). Every configured zone carries a balance, GB included since its
promotion (see the superseded Phase-1 decision above).

## Phase 6 (hydro coordination) — reservoir done

Two-level decomposition, **option (b)**: `hydro/guide_curves.py` derives the weekly reservoir energy
budget from the historical seasonal generation profile (`master_hourly` prod_hydro_water_reservoir),
scaled by annual wetness; `rte_water_reserves` kept as the stock guide curve for a later SDDP swap
(option a). The LP (`energy_caps` param) caps reservoir generation to the weekly budget; reservoir is bid
at ~0 so it **self-allocates to peak hours (peak-shaving)** and the **water value emerges as the dual of
the budget cap** (verified = the €60 gas it displaces) — no more €40 placeholder. Tests green (budget
binds, peak-only dispatch, water value = displaced tech, DB climatology winter>summer).

**Remaining in this phase:** PSP round-trip storage arbitrage (charge/discharge state within the window)
and coarser CH/ES/IT-North reservoir budgets — refinements on top of the working FR reservoir mechanism.

## Milestone — first real 7-zone prices (2026-07-14)

`rolling/assemble.py` wires FR unit stack + 5 neighbour block-stacks + GB-as-border-curve + flat NTCs +
hydro budgets into the multi-zone LP. Double-count traps handled (ROR/solar/wind = must-take; PSP excluded
v1; reservoir budget = window's actual reservoir energy). First real 6-zone week (2019-01-14/21) solved in
0.2 s:

| zone | model | obs | corr |    | zone | model | obs | corr |
|---|---|---|---|---|---|---|---|---|
| FR | 49.9 | 62.1 | 0.53 | | CH | 50.6 | 62.7 | 0.58 |
| DE_LU | 48.4 | 48.5 | 0.55 | | IT_NORTH | 51.4 | 68.8 | 0.62 |
| BE | 49.9 | 57.9 | 0.54 | | ES | 49.1 | 65.7 | 0.84 |

VoLL scarcity 0 h, FR–DE spread sign-match 74%, DE_LU nearly exact. **First-pass fix that mattered:**
neighbour capacity from p99→**p99.9** of generation (p99 badly undersized peakers → false VoLL) + a **GB
import block** on FR. Residuals: (1) ~€12-15 low offset = SMC→spot markup (step vii); (2) under-
differentiation between zones from flat NTCs → **Phase 8** wires real time-varying NTCs (`rte_ntc`/ENTSO-E)
and ENTSO-E installed capacity. The multi-zone assembly + coupling is proven.

## Phase 8 (backtest + methodology) — first full-year results

`rolling/backtest.py`: preloads a year once, solves the multi-zone LP over ~52 weekly windows, scores §8
price metrics per zone vs observed ENTSO-E prices, writes Parquet + metrics CSV. **DSR tranches**
(300/1000/4000 €/MWh as high-SRMC pseudo-units per zone, spec §2) added — they step the price below VoLL
and were the fix that collapsed false-scarcity means (855% → sane). `METHODOLOGY.md` written (LP
formulation, hydro, simplifications+signs, backtest table, step-vii contract).

**Installed-capacity fix (big win):** neighbour stacks now sized from **ENTSO-E installed capacity ×
availability derating** (`ingest_installed_capacity` → `entsoe_installed_capacity`; `load_installed_
capacity`), not the p99.9-of-generation proxy which undersized peakers (DE gas 11.8→31.7 GW → DE was
over-priced). Full-year 2019 annual baseload went to **FR −4.3%, DE +4.4%, BE −4.0%, CH +0.6% (4/6 within
±5%)**, IT −19.7%, ES −19.3%; **correlations jumped to 0.71–0.74 (FR/DE/CH)** from ~0.4. P50 ≈0.

**Flow-derived NTC:** `flow_derived_ntc` sets per-border/direction NTC = p99.5 of realized physical flow
(congestion-reflecting; FR→IT 3.1 GW not 4.35, DE→BE 0.17 GW). CH → −0.3%; spreads now data-grounded.
Final 2019: **FR −3.8, DE +4.5, BE −3.5, CH −0.3 (4/6 within ±4.5%)**, corr 0.60–0.74.

Remaining residuals = calibration signal, not bugs: (1) parallel level gap + neg-P95 ≈ **step-vii markup**;
(2) **IT-North/ES −19% is NOT interconnection** (flow-NTC didn't move it) — they burn gas above TTF (PSV/
MIBGAS +€2–5/MWh_th) + larger day-ahead premium → zone gas premium + step vii (deliberately not ad-hoc-
fitted here). **Remaining:** projection-mode neighbours, 50-draw projection engine + Parquet, structural/
physics metrics (per-tech gen ±10%, net-export ±15 TWh), zone gas hubs.

## §8 projection-sensitivity checks — PASS

`assemble_window(price_mult=, nuc_avail_mult=)` perturbs commodities/availability; monotone checks on a
2019 winter week (tested in `test_sensitivity.py`): **gas +50% → prices up, FR more (gas-marginal), spread
widens**; **CO2 +50% → prices up, DE more (coal/lignite-heavy → higher CO2 intensity), spread narrows** —
correct fuel-mix physics; **nuclear −30% → FR price explodes, DE flat → FR premium** (common-mode-year
signature). Confirms the model's forward behaviour is directionally sound for the projection.

## Availability source for the backtest — settled

FR unit availability for backtests: **ENTSO-E REMIT** (`query_unavailability_of_generation_units`,
confirmed accessible — task #41) as ground truth once ingested, with step-(v) inference as fallback.
Supersedes the earlier "cap thermal at observed generation" idea.

## Out of scope (documented, handled by step vii calibration)

Intra-zonal grid, reserve co-optimisation, unit-commitment combinatorics, strategic bidding, FB market
coupling (plain NTC used). The SMC→spot gap is step (vii)'s calibrated markup/spread layer.

## Lazy rows — TRIED, MEASURED, REJECTED (2026-08)

Deferring the rarely-binding FLEX row families and adding them back only where violated. Exact by
construction: removing rows relaxes an LP, so a reduced solution violating none of the omitted rows is
optimal for the full problem and the omitted rows' duals are genuinely zero. Built to the letter — pure
per-family builders (kept, `_fam_*`), a round loop, a violation check, a round-cap fallback to the full
build of the same spec variant, and a mandatory invariant asserting every omitted row is satisfied.

**It works, and it is not worth it. Measured 1.16x.**

    one arm per process, warm-then-measure, 6 backtest windows of 2024
    full build   182.14 s   147 153 rows
    lazy         156.65 s   106 953 rows   (-27.3 % rows, 16 % faster)

The deferred set came from a dual census, not from expectation: over 10 backtest and 10 projection-2034
windows, C2a binds on 0.016 %/0.243 % of its rows and rho on 0.190 %/0.010 %, against C1a at 16 %/43 %.
C2a never added a single row in any window measured.

**Why 27 % fewer rows buys only 16 %: presolve was already doing it.** This repo measured presolve as
worth >20x on this model, and rows that never bind are exactly what presolve eliminates. The census
selected C2a and rho *because* they never bind — i.e. precisely the rows presolve was always going to
strip. The driver reimplements presolve's job in Python and pays extra rounds for it.

**A BEWARE for anyone reaching for this again.** An earlier measurement showed 3.90x and was wrong: both
arms ran in one process with the full build first, so it paid ~600 s of cache warming (`framecache`, the
`lru_cache`s, `_REGIME_NTC_MEMO`) that the lazy arm inherited for free. Two cold runs of identical work
measured 573 s and 455 s here — a 26 % spread — so ANY A/B on this codebase must run one arm per process
and compare warm runs.

### The finding that outlives it: the golden is not robust to solver perturbation

Lazy rows are exact PER WINDOW — replayed against a fixed seam state, objectives matched to 0.0000 with
zero violated rows across all 147 513 constraints. Across a SEQUENCE they are not, and the cause is not
the lazy rows:

* the LP has a large degenerate optimal face. Two solutions of one window differed in 2276 columns —
  49 GWh of Spanish generation, 20 GWh across ES>PT — at an objective difference of 5.3e-16, every
  block's cost-delta cancelling to zero;
* `flexibility.tail_state` carries the window's PRIMAL into the next window's seam (u_init/p_init/
  d_hist), so an arbitrary vertex choice becomes a genuinely different LP downstream. Measured: 5.3e-16
  in window 1 became 2.9e-05 in window 2.

So ANY perturbation moves the artifact: a HiGHS upgrade, a presolve change, a different platform. That is
true of the tree today, independent of lazy rows.

**A border tie-break was tried against this and REJECTED.** Extending the F6/F8 deterministic-perturbation
technique to per-border flow costs (`_EPS_FLOW` + up to 1e-4, keyed per border and direction) left the
divergence at 2.898e-05 unchanged, left the primal delta at ~4950 MW, spread the price difference from 14
cells to 132, and shifted one window's objective by 179. The reason it cannot work: the degenerate
direction is INTER-TEMPORAL, not inter-unit. Two flexible units swapping output between two hours cost
exactly the same because SRMC does not vary by hour, and no per-unit or per-border perturbation — both
constant in time — can separate that. Breaking it needs an hour-dependent cost perturbation, which
distorts the intra-day merit order.

## Four metered technologies belonged to no dispatch class — and were silently dropped (2026-08-12)

`io/entsoe_hist.PSR2TECH` maps sixteen ENTSO-E labels to model technologies. `neighbours/blocks.py` then
splits those technologies into `_DISPATCHABLE` (they bid) and `_MUSTTAKE` (they set net load). **Four of
the sixteen — `waste`, `geothermal`, `other_res`, `other` — appeared in neither list**, so they were read
out of the lake, mapped to a tech, and then dropped on the floor. Nothing failed; the generation simply
ceased to exist. Measured on 2024 as a share of each zone's own load:

    NL 34.6 %   IT_NORTH 8.8 %   IT_SOUTH 8.2 %   PL_CZ 2.8 %   BE 2.7 %   DK 2.3 %
    DE_LU 1.9 % AT_SI 1.8 %      GB 1.3 %         ES 1.2 %      FR 0.4 %   PT 0.5 %

**The Netherlands is a third of its own load.** This was found while chasing NL's +23.5 EUR/MWh dispatch
error, and it is the single largest defect the model has carried. The 2024 balance closes on it exactly:

    model asks NL dispatchable for  10.91 GW   (net load 10.44 + net exports 0.47)
    observed NL thermal              4.91 GW
              + Other                4.21   + Waste 0.32   + flat load residual 1.48
                                    10.92 GW      <- closes to 0.01 GW

### NL's `Other` is Dutch solar, and `Solar` is a 0.4 GW stub

The four are not one phenomenon and must not be treated alike. Measured per zone on 2024 (`day` = 10-15
UTC mean, `night` = 22-05, `corr` = correlation with that zone's own day-ahead price):

| category | day/night | corr price | reading |
|---|---|---|---|
| `waste` / `geothermal` / `other_res`, every zone | 0.95–1.01 | ~0 | flat must-run |
| **NL `other`** | **3.04** | **−0.444** | **solar** |
| every other zone's `other` | 0.98–1.39 | +0.09 … +0.47 | price-following |

TenneT reports the decentralised Dutch fleet under `Other` and leaves `Solar` a stub reading **0.055 GW
mean / 0.399 GW peak against a fleet of ~26 GW**. `corr(Other, Solar) = +0.871`; the diurnal runs 2.0 GW at
03:00 to 7.7 GW at 13:00; the seasonal peak is April–July. A model reading `Solar` therefore saw 0.4 GW of
peak Dutch PV where ~12 GW exists — which is why NL carried 458 observed negative hours against roughly
zero modelled, and why winter evenings (solar zero, industrial floor only) ran to VoLL: 3.3 GW missing at
2024-01-17 16:00 against a measured shortfall of 0.8 GW.

This also **retires an earlier misdiagnosis**. The `participation_caps` ceiling was suspected first, and it
is not the cause: NL gas peaked at 9.55 GW in 2024 against a 9.12 GW ceiling, and observed gas in the VoLL
hour was 8.15 GW — the ceiling never bound. Relaxing it would have loosened a mechanism that is very nearly
correct, to compensate for generation that was missing somewhere else entirely.

### Decisions

**Netting is sound here, and it was checked rather than assumed** — it is exactly the trap the GB entry
above documents from the other side. If NL's load were already net of this generation, subtracting it again
would double-count. The 2024 balance says it is not: `load 13.10 = generation 12.09 + net imports (−0.47) +
residual 1.48`, with the residual **flat to ±0.03 GW across day and night** (p10 1.45, p50 1.48, p90 1.50).
A flat residual carries no solar shape, so the load series is gross. (The 1.48 GW itself is an unexplained
load-definition offset — station service, pumping, or a boundary difference. It is level, not shape, and is
left for a later pass rather than folded into this one.)

* **`waste` / `geothermal` / `other_res` → must-run in every zone, taken whole.** IT_CNOR's geothermal
  averages 599 MW against a 636 MW peak — flat to within 6 % — and waste-to-energy burns because the waste
  arrives, not because the price cleared.
* **`other` → split exactly**, `mustrun[h] = min(floor[day(h)], other[h])` and `variable = other − mustrun`,
  so the parts sum back to the metered series in every hour with no leakage either way. The floor is each
  day's night minimum smoothed by a centred 7-day rolling median. What a fleet never drops below is
  price-insensitive whatever its fuel, so the floor is must-run in **every** zone.
* **The variable part is credited to must-take RES only where the series is demonstrably solar-shaped**
  (threshold 2.0; NL scores 3.04, the runner-up 1.39 — the threshold sits in a wide gap and is not doing
  delicate work).
* **Must-run lands on load, solar lands on must-take** — not interchangeable. In projection, load scales
  with the demand factor and must-take with the RES trajectory, which are the right drivers respectively
  for waste/geothermal/industrial baseload and for Dutch PV. `weather_shapes.py` applies the same
  correction *before* its temperature regression, since `load_coef` is what `shape()` evaluates to produce
  a projected load — fitting it on gross load while reporting a net `mean_load_mw` would leave the two
  halves of that model describing different quantities.
* **GB is excluded by construction, not by exception.** `gb_embedded` nets a load-vs-generation *residual*,
  and a residual already absorbs everything not otherwise represented; applying both would subtract GB's
  `other`/`waste` floor twice.

An early version of the floor used a per-month p10 over night hours and **leaked baseload into the solar
bucket**: 0.9 GW of "solar" at midnight and a Dutch PV total of 26.3 TWh against IRENA's ~21. A floor must
sit *at* the night level, not under its tenth percentile, because solar at night is zero rather than small.
The day-tracking floor gives **22.3 TWh** — within 6 % of IRENA, on a quantity derived purely from shape.
`tests/test_unclassified_gen.py` pins that as a regression, along with the exactness of the split.

### Left out deliberately

The **variable** part of `other` where it is price-FOLLOWING: IT_NORTH 12.0 TWh at corr +0.330, the Italian
south, GB 3.3 TWh at +0.465 — 8.6 TWh unrepresented in total, 7 % of the 131.2 TWh at stake. That
generation is real and it is dispatchable, so representing it needs a short-run marginal cost, and `other`
is a residual label with no fuel. Netting it off load would assert it is must-run, which the positive price
correlation refutes; a guessed SRMC would be an invented supply curve in the two zones whose levels are
already the worst in the model. It stays out until it can be given a cost. `scripts/audit_unclassified.py`
prints the split per zone so the omission is found by measurement rather than rediscovered as a surprise.

### Measured — the solar half shipped, the must-run half did not

The two halves were gated separately (`DISPATCH_UNCLASSIFIED_SOLAR` / `DISPATCH_UNCLASSIFIED_MUSTRUN`)
once the first combined measurement came back worse, because they are independent corrections that only
happened to arrive together. Multi-year gate:

| arm | \|mean err\| | log_err | NL 2024 neg (obs 458) |
|---|---|---|---|
| pre-fix baseline | 11.15 | 0.737 | 951 |
| **solar half only — SHIPPED** | **11.00** | **0.692** | **379** |
| both halves | 12.71 | 0.785 | 742 |
| both, solar netted off load | 13.88 | 0.849 | 1829 |

The **solar half** beats the baseline on both pooled metrics and leaves every other zone within
0.5 €/MWh (FR −4.3→−4.2, DE_LU −23.9→−23.8, CH +7.3→+7.2) — it is a NL-only correction that *replaces*
a synthetic reconstruction rather than adding supply. On by default.

The **must-run half** is off by default. It costs 1.7 €/MWh of pooled mean error because it lowers every
zone ~4 €/MWh — helping the three zones priced too dear and hurting the five already priced too cheap.
CH had **0.00 GW added yet moved −5.7**, so most of the movement is contagion, not local supply. The
generation is real; this is not a correctness verdict. It is that adding correct supply to a model with a
pre-existing cheap bias moves it further from observation, and the bias must be found first.

### It supersedes `btm_solar` — the model was already reconstructing this fleet, wrongly

The decisive finding, and the reason the first combined measurement looked like "correct but harmful".
`rolling/backtest.py` and `rolling/projection.py` already reconstructed Dutch behind-the-meter PV via
`flexibility/res_potential.btm_solar`, on the stated premise that the fleet is *"invisible on BOTH sides
of the ENTSO-E balance — not in generation (behind-the-meter)"*. **The premise is false.** The
"0.5 TWh/yr metered" it cites is the `Solar` key alone (0.055 GW × 8784 h = 0.49 TWh); the fleet is in
`Other`, which no dispatch class claimed — dropped, not absent.

|  | energy | peak | peak/nameplate |
|---|---|---|---|
| `btm_solar` | 28.4 TWh | **22.28 GW** | **0.80** |
| metered `Other` solar | 22.3 TWh | 13.04 GW | 0.47 |
| actual (IRENA/CBS) | ~21–23 TWh | | |

`corr(btm, metered) = +0.913` — the same fleet twice. The **energies nearly agree; the peak is what was
wrong**, and a 0.80 peak factor across 28 GW of mixed roof pitch and orientation is unreachable. That
excess peak was NL's phantom midday surplus: **727 model-only negative hours in 2024, every one between
05:00 and 15:00 UTC, March–September**, with NL the cheapest zone in nearly all of them. Running both
paths gives 50.6 TWh at a 35.3 GW peak against a real ~21 TWh — the double-count that produced NL's
−27.4 €/MWh in the first arm. Both call sites are gated on `solar_enabled()`, so disabling the half
restores the synthetic path rather than leaving NL with no solar.

Corroborating: observed NL and DE_LU go negative together in **86 %** of NL's negative hours (396/458);
the pre-fix model managed **24 %** (231/951). It had decoupled the Netherlands from Germany.

### Netting it off load instead — TRIED, MEASURED, REJECTED

Net load is identical whether the solar part is subtracted from `load_mw` or added to `musttake_res_mw`
(`load − mustrun − solar − musttake` regrouped), so the choice looks cosmetic. It is not: it decides
whether the solar is **curtailable**. Netted off load it becomes inflexible negative demand, so NL's
surplus hours ran past the RES bid floor to the **−500 EUR/MWh price floor in 997 hours of 2024**, and NL's
pooled mean error went −19.8 → **−60.6**. Every other zone came back bit-identical, which is the tell: the
thermal dispatch never moved, only NL's own dual.

This **refines the GB decision above rather than contradicting it**. `gb_embedded` nets embedded generation
off demand precisely to avoid manufacturing negative prices — and that manoeuvre is only safe for genuinely
inflexible generation. Applied to a 13 GW-peak solar fleet in a zone that does price negative, it does not
suppress the low tail, it detonates it. The distinction to carry forward is flexibility, not accounting.

## Published NTC "throttling" — CLAIMED, TESTED, RETRACTED; the truth is the opposite (2026-08-13)

Recorded because the wrong version of this was written into this file for several hours, and because the
error is an easy one to repeat.

**The claim.** Chasing NL's residual surplus, the applied NTC was compared against `entsoe_flows` and
found to be exceeded constantly — NL→DE_LU capped near 1081 MW against flows reaching 5171 MW, over the
cap in 32 % of 2024 hours; FR→CH, DE_LU→CH, FR→IT_NORTH and BE→NL the same. The reading was that the
model forbids transfers that physically happened, isolating NL and widening both its price tails.

**Why it is wrong, twice over.**

1. `entsoe_flows` is ingested via `client.query_crossborder_flows` — ENTSO-E **physical flows** (A11), not
   scheduled commercial exchanges (A09). In a meshed AC grid physical flow routinely exceeds commercial
   NTC through loop flows and transit, so "flow > NTC" is ordinary grid physics and proves nothing. The
   first comparison was also made against the series MEDIAN, which is not even the right scalar: the
   published series is genuinely hourly (NL→DE_LU has 751 distinct values spanning 0–4907 MW).
2. The valid test is price coupling — when two zones clear at the same price the border was not congested,
   so the commercial exchange was unconstrained in that hour. In those hours the published NTC is **not**
   small: NL→DE_LU median 1081 MW with only 130 of 1754 coupled hours below 500 MW. The published series
   is consistent with observed coupling.

**And the model runs the other way.** Comparing model against observed price convergence (|ΔP| < 1 €/MWh)
shows the model **UNDER**-congests nearly every border — its zones are too tightly coupled, not too
isolated:

| border (2024) | observed coupled | model coupled | gap |
|---|---|---|---|
| CH→IT_NORTH | 3.3 % | **69.1 %** | +65.8 |
| BE→FR | 30.1 % | 74.0 % | +43.9 |
| CH→DE_LU | 8.2 % | 50.1 % | +41.9 |
| FR→CH | 5.6 % | 25.2 % | +19.6 |
| NL→DE_LU | 41.3 % | 49.4 % | +8.1 |

2025 is worse on every line. Over-coupling compresses the zonal price distribution — it drags the dearest
zone down and lifts the cheapest — which is the shape of IT-North's **−12.7 €/MWh** (observed the dearest
zone at 107) sitting coupled to Switzerland 69 % of the time against 3.3 % observed. The model reproduces
only half of the observed IT-North↔CH spread (16 €/MWh against 31).

**Not yet a cause, and deliberately not written up as one.** Price-equality frequency conflates two
mechanisms: an over-generous border AND two zones simply sharing a marginal fuel. Both would show as
coupling. A plausible contributor is that `flow_derived_ntc` takes p99.5 of realized PHYSICAL flow and
uses it as a COMMERCIAL transfer limit — the same physical-vs-commercial confusion that produced the
retracted claim above, but this time in the model rather than in the analysis, and biased toward
capacities that are too large. Discriminating between that and a stack/fuel explanation needs the model's
own border flows, which the gate does not currently persist. **Open, and the largest lead still standing.**

## `solar_uplift` booked cloud cover as curtailment — FIXED (2026-08-13)

A parallel audit of every synthetic reconstruction in the model (5 investigations, adversarially verified)
found `flexibility/res_potential.solar_uplift` — applied to EVERY neighbour zone at `backtest.py:221` —
adds **46.1 TWh (2024) and 51.1 TWh (2025)** of zero-cost must-take RES across the neighbour zones, of
which **70–72 % lands on hours the market priced ≥50 €/MWh**, where no operator curtails PV.

The falsification is clean: the estimator returns the **same 16–25 % uplift in zone-years where
curtailment was impossible because the zone never priced negative**. IT_NORTH has zero negative hours in
2019, 2022, 2024 *and* 2025, yet is uplifted 16.3–24.6 % every year. A curtailment-free synthetic placebo
reproduces 102–150 % of the real uplift in all 14 zone-years tested — i.e. essentially all of it is
day-to-day cloud variability, not censored potential. A defensible upper bound on the genuine signal
(the negative-hour dip in excess of the hour-of-day-matched positive-hour mean) is DE_LU 0.99 TWh,
ES 0.65, GB 0.07, IT_NORTH 0.00 — roughly a tenth of what DE_LU alone is booked.

Two scope facts that bound the damage: it is **peak-bounded** (uplifted peak equals observed peak in all
28 zone-years tested, so unlike `btm_solar` it cannot inflate the peak), and it **does not reach the
forward projection** (`projection.py` imports only `btm_solar`). It is an energy/placement bias in the
backtest and gate-scoring path — 82.4 TWh across the four gate years.

Separately, its price filter is **entirely disabled** for DK, PL_CZ, AT_SI and IT_SOUTH: `_observed_prices`
looks up the virtual-zone key while the lake stores prices under the constituent keys, so `prices=None`
and the noise floor falls to 0 — the docstring's "conservative over-uplift, flagged for projection use"
branch firing silently in the backtest for 4 of 12 zones, worth +12.05 TWh in 2025. Same "dropped rather
than absent" template as the Dutch `Other` defect.

**This is the leading candidate for the cheap bias** that made the must-run half unshippable: ~46 TWh/yr
of free zero-cost energy in the neighbour zones is exactly what would hold DE_LU at −23.9 and BE at −11.6.

### The fix: a floor has to be in the tail, not at the centre

**Root cause, in one line: the noise floor was a MEDIAN.** The term exists to subtract ordinary cloud
variability so that what survives is censored potential — but taking its *centre* leaves half of every
uncensored hour's dip above it by construction. Two changes, both in `flexibility/res_potential.py`:

1. `_NOISE_Q = 0.90` — the floor is now an upper quantile of the same-hour dip over clearly-uncensored
   hours. Measured effect on the 2024 inputs, summed over the 12 neighbour zones:

       floor quantile   0.50 (was)   0.75    0.90 (now)   0.95   0.99
       uplift TWh             29.0   11.6           3.6    1.5    0.2

2. `prices=None` ⇒ **no uplift**, replacing `floor = 0` (the full dip counted as curtailment — maximum
   uplift precisely where there is least evidence, and described in the docstring as "conservative"). The
   call site in `backtest.py` now averages CONSTITUENT prices for the aggregate zones, so IT_SOUTH gets a
   properly floored uplift instead of none; DK/PL_CZ/AT_SI have no constituent prices in the lake for
   2024 and now correctly receive nothing rather than an unfloored ~17 TWh.

Together: **46.1 TWh → 3.6 TWh** on 2024.

`q90` is calibrated on two independent axes. On real data it lands nearest the audit's bound on the
genuine signal (DE_LU 1.29 vs 0.99 TWh, ES 0.77 vs 0.65, GB 0.26 vs 0.07; q95 undershoots DE_LU and ES by
about half). On a synthetic pair of solar years — one curtailment-free, one with curtailment injected on
negative-price hours — it trades recovery for precision the right way:

| floor q | placebo leak | real curtailment recovered | **false uplift on clean hours** |
|---|---|---|---|
| 0.50 (was) | 15.8 % | 90.0 % | **191 %** |
| 0.90 (now) | 0.7 % | 25.2 % | **9.9 %** |
| 0.95 | 0.3 % | 15.2 % | 1.4 % |

At the old setting the estimator invented nearly twice as much curtailment on clean hours as it recovered
on real ones — and since most hours are uncensored, that leak *is* the 46 TWh. Under-recovering real
curtailment is the safe direction; inventing it is not. `tests/test_res_potential.py` pins the placebo
property and both thresholds at their measured values.

## Le derating nucleaire global plafonnait quatre parcs sous leur propre moyenne observee (2026-08-13)

`_AVAIL_FACTOR["nuclear"] = 0.78` is a single global constant. On 2024 it put the capacity ceiling of four
zones **below their own observed annual mean output**:

| zone | nameplate | ×0.78 | observed **mean** | observed max |
|---|---|---|---|---|
| BE | 3929 | 3065 | **3385** | 3971 |
| ES | 7117 | 5551 | **5957** | 7118 |
| CH | 2970 | 2317 | **2614** | 3036 |
| NL | 486 | 379 | **385** | 490 |

A ceiling below the mean of what actually happened is not a modelling choice — no dispatch can reproduce
the observed year against it. ~1.0 GW of baseload was permanently absent.

### A floor, never a replacement — and that is the whole safety argument

`METHODOLOGY.md` records why this module moved AWAY from generation quantiles to nameplate × derating: a
quantile UNDER-reads plant that rarely runs at full output (DE gas read 11.8 GW against 31.7 GW installed,
which over-priced Germany). `_measured_avail` therefore returns `clip(median(observed)/nameplate, default,
1.0)` — it can only ever **raise** a factor, so the old failure mode is unreachable. Verified on 2024
across every zone and technology:

* **binds** — BE/CH/ES/NL nuclear, whose median output is 0.85–1.00 of nameplate;
* **no-op** — every peaker, by a wide margin: DE_LU gas p99.5 = 0.48 against its 0.90 default, ES coal
  0.29 against 0.88, DE_LU oil 0.20 against 0.85;
* **no-op** — GB nuclear, whose AGR fleet genuinely runs at a median of 0.50 and correctly keeps 0.78.

**The MEDIAN, not a high quantile.** p75/p95 put these fleets at ~1.00 of nameplate, which would let the
model run them flat out in every hour and over-supply the mean instead of under-supplying it. The median
carries the honest claim — *the derating may not assert less capacity than the fleet delivered in half its
hours* — and clears the defect with the least overshoot (BE 0.88 × 3929 = 3456 against a mean of 3385).
Result: BE 3065→3456, CH 2317→2922, ES 5551→6070, NL 379→478.

### Measured: best pooled mean error and best scarcity recall of any arm

| arm | \|mean err\| | log_err | negatives (obs 6136) | >200 €/MWh (obs 34917) |
|---|---|---|---|---|
| pre-`unclassified` baseline | 11.15 | 0.737 | 4915 | 36418 |
| + NL metered solar + `solar_uplift` q90 | 11.08 | **0.652** | 2523 | 37091 |
| **+ per-zone availability floor** | **10.45** | 0.666 | 2912 | **35533** |

Both modern years improve on BOTH metrics (2024 |mean| 10.13→8.31 and log_err 0.838→0.820; 2025 8.78→8.17
and 0.797→0.794). Scarcity recall lands 616 hours off target against the previous arm's 2174.

**The split is informative, and it is the same split as everything else this session.** The three zones
that improve are the over-priced ones, and they improve a lot — **CH +10.1 → +4.8**, PT +8.3 → +3.9,
NL +4.2 → +3.1. The five that worsen were already priced too cheap and move ~2 €/MWh each (BE −8.6 →
−11.7, DE_LU −19.8 → −21.8, FR −2.0 → −4.4, ES +1.7 → −3.0, IT_NORTH −4.3 → −6.3). Adding physically
real capacity to a model with a residual cheap bias will keep doing this: **DE_LU at −21.8 and the 2022
merit order remain the dominant unexplained terms**, and the single log_err regression here is confined to
2022 (0.628 → 0.713), the year whose coal-ahead-of-gas defect is diagnosed and unfixed.

On by default; opt out with `DISPATCH_ZONE_AVAIL=0`. The flag is part of the `_STACK_CACHE` key so an
in-process A/B cannot be served the other arm's stack. `tests/test_zone_availability.py` pins the floor
property, the peaker no-op, the 1.0 cap and the fallbacks.

**Time-varying neighbour availability is the structurally right answer** — a flat number cannot represent
a fleet that is either at nameplate or in outage. This only stops the flat number from being provably too
low.

## Le lignite de fond de mine etait facture au prix du charbon maritime (2026-08-13)

`stacks/costs.py` mapped `"lignite" -> "coal"` in `FUEL_COMMODITY`, so German/Polish/Czech lignite was
charged the seaborne hard-coal index. Lignite is dug in an opencast pit beside the plant, moved by
conveyor, never traded and never shipped; its cost is a **production** cost of ~1.2–1.8 €/GJ ≈ 4.3–6.5
€/MWh_th, essentially flat year to year. In July 2022 the model charged it **37.9 €/MWh_th — about
264 €/t of hard coal — for fuel that costs ~5**.

**This repo recorded the symptom without recognising it.** The Phase-3 verification note above reads:
*"2022 gas €340 » coal €147 / lignite €159"*. Lignite above hard coal is an **inversion** — lignite has
the worse efficiency and the higher CO2 intensity but by far the cheaper fuel, which is precisely why
German lignite runs baseload and hard coal does not. Corrected SRMC for July 2022, worst-efficiency block
of each class: **lignite 195 → 101, coal 184, gas 473**.

### It makes the backtest worse, and it ships anyway

| arm | \|mean err\| | log_err | DE_LU 2022 mean err |
|---|---|---|---|
| previous shipped | **10.45** | 0.666 | −79.7 |
| + lignite fuel corrected | 11.35 | 0.667 | **−95.3** |

The over-priced lignite was a **compensating error**: it propped up the German price plateau and masked
part of a deficiency that lives elsewhere. Removing it exposes the true size of the DE_LU 2022 gap.

Shipped ON regardless, for three reasons:

1. **`log_err` is unchanged (0.666 → 0.667).** The distribution's *shape* is untouched; only the level in
   one year moved. 2024 improves marginally on both metrics.
2. **Projection integrity, which is the decisive argument.** This is a 2027-46 projection model. Chaining
   lignite to a seaborne index means every scenario in which coal prices rise makes German lignite look
   uneconomic and retires it from the merit order — physically false, and it would distort twenty years
   of projected German baseload. The compensating benefit is backtest-only; the damage is structural and
   forward-looking.
3. Keeping a known-false input because it happens to offset another error is how a model becomes
   unfalsifiable. The offset is not robust — it depends on the coal price staying high.

`DISPATCH_LIGNITE_FUEL=0` restores the old behaviour for A/B or if backtest level is preferred.

### What this says about the 2022 merit order — the request that produced it

The chantier was "fix the 2022 merit order". Two hypotheses were tested and **both failed**:

* **Rhine low water restricting coal barges — REFUTED on three independent counts.** The Kaub gauge
  bottomed in mid-August yet DE coal's monthly max ROSE 8552 (Jun) → 8977 (Jul) → 11381 (Aug, +33 %) →
  12743 (Sep, +42 %), straight through the worst low water; strictly tidewater plants immune to Rhine
  levels show a LARGER relative summer outage swing (19.7×) than Rhine-fed ones (3.8×); and the entire
  barge-fed fleet above the bottleneck is 3.9 GW nominal (5.5 GW including the Saar/Moselle), too small
  to open an 8 GW hole. Monthly coal max instead tracks the REMIT outage calendar at corr = −0.839.
* **"It is just summer maintenance seasonality, and 2022 is unremarkable" — ALSO REFUTED**, and this is
  the subtler error. Comparing summer dips across years has **no valid control**: coal SRMC vs spot in
  May–Jul was in-merit 15 % of hours in 2019, 12 % in 2023, 24 % in 2024, 27 % in 2025 — but **79.7 % in
  2022**, with 177-hour continuous in-merit runs against 7–12 hours elsewhere. In every other year the
  output envelope measures *demand for the fleet*, not its availability. 2022 is the ONE year in which
  "did not run" implies "could not run", so it is the one year the seasonality argument cannot be applied
  to.

**The 8 GW of summer coal headroom is real and it is availability** — but the honest sizing rules it out
as the fix: removing 8 GW in May–Jul is worth **+6.45 €/MWh, 8 % of DE_LU 2022's −79.7 bias**. Capping
coal at its metered monthly max in every month reaches only +18.6. Deleting the ENTIRE German hard-coal
fleet all year is the absolute ceiling at +73.0, and it breaks October and November. The reason is that
**lignite is interleaved with coal in the merit order**: removing a coal block hands the margin to a
lignite block €13–20 away, never to gas. For gas to set the German price at the median, ~13.2 GW must
leave the 31.7 GW coal+lignite stack in Jun–Aug — twice what any coal-availability story can supply.

**Left alone, deliberately: the coal index.** `commodities/public_sources.py` already documents that API2
is not published in the World Bank sheet and that "Coal, South African" (FOB Richards Bay) is a proxy with
its own basis to ARA. Measured, it runs **17–18 % low** in 2022 — 32.78 €/MWh_th against 39.3 (CIF NWE
marker) / 40.0 (German border price). That is a real gap, but closing it needs an ingested ARA series, not
an invented basis adjustment, and it is worth ~5 % of the summer mis-pricing.

**So the 2022 merit order is NOT fixed.** What is fixed is one unambiguous input defect found while
looking at it. The remaining German gap is a stack-size/level problem that no coal fix reaches, and it is
now the largest single unexplained term in the model.

## La disponibilite thermique voisine devient mensuelle (2026-08-13)

`participation_caps` already clamps neighbour thermal to the fleet the market revealed, but with **one
number for the whole year** — so the model may spend a November availability in June. Measured on German
coal+lignite, output in the 100 dearest hours of each year against the model's own capacity:

| year | delivered | model capacity | **phantom** | gas−coal SRMC spread |
|---|---|---|---|---|
| 2019 | 32.9 GW | 41.3 GW | **8.5 GW** | €1 |
| 2022 | 24.2 | 33.3 | **9.1** | **€184** |
| 2024 | 20.3 | 32.7 | **12.5** | €14 |
| 2025 | 20.8 | 27.7 | **6.9** | €27 |

**7–12 GW of German coal+lignite never delivers even at the 100 highest prices of the year — in every
year.** The defect is chronic; only its price consequence is not. When coal and gas sit within €1–27 of
each other, getting the marginal technology wrong costs nothing. At a €184 spread the same error is worth
−95 €/MWh. That is the whole explanation for DE_LU reading −3.0 / −4.8 / −10.0 in 2019/2024/2025 and
−95.3 in 2022: **2022 is not a broken year, it is the only year with a working test.** DE_LU's net load is
right in 2022 (model 33.6 GW against 35.1 GW implied), so this is not demand-side; and the model carries
84.8 GW of dispatchable against a net-load p95 of 54.5 GW, which is why scarcity almost never binds.

### The reveal test is the whole design

Observed output bounds AVAILABILITY only where the plant was INFRAMARGINAL — only then does "did not run"
imply "could not run". German coal was in merit for 79.7 % of May–July hours in 2022 but 12–27 % in
2019/2023/2024/2025. A naive monthly cap would therefore encode DEMAND as unavailability in the cheap
years and invent scarcity. So `blocks.monthly_avail` uses a month only when the zone's observed spot
exceeded that tech's SRMC for ≥ 50 % of its hours. The profiles show the test doing its job:

    DE_LU 2022  coal     Jan 0.84  Mar 0.75  May 0.52  Jun 0.50  Jul 0.54  Aug 0.66  Nov 0.95  Dec 0.93
                lignite  0.83-0.97, all twelve months
    DE_LU 2019  lignite  January and February only   (coal never in merit — spread €1)
    DE_LU 2024  coal     November only               (spread €14)
    gas         never, in any year

Gas never earns a shape, which is correct: it is the marginal unit, so its output is demand-determined
rather than availability-determined. The test excludes it automatically rather than by a hardcoded rule.

### Measured: the best arm of the series on both headline metrics

| arm | \|mean err\| | log_err | DE_LU 2022 | DE_LU 2022 hours >200 (obs 4642) |
|---|---|---|---|---|
| pre-`unclassified` baseline | 11.15 | 0.737 | −80.8 | 1487 |
| + per-zone availability floor | 10.45 | 0.666 | −79.7 | — |
| + lignite fuel corrected | 11.35 | 0.667 | −95.3 | 1487 |
| **+ monthly availability** | **9.96** | **0.654** | **−63.5** | **2998** |

DE_LU 2022 recovers **+31.8 €/MWh** and its scarcity-hour deficit halves. Five of eight zones improve
(FR −5.4→−2.6, BE −13.2→−9.5, DE_LU −28.3→−18.9, ES −3.4→−2.1, IT_NORTH −6.7→−6.2).

**It is neutral where it was designed to be neutral**, which is the property that distinguishes it from
every other change in this series: log_err 2019 0.345→0.344, 2024 0.816→0.819, 2025 0.793→0.793. The
reveal test simply declines to act in the years that are already right, so it cannot break them.

**The scarcity overshoot is inherited, not created.** System-wide `>200 €/MWh` goes from 1501 under target
to 3308 over, but the decomposition is entirely 2022 and mostly pre-existing: CH was already +1685 hours
over and this added **29**; IT-North was +1233 over and this added **13**. What the change actually did was
move DE_LU **1511 hours toward** its 4642-hour target while adding ~760 across FR/BE/PT. CH and IT-North
being over-scarce in 2022 while DE_LU is under-scarce is a separate, older defect — the same
"distribution too wide across zones" signature recorded elsewhere in this file.

### Scope and hazards

* **Backtest only.** `rolling/projection.py` still passes `avail_profile=None`. A shape measured on an
  observed year must not silently propagate into projected years, where the fleet is a TYNDP trajectory
  and no reveal test can be run. Carrying a seasonal maintenance shape forward is defensible and is the
  obvious follow-up, but it is a separate decision.
* **Returns `{}`, never a default.** A zone with no observed price series, or a tech with no revealing
  month, keeps flat availability — absence of evidence produces no clamp.
* `DISPATCH_MONTHLY_AVAIL=0` restores flat behaviour.
* **The low tail is untouched and still wrong**: negatives 2922 → 2797 against 6136 observed, with ES at
  5 against 803 and CH at 15 against 613. Those two zones have no negative-price mechanism at all and
  remain the largest open item after this.

## Le run-of-river suisse etait sous-declare jusqu'a la correction de flux 2025 (2026-08-13)

The ENTSO-E Swiss run-of-river series reads **1.95 / 1.85 / 2.13 / 2.27 TWh in 2019/2022/2023/2024 and
then 14.50 TWh in 2025**. Swiss reservoir moves the other way across the same boundary (13.9 → 9.9 TWh), so
the step is partly new filers and partly a reclassification between the two hydro categories. It is not
water: `entsoe_installed_capacity` is flat across it, and the pre-2025 declaration is self-refuting — 0.63
GW of nameplate against a metered series peaking at 0.98 GW.

Run-of-river is must-take, uncurtailable and peaks on snowmelt, and Swiss negative prices ARE a snowmelt
phenomenon (observed CH negatives cluster April–July, 09:00–14:00). With five sixths of the fleet missing
the zone had no surplus to price: **15 modelled negative hours against 613 observed**, and in the hours
Switzerland really did clear negative the model's median was **+22.35 €/MWh** — nowhere near its floor.
CH was not floored, it was empty.

### The anchor is the same series in the year it is complete

**A residual-based reconstruction was built first and it was wrong.** Using the zone's balance residual
(load − generation − net imports, 2.10 GW mean on 2024) looked reasonable — the residual is ROR-shaped
(monthly corr +0.778 against ROR, +0.328 against reservoir; day/night 0.93 against ROR's 1.00). But the
residual collapses from 18.5 to 5.6 TWh across the same 2024/2025 boundary, so it was tracking **reporting
completeness, not hydrology**. It produced a 3× swing between adjacent years and a 4.99 GW peak, 36 % above
anything the Swiss fleet has ever delivered. Recorded because it is a convincing wrong answer.

The 2025 feed supplies both level and envelope, and the assumption it rests on is stated rather than
assumed: **the partial filers are shape-representative.** Every partial year's normalised monthly profile
correlates **0.81–0.90** with the complete year's, and all peak in June–July and trough in February–March.
So they are a scaled-down sample of the same fleet on the same freshet, and their own year-to-year
variation carries that year's hydrology.

    reconstructed = metered × k,   k = 14.50 / 2.05 = 7.07,   capped at the 2025 p99 of 3.39 GW

The cap matters: 2024's metered peak is 0.98 GW against 0.46–0.49 GW in the other partial years, so a bare
scale factor would assert more capacity than the fleet possesses.

Added to **must-take**, not netted off load — the opposite of `gb_embedded`, deliberately. Britain's
residual is netted off demand precisely so it cannot price, because heat-led embedded CHP does not respond
to price. Swiss ROR is what spills in a Swiss surplus; netting it off load would create the surplus and
then hide it.

### Measured

| arm | \|mean err\| | log_err | negatives (obs 6136) | >200 (obs 34917) |
|---|---|---|---|---|
| previous shipped | **9.96** | 0.654 | 2797 | 38225 |
| + CH ROR | 10.50 | **0.650** | 2879 | **36956** |

CH's level improves where the repair bites — **2019 +5.8 → +2.5, 2024 +13.3 → +6.0**, pooled +5.4 → −3.3 —
and 2025 is correctly untouched because its feed is already complete. `log_err` and scarcity recall both
improve. The cost is 0.54 €/MWh of pooled mean error, and it is **almost entirely one cell: CH 2022
overshooting to −16.8 from +7.5**. Adding 1.29 GW of free must-take to a zone clearing near 280 €/MWh in
the crisis year moves it hard. **Open**: a correct repair should not move one year by 24 €/MWh, and the
untouched reservoir side of the same reclassification (13.9 → 9.9 TWh, i.e. CH's *dispatchable* stack is
also mis-stated pre-2025) is the leading suspect.

### The sweep behind "CH only"

Every (zone, sub_key) year-on-year step above 1.8× was swept and classified against declared capacity, the
zone's generation total, boundary timing, sibling offsets and reporting-hour counts. Of **51 candidate
steps**, adjudication gave: 5 confirmed real by a matching capacity move, 3 closures (PT hard coal 2021,
GB hard coal 2024, IT_CSUD waste), 3 reclassifications (CZ coal→solar 96 % mirrored, IT_CALA, DK_2), 2
physical ramps, 1 coverage loss (IT_CNOR fossil oil, reporting hours 8039 → 114), 1 data step at 1 January
(**FR hard coal 2022→23**, capacity flat — France is scored, and FR coal *also* steps ×7.76 in 2024→25 with
capacity flat, so the French coal series moves twice without its fleet changing), and 16 unresolved.

**There is no general feed-completeness epidemic.** Only 6 of the 26 unresolved sit in scored zones and 4
of those are sub-TWh series that are never marginal. The 16 unresolved mostly show `jump ≈ 1.0` and no
ramp, no sibling mirror — ordinary variation in small plant that the 1.8× threshold was too tight for, so
the sweep's false-positive rate at that threshold is high. That is why this ships as a targeted repair
keyed on `_COVERAGE_FIXED` rather than as a general coverage-factor mechanism. `scratchpad/` holds the
sweep and the adjudicator.

## Le rung RES a exactement zero est un puits de curtailment gratuit — MESURE, TENU (2026-08-13)

`res_schemes._zone_tranches` now reprices any scheme tranche recorded at exactly 0.00 to −0.01, **behind
`DISPATCH_NO_ZERO_RES_BID`, default OFF**. The mechanism is real: the LP curtails the least-negative
tranche first, so a rung at exactly zero is marginal for every surplus that fits inside it and pins the
dual at exactly 0.000. Spain's merchant rung holds 38 % of RES at 0.00 against rungs of −1.01/−5.80 below,
so the surplus fitted inside the sink in 91–94 % of hours: **1513 pooled hours at exactly zero and zero
hours below −0.05**, against 803 observed negatives. It leaked into Portugal, whose 186 "negatives" were
all exactly −0.001 — the `_EPS_FLOW` wheeling penalty on Spain's zero. This repo already fixed it for
France (`mer_bid = −0.01`, with the same rationale) and the fix never reached the other zones.

**Held anyway, because it moves nothing economic.** With CH ROR in place, enabling it leaves `|mean err|`
at 10.50 and `log_err` at 0.650 — identical to three digits — while adding 2678 negative hours. It flips
signs at 0.01 €/MWh. It also buys false positives: **ES 1224 against 803 observed, PT 859 against 396,
IT_NORTH 100 against 0**, while FR (549/893), DE_LU (667/1288) and CH (190/613) still undershoot — the
right total from the wrong parts.

**What it exposed is the real defect: the ladders are too coarse to have a negative-price distribution.**
The model can only print AT a rung, and the observed negative mass falls between them:

| zone | rungs | observed negatives strictly between rungs |
|---|---|---|
| ES 2024 | [−1.01, 0.00] | 220/247 (89 %) |
| CH | [−50.0, 0.00] | 264/292 (90 %) |
| FR | [−40, −5, 0] | 211/352 (60 %) |
| DE_LU | [−300, −20, −2] | 142/457 (31 %) |

DE_LU has the finest ladder and is the only zone whose median negative depth is right (−2.00 against
−2.01). Depth, not sign, is what the markup layer and the projection consume. Ship this together with a
depth fix — sub-tranche interpolation *within* the existing endpoints, which uses no observed-price
information — rather than alone.

## Contrainte jointe de transfert par zone — ESSAYÉE, MESURÉE, REJETÉE (2026-08-15)

Four gate runs, a correct diagnosis, a physically-motivated fix, and it does not work. Recorded because
the reasoning is convincing all the way to the measurement, and the next person to reach for it deserves
the numbers rather than the argument.

### The diagnosis, which still stands

`flow_derived_ntc` bounds a zone's simultaneous export by scaling EVERY border by one coincidence factor
= p99.5(total simultaneous export) / Σ(per-border p99.5). That is a **box approximation to a simplex**: it
forbids one corridor running at its own revealed capability while the others idle, and only touches the
true constraint at a corner. The fair form of "these corridors share a limit" is a constraint on the SUM.

Measured on IT-North, whose internal NORD→CNOR corridor carries **91 % of its exports** and is
**anti-correlated (−0.081)** with the rest: own p99.5 = 4210 MW, derated to **1843**, and observed flow
exceeds that in **41.8 % of 2024 hours**. Northern Italy is not competing for one export budget — it is
**transiting**: in its top-100 export hours it imports **6470 MW** across the Alps while exporting
**4146 MW** south, and it does both at once in 71 % of all hours. Germany is the opposite and is the case
the factor was built for: every corridor positively correlated (+0.08…+0.70), Σp99.5 20.0 GW against
13.4 GW simultaneous. **The factor is right for Germany and wrong for Italy, and the correlation sign is
the discriminator.**

### The fix, and what it measured

A per-zone row in `lp.highs_solver._build`, `Σ_w f_{z→w,t} ≤ export_cap_z`, carried in `zones_data` exactly
as `energy_caps` is (so no signature changes and `multi_zone.py` untouched), with the coincidence derating
switched off underneath it. Then a symmetric import row, then a version restricted to borders with no
published NTC. All three:

| arm | \|mean err\| | log_err | IT_NORTH err | IT_NORTH log | >200 (obs 34917) |
|---|---|---|---|---|---|
| shipped | **10.50** | 0.650 | **−7.9** | 0.417 | 36956 |
| joint export only | 11.71 | 0.630 | −9.1 | 0.406 | 35558 |
| + symmetric import row | 11.71 | **0.628** | −9.1 | 0.403 | 35862 |
| + restricted to unpublished borders | 11.56 | 0.629 | **−9.1** | 0.403 | **35394** |

**The target zone gets WORSE by 1.2 €/MWh, identically in all three.** Freeing NORD→CNOR from 1843 to
4210 MW does not move Italy's price level at all, which means that corridor was never the binding
constraint on IT-North's price — despite genuinely being over-derated.

### Two explanations offered and both refuted by their own follow-up

1. *"The export row is asymmetric — turning off the derating loosened imports too."* The symmetric import
   row was built to test it and moved essentially nothing (11.85 → 11.71, log_err 0.630 → 0.628). The
   import bound almost never binds, because a zone's imports are already limited by its neighbours' export
   rows.
2. *"The damage comes from the eleven interior borders where the factor was doing real work."* Restricting
   the rows to unpublished borders was built to test it and also moved essentially nothing (11.71 → 11.56,
   log_err 0.628 → 0.629). Whatever degrades levels here is **not scope-dependent**.

A third claim died earlier in the same investigation: IT-North's Alpine borders were never affected at
all. `hourly_ntc` overrides the derived scalar wherever ENTSO-E publishes a day-ahead series, so
IT_NORTH↔CH stayed 1810/2748 and IT_NORTH↔FR 1995/2622 in every arm. Only ONE IT-North border ever moved.

### What is worth keeping

* **`flow_derived_ntc` takes p99.5 of PHYSICAL flow as a COMMERCIAL limit.** CH→IT's published day-ahead
  median is 2738 against a physical p99.5 of 4210. Physical flow contains loop flows and transit that no
  participant can schedule, so the derived scalar overstates commercial capability wherever it is used —
  and the coincidence factor has been partly compensating for that overstatement, which is why removing it
  loosens so much so widely. **This is a defect in the derivation itself and is independent of how the
  simultaneity is expressed.**
* The joint rows do buy a small real gain in coupling SHAPE — pooled log_err 0.650 → 0.629, better in 6 of
  8 zones, and 2024 improves on both metrics (8.44 → 7.74 and 0.808 → 0.791) — at a mean-error cost
  concentrated in 2022 (23.36 → 27.32) and 2025 (8.55 → 9.93). Not enough to ship, but the mechanism is
  sound and is preserved behind `DISPATCH_JOINT_EXPORT` (default off, 13 tests) should the level problem
  ever be understood.
* **IT-North's real error is most likely its STACK, not its borders.** The workflow that opened this line
  measured `EFF_RANGE["gas"]` topping at 0.58 against an Italian revealed marginal heat rate of 0.45–0.53,
  so roughly 6 of 18 GW of Italian gas is offered 13–18 €/MWh too cheap. That is the lead to pick up next,
  and it is a different file.

### The methodological note, which is the expensive part

Three of this investigation's mechanisms were diagnosed correctly and none of the fixes helped. The
pattern across this whole series is now unmistakable: **changes that improve the SHAPE of the price
distribution keep degrading its LEVEL**, and it has happened with the unclassified must-run half, the
lignite fuel correction, the CH run-of-river repair and all three joint-transfer arms. That is not four
coincidences. It says the model carries a systematic level bias that shape-improving changes expose rather
than cause, and that chasing it one mechanism at a time will keep producing this result. Finding it is
worth more than the next border fix.

## Capacité de zone de réglage allouée aux zones de marché — CONSTRUITE, MESURÉE, DÉSACTIVÉE (2026-08-16)

### The data gap is real and is now closed

`entsoe_installed_capacity` held **no rows for any Italian bidding zone**, so every Italian technology fell
through `build_neighbour_stack`'s p99.9-of-generation fallback. That was not a missing ingestion: all seven
IT zones had been requested and ENTSO-E returned `nodata` for each. Verified directly against the API —
`IT` returns 18 technologies and 97.5 GW for 2024, while `IT_NORD` and `IT_CNOR` raise
`NoMatchingDataError`. **Italy publishes installed capacity at control-area level only**, while its market
is split into seven bidding zones.

**161 rows of Italian control-area capacity (2018–2026) were ingested** and are now in the lake under
`series_key='IT'`. The series is coherent and matches known history: gas 39.1 → 48.5 GW, hard coal 6.0 →
4.7, solar 4.7 → 11.9 with the post-2024 acceleration, `Energy storage` first appearing in 2026. **That
ingestion stands regardless of everything below** — it is a fact the lake now carries.

Two findings from the same probe, both worth recording so nobody re-runs it:

* **GB raises `NoMatchingDataError` at every level.** ENTSO-E publishes no British installed capacity at
  all (12 rows in the lake, 2 chunks logged `nodata`). Not fixable by ingestion; GB's data legitimately
  comes from Elexon, which the model already uses.
* **CH returns only four technologies** (hydro ×3 + nuclear, 16.1 GW). That is precisely why Swiss solar
  and wind are absent, independently confirming the `TYNDP_SOURCES.md` note, and it is the same root cause
  as the run-of-river gap `io.ch_hydro` repairs.

### The allocation, and why it is off

`io/area_capacity.py` splits the control-area nameplate across bidding zones by **each zone's share of
observed generation per technology** — so the total is externally anchored and only the split is inferred,
which is strictly better than the status quo where both came from the same quantile.

**It validates geographically**, which is the check that it is not merely reproducing the proxy it
replaces. The 2024 shares put hard coal 63 % Sardinia / 30 % IT_CNOR / 1 % north (Fiume Santo and
Torrevaldaliga; the northern plants had effectively stopped), geothermal 100 % IT_CNOR (Larderello),
reservoir 92 % north and run-of-river 88 % north (the Alpine fleet), oil 50 % IT_CNOR + 41 % Sicily. Every
one is where the plant physically is.

It also **corrected a hypothesis recorded in this file's own investigation**: IT-North's 0.00 GW of
modelled hard coal looked like a defect and is not one. Italian coal is southern and insular; the north
really does have almost none. The quantile was right.

Effect on the stack (IT-North 2024, dispatchable 22.33 → **29.57 GW**), concentrated exactly where a
generation quantile under-reads energy-limited plant: reservoir 1.54 → 4.18, PSP 2.52 → 4.66, gas
18.05 → 20.39 (after its 0.90 derating), coal 0.00 → 0.05.

**And the gate says no:**

| arm | \|mean err\| | log_err |
|---|---|---|
| shipped | **10.50** | **0.650** |
| + allocation | 12.01 | 0.663 |

IT-North goes −7.9 → **−15.5** and every zone drifts cheaper. The per-year split is the diagnosis:

    IT_NORTH   2019  -2.1 -> -13.7   log 0.200 -> 0.532
               2022  -3.0 -> -22.4   log 0.297 -> 0.290
               2024 -14.6 -> -15.1   log 0.584 -> 0.559
               2025 -11.8 -> -10.9   log 0.589 -> 0.466   <- better on BOTH

**2024 and 2025 improve; 2019 and 2022 collapse.** The key's weakness is the cause: a zone gets none of a
technology it did not generate, and in 2019/2022 Italian plant ran in different places, so the allocation
hands IT-North capacity its dispatch of those years cannot justify — in the crisis year above all, which
the model already struggles with.

Left **off** (`DISPATCH_AREA_CAPACITY`, 7 tests). What is demonstrably right is narrower than what was
built: the ingested data, the geographic validation, and the 2025 result. A better key would split by
something that does not vanish when a plant is idle — per-zone REMIT nominal capacity would qualify, except
that REMIT files the same Italian plant at both production-unit and generation-unit level with different
EICs (35.1 GW of "IT-North gas" against a real ~18), and only 21 of 45 coded units have a capacity twin
among the clean names, so it needs the unit hierarchy resolved first.

## Le markup refité sur le SMC courant, et sa force choisie hors échantillon (2026-08-16)

### The gate does not score what step vii ships

`apply_markup` is imported by `rolling/projection.py` and **not** by `rolling/backtest.py`. So every
`mean_err` in this file compares **raw SMC against post-markup observed spot** — and the model is *designed*
to sit below spot, with the wedge as the bridge. `STEP_VII_METHODOLOGY.md` states the trade explicitly:
*"The markup can correct a level bias… but it cannot invent shape. So every structural driver of shape had
to be fixed inside the dispatch first."*

That reframes this whole series. The recurring pattern — a change improves `log_err` and worsens
`|mean err|` — is **the architecture working**, not a defect, and the methodological note in `6db9cf2`
overstates it. `|mean err|` on raw SMC is close to the wrong yardstick for judging a dispatch fix.

### The shipped wedge was stale, and it was applied at twice a defensible strength

`reports/markup_model.json` was fitted on 2019/2022/2023 against an SMC predating this session's five
dispatch changes, so every fix that raised the SMC left the wedge double-counting. Measured, it closed the
gap on its fitted years and **overshot the recent ones**: DE_LU −3 → +14, ES +7 → +20, CH +6 → +22,
FR +6 → +17. Pooled it moved `|mean err|` 10.50 → 10.31 while making `log_err` **worse**, 0.650 → 0.690.

A naive refit on the current SMC improves in-sample (10.50 → 7.80) and is **worse under leave-one-year-out**
(10.99, `log_err` 0.706 against 0.650 for no wedge at all). A wedge that memorises its training years is
worthless for 2027-46, which the projection extrapolates entirely out of sample — so LOYO, not fit quality,
is the selection criterion. Sweeping (alpha_frac, shrink) and scoring each on held-out years:

    alpha shrink   LOYO bias   LOYO MAE   LOYO RMSE
      —   (none)      -5.22      25.06       41.47   <- raw SMC, the thing to beat
     0.5    0.25      -4.57      24.80       41.14   <- selected
     0.5    0.50      -3.93      25.20       41.44   <- the SHIPPED default: worse than no wedge
     0.5    1.00      -2.65      27.20       43.35

**`shrink = 0` reproduces the raw-SMC numbers exactly**, so the parameter is "how much of the fitted wedge
to apply" — and **every setting at 0.5 or above is worse than applying none**. The old default was in that
band.

Saved: `alpha_frac=0.5, shrink=0.25`, panel 2019/2022/2024/2025 (2023 has no current gate run; 2024/2025
enter, which is the point). Nine zones now carry a wedge against seven before — GB and PT newly pass
`build_panel`'s quality gate, and CH and IT-North's 2022, which previously failed it on correlation, now
survive. **That improvement is this session's dispatch work showing up in the calibration inputs.**

### What it is worth, and what it is not

**The best projectable wedge buys ~1 % of MAE** (25.06 → 24.80). Calibration is spent as a lever; the rest
is dispatch error. But the refit is still a large fix *relative to the status quo*, because it removes the
stale wedge's damage. France, the zone that matters most here:

| FR | shipped (stale) | refit | raw SMC |
|---|---|---|---|
| 2024 bias / MAE | +16.7 / 22.2 | **+5.9 / 16.7** | +6.0 / 16.9 |
| 2025 bias / MAE | +15.3 / 23.2 | **+4.3 / 18.5** | +4.4 / 19.0 |

**France's honest accuracy for a Monte Carlo: MAE 17.6 €/MWh, bias +5.1, RMSE 23.5 — 30 % of a 59 €/MWh
level — on 2024-25, correlation 0.85-0.87.** And that is optimistic: 2019-2025 are the years every
structural fix was measured against, so they are in-sample for the *dispatch* too, and genuine 2027-46
error will exceed it. Crisis-regime draws are the weak point — 2022 sits at MAE 60 on a 276 level.

### The artifact is now tracked

`.gitignore` gains one exception, `!**/reports/markup_model.json`. It is generated, but it is not a report:
`projection.py` LOADS it, so it is an **input** to every projected year. Untracked, a fresh clone has no
wedge and `apply_markup` silently falls back to clipped SMC — the same code producing a different pipeline
per machine, and degrading *gracefully* (~1 % of MAE), which is exactly what makes the divergence easy to
miss. Every other `reports/*.json|csv|png|npz` stays ignored; verified that `git add reports/` now stages
this file and nothing else. The pre-refit model is preserved at `scratchpad/markup_model_BACKUP.json`, and
`scratchpad/refit_projectable.py` regenerates the selection from the gate parquets in ~15 minutes.

---

## The first 20-year projection run, and the two zones whose prices it cannot be used for (2026-08)

Running the 2027–2046 horizon end to end and reading VOLUMES out of it (`DISPATCH_CAPTURE_DISPATCH`, see
METHODOLOGY.md) surfaced two zone-level failures that no price-only gate could have shown. Both are
recorded here because both look, from the price series alone, like ordinary model output.

### Portugal is capacity-inadequate in the scenario — the LP is right, the inputs are short

PT prints 15000 €/MWh (VOLL) in ~200 h of 2027 and carries 6.4 GWh of unserved energy, while also showing
1947 negative hours: scarcity and glut in the same year. The stack is the explanation — **7.3 GW of firm
capacity** (gas 4.0, hydro reservoir 1.6, DSR 1.3, biomass 0.5) against a **15.3 GW peak**, with RES
peaking at 14.4 GW and a single Spanish link.

The first reading of this was that the link was under-used — PT at VOLL while ES sat at 134 €/MWh, with the
flow at 2.0 GW against a 4.3 GW annual maximum. **That reading was wrong**, and the way it was wrong is
worth keeping: it compared the flow to the border's ANNUAL maximum instead of to that hour's own published
NTC. Measured against the hourly cap, the link is at **100.0 % of capacity in all 197 scarce hours** —
mean utilisation 1.000, minimum 1.000. The LP is doing exactly the right thing.

The gap is in the scenario, not the code: TYNDP `cap_flex_gw` is **missing for PT**, so no battery or DSR
build-out is applied to a zone whose entire adequacy problem is flexible capacity. The same field is
missing for GB, NL, DK, PL_CZ and AT_SI, which biases scarcity up and negative hours down in all six.

**PT volumes are usable; PT prices are not.**

### Great Britain's markup coefficient is out of family by a factor of twenty

GB projects a 30.4 €/MWh annual mean spot against a 54.9 €/MWh SMC — the wedge subtracts 24.5 €/MWh on
average and ranges −119 to +47. The cause is in the fitted model, not the dispatch: GB carries
**β = −27.41** on one standardised feature where every other zone is under 2 in absolute value (NL −1.28,
PT −1.14, IT-North +0.23), and GB's SMC feature was standardised with **σ = 753 €/MWh**, which is 2022
dominating its panel.

This is a direct consequence of the refit above: GB and PT are the two zones that newly passed
`build_panel`'s quality bar, and the LOYO selection that chose `shrink=0.25` was scored **pooled across
zones**, so a single zone's pathological coefficient could not show up in it. The selection criterion needs
a per-zone extrapolation-safety check, not only a pooled out-of-sample error.

An independent confirmation falls out of the congestion-rent table: on the FR–GB border the markup inverts
the price order relative to the LP's flow in **3895 of 8736 hours** (45 %), against 535–1113 on France's
other five borders, and the border's annual rent drops from 508.5 M€ on SMC to 160.9 M€ on spot.

**Use GB's SMC column. GB spot is not usable.** Not fixed here — changing the markup artifact is a
behavioural change to a tracked projection input and belongs in its own measured, gated change.

### What the run does support

France's 2027 numbers are internally coherent and behave the way the underlying physics should: RES
captures **0.68** of the zone mean — the cannibalisation ratio, and close to the 0.676 observed for FR
solar in 2024 — gas captures 2.72 as a peaker should, and nuclear 1.28. Congestion rent on SMC is
non-negative in every hour on every border, which is the sign convention checking itself.

---

## The GB markup coefficient, fixed — and it was never really a coefficient (2026-08)

The entry above called GB's `beta = -27.41` an out-of-family coefficient and proposed a per-zone
extrapolation-safety check. That diagnosis was **half right and pointed at the wrong layer.** Chasing it
found two defects, neither of which is about coefficient magnitude, and a per-zone `|beta|` cap would have
suppressed the symptom while leaving both causes in place.

### Cause 1 — the panel was being fed a failed dispatch

GB's backtested SMC reaches the LP's 15000 €/MWh VOLL in **89–155 hours of every fitted year**, against an
observed GB that never exceeded 1860. Modelled standard deviation vs observed: **5.5× (2022), 13.4× (2024),
25.8× (2025)**, and 2.55× even in the quiet 2019. `build_panel`'s quality gate never saw it, because both
its tests are blind to tails — a median does not move for 100 hours out of 8735, and the correlation bar is
0.2 while GB scores 0.28–0.42.

So the regression was handed a zone whose price error is a **capacity failure**, and spent its entire
coefficient budget trying to wedge away a 15000 €/MWh dispatch defect. That is not something a wedge can
repair, and the docstring already said so: *"a zone-year the dispatch prices badly is a failed dispatch,
not a wedge the markup should learn."* The gate simply did not test for this way of pricing badly.

Added criterion (c): `max_std_ratio`, default 2.0. It excludes GB in all four years and NL 2024, and
**nothing else** — the largest surviving ratio is PT 2022 at 1.32, so the threshold sits in a wide empty
gap rather than being tuned to a target. NL 2024 is an independent confirmation: it is the same zone that
ran away to 1522 €/MWh by 2046 in the projection.

### Cause 2 — the sign constraints were one-sided across a collinear pair

`_SIGN_LB` bounded `smc`, `tight` and `peak_kink` at zero and left `tight_sq` free. But `tight` and
`tight_sq` are **0.96–0.99 collinear** within any one zone's range, so bounding one of them does not bound
the tightness *response*: the solver satisfies `tight ≥ 0` by pinning it to exactly 0 and putting the
negative response on the free partner. **Five of nine zones did exactly this** — BE −0.46, CH −0.53,
PT −1.14, NL −1.28, GB −27.41 — every one of them with `tight` sitting precisely on its bound.

GB's fitted wedge therefore ran from **+19 €/MWh at `tight=0` to −115 at `tight=0.68`**: strictly
decreasing in tightness, which is the precise behaviour the constraint block's own comment says it exists
to forbid ("non-decreasing in the price level and in tightness"). GB was the loudest case, not a special
one.

### Why the first fix alone was not enough

Constraining `tight_sq` without the panel gate leaves GB's wedge still falling (+9 → −39) and `max|beta|`
at 14.3 — the response just moves again. **Both causes had to go.** This is why the "cap the coefficient"
instinct from the previous entry was wrong: the magnitude was a symptom of a mis-specified constraint set
operating on unusable data.

### The measured result

Re-selected on the repaired panel with **every zone scored** (the previous sweep scored eight and fitted
nine — a zone that is fitted but never scored cannot be caught by any criterion, which is the root cause
behind both of the above). Old configuration vs new, each at its own best setting, leave-one-year-out:

| | LOYO MAE | RMSE | bias | worst \|beta\| |
|---|---|---|---|---|
| old | 30.49 | 66.00 | −5.12 | 13.26 (GB) |
| new (`alpha_frac=0.1, shrink=0.25`) | **29.58** | **65.30** | **−4.37** | **5.04** (DE_LU) |

Per zone, all improving except PT (+0.05, noise): **GB −5.53**, NL −0.91, ES −0.30, FR −0.36, BE −0.35,
IT-North −0.32, CH −0.29, DE-LU −0.20.

One detail worth keeping: the old arm's *own* best LOYO setting is `alpha=2.0`, not the shipped `0.5`.
Scoring GB would have pushed the old configuration toward heavier regularisation on its own — the missing
evaluation zone is what selected the setting that let 27.41 through, more than the constraint set did.

In projection, GB's applied wedge goes from **−24.5 €/MWh to exactly 0**: it is out of the panel, so it
projects on clipped SMC. For a zone whose dispatch runs out of capacity 100+ hours a year, "no wedge" is
the honest answer, not a defeat. NL's flips from −3.0 to +1.4. France moves −0.60 → −0.76, i.e. not at all.

Honest limit on GB by year: the repair wins through 2019 (MAE 38.6 → 15.0 — **the shipped wedge was making
2019 worse than raw SMC**) and loses 1–6 MAE in 2022/2024/2025, netting −5.5. The wedge it removes was
buying in-sample level correction on three years by memorising a dispatch failure; LOYO says that does not
transfer, and 2027-46 is entirely out of sample.

The pre-repair artifact is preserved at `scratchpad/markup_model_PRE_GBFIX.json`;
`scratchpad/refit_gbfix.py` regenerates the selection and `scratchpad/loyo_old_vs_new.py` the A/B above.

---

## The flexibility bid is the price level, and it is pinned twice at 300 €/MWh (2026-08)

Once `cap_flex_gw` closes adequacy in every zone, the flex block stops being a backstop and becomes the
marginal unit. Measured on FR 2046: the SMC takes four values, and **300.0 — `VOM["flex"]`, exactly — is
3140 of 8640 hours (36 %), carrying 71 % of the annual mean.** The others are −0.0 (RES surplus, 22 %),
7.0 (nuclear, 16 %) and 201.0 (gas, 6 %).

Those hours are **nightly, not seasonal**: hour-of-day peaks at 23:00 (257 h) and is empty 10:00–14:00
(1–3 h), with RES at 18.2 GW against 48.0 GW in the cheapest half of the year. This is why the RES
trajectory is the wrong lever on the LEVEL — scaling FR RES to the PPE3 high end (+27 %) removes 121 of
those 3129 hours, and doubling it leaves 1476. Solar cannot clear a 22:00 price. The RES range moves the
cheap tail (negative hours, capture rates, RES revenue), not the mean.

### The sensitivity, measured

A full 300 → 180 arm (`DISPATCH_FLEX_VOM`, 20 years, same workbook and draw):

| FR mean spot | 2030 | 2035 | 2040 | 2046 |
|---|---|---|---|---|
| VOM 300 | 58.4 | 85.0 | 114.6 | 159.5 |
| VOM 180 | 56.2 | 78.6 | 103.0 | **133.4** |
| Δ | −2.2 | −6.4 | −11.6 | **−26.0** |

**−26.0, not the −43.5 a back-of-envelope gives** (3129 h × 120 / 8640). The arithmetic fails because the
affected hours split three ways, and only one of the three reprices by the full 120:

| the 3140 base hours at 300 became | hours | share | why |
|---|---|---|---|
| **300.0, unchanged** | 1324 | 42 % | **DSR's first rung is ALSO exactly 300** |
| 180.0 | 879 | 28 % | flex still marginal, now cheaper |
| 201.0 | 543 | 17 % | flex fell below gas; gas is marginal |

**Two independent scarcity parameters sit at the same number.** `_DSR = [(0.03, 300.0), (0.03, 1000.0),
(0.05, 4000.0)]` in `rolling/windows.py` puts its cheapest demand-response tranche at exactly `VOM["flex"]`.
Lower one alone and DSR simply inherits the margin at the same price in 42 % of the hours. Anyone trying to
move the model's scarcity price must move **both**, and the coincidence is undocumented on either side.

Confirmed directly: in the 1327 hours that stayed at 300, flex runs 21.6 GW (near its cap) and DSR
dispatches 0.84 GW — DSR is the marginal unit.

### It is a DISPATCH parameter, not only a price parameter

The larger surprise. FR 2046 volumes and revenue:

| tech | TWh @300 | TWh @180 | revenue @300 | @180 | Δ M€ |
|---|---|---|---|---|---|
| flex | 22.1 | **68.8** | 7412 | 17762 | **+10350** |
| nuclear | 298.7 | 298.6 | 61651 | 51716 | −9935 |
| hydro reservoir | 44.0 | **30.9** | 11789 | 7089 | −4699 |
| res | 299.9 | 299.9 | 29764 | 24828 | −4936 |
| oil | 8.1 | **3.7** | 2560 | 1183 | −1377 |
| dsr | 7.2 | **1.3** | 2412 | 580 | −1832 |

At 300 the block is a peaking backstop (22 TWh); at 180 it is a **mid-merit resource running 3× the
energy** (69 TWh), displacing 13 TWh of hydro reservoir, 4.4 of oil and 5.9 of DSR. Total French generator
revenue falls **14.1 bn€, −11 %**, on one parameter that `stacks/costs.py` documents as a default rather
than a revealed-behaviour measurement.

Negative hours are untouched (1227 in both arms) and the median barely moves (146.2 → 146.1): the whole
effect sits in the upper-middle of the distribution.

Every zone moves the same way, France most — 2046 Δ: FR −26.0, CH −8.6, IT-North −7.8, BE −6.0, ES −5.6,
GB −3.5, PT −3.8, DE_LU −2.5, NL −1.9, DK −1.2, PL_CZ and AT_SI ~0.

**Nothing is changed by default.** `flex_vom()` reads `DISPATCH_FLEX_VOM` at call time and falls back to
`VOM["flex"] = 300`, so the shipped model is unchanged; `scratchpad/cmp_flexvom.py` reproduces the table.

---

## The flexibility module is switched ON, and the flex block is split into real storage + a DSR/H2 rump (2026-08)

Three changes, one scenario decision.

### 1. `flexibility.enabled: false → true`

The storage machinery — `flexibility/storage.py`, SoC-constrained PSP and BESS — is gated behind this
flag, and the flag was off. So for the whole 2027-46 horizon **there was no storage in the LP at all**:
no batteries, and no pumped storage either (France's 3.3 GW of PSP simply absent). The dispatch capture
confirms it directly: no `storage_*` and no `hydro_psp` columns in any projected year before this change.

`config.yaml` had a test asserting it ships off ("byte-identical default, golden preserved"). That test is
now inverted, and the guarantee it protected is deliberately gone. **`rolling/backtest.py` reads the same
flag**, so this changes the backtest too — and `reports/markup_model.json` was fitted on flat-LP backtest
SMC. The wedge is small (shrink 0.25, mean 1-5 €/MWh in most zones) next to what the module itself moves,
but the calibration and the dispatch are no longer the same model. Re-deriving the panel under flex is
outstanding.

Cost: ~12 s per weekly window against ~1 s flat, so a 20-year horizon runs in hours rather than minutes.

### 2. The flex block was never a battery, and now it isn't asked to be

`cap_flex_gw` is documented as "battery + demand-response + H2-peaker" but entered the LP as ONE
thermal-like unit with a flat bid: no state of charge, no charging leg, no round-trip loss, **no energy
limit**. Its energy was free and unbounded. Measured on FR 2046 at a 180 €/MWh bid it delivered **68.8 TWh
from 24 GW — 2866 full-load hours, roughly double a 4-hour battery at one cycle a day**.

The workbook now carries `cap_bess_gw` beside `cap_flex_gw`; `storage_spec`/`bess_power_mw` read it and
`_append_flex` shrinks the flat block by exactly that much (the anti-double-count hook already existed —
it had simply never run). France 2050: **28 GW total = 17.5 BESS + 10.5 DSR/H2**, a 62.5 % battery share
applied to every zone at every anchor. With storage live, FR 2046 discharges 26.9 TWh from 18.3 GW =
**1471 FLH, almost exactly one cycle a day**, charging 31.6 TWh at an 0.853 round-trip.

`_BESS_PROJ_DURATION_H = 4.0` is the parameter that decides whether a stated GW figure covers an evening
peak: 15 GW is 22.5 GWh at the 2024 seeds' 1.5 h and 60 GWh at 4 h. New grid-scale build is procured at
4 h; the seeds describe what was built by 2024.

`VOM["flex"]` returns to **180**, and now prices only what is left in the block — demand response and H2
peakers. 180 is an H2 OCGT at ~2 €/kg and ~40 % efficiency; published H2-peaker SRMC spans ~150 (2 €/kg)
to ~300 (4 €/kg), with one published scenario at 284.

### 3. The DSR ladder, 300/1000/4000 → 180/250/500

With the module on, FR 2046 sat at **exactly 4000 €/MWh for 170 hours**, which alone carried 79 €/MWh of a
221 €/MWh annual mean — with ZERO unserved energy. A ladder written to represent emergency load-shedding
was setting the average price of the year. The rungs now sit in the same register as the resources they
compete against.

**The first rung equals `VOM["flex"]` exactly, as 300 did before.** Two independent scarcity parameters
remain pinned to one number, so a sensitivity on either alone leaves ~40 % of the affected hours unmoved —
measured on the old 300 pair: of 3140 FR hours at 300, 1324 did not move when only the flex bid fell.
Move them together.

### What enabling the module does to the shape

FR 2046 SMC, flat LP vs flexibility on (before the DSR change):

| | mean | median | p90 | h ≤ 0 | h > 500 | h at 4000 |
|---|---|---|---|---|---|---|
| flat LP | 128.6 | 140.7 | 300 | 1677 | 17 | 0 |
| FLEX + BESS | **221.2** | **130.0** | **215** | 1979 | **364** | **170** |

The median FELL and p90 FELL — the middle of the distribution improves. The entire mean increase is the
scarcity tail. Nuclear-marginal hours collapse 1414 → 371 because the rigidity module bids the socle at
~45 rather than 7. This is qualitatively what a rigidity module is for; the open question is whether a
mean decided by ~2 % of hours at a ladder rung is fit for a Monte Carlo.

---

## Monte-Carlo over real weather draws — and the two bugs that made the old draw index a lie (2026-08)

The projection's `--draw N` had been threaded through demand, RES, availability and dispatch since #77/#80.
It did nothing. Two independent defects, both silent.

### 1. The realization index selected nothing

`weathergen/output/simulation.nc` has dims `(time, station, variable)` — 175 200 h, exactly the 20-year
horizon, and **no ensemble dimension**. `powersim_core.weather_cube._select_realization` looks for
`realization|member|draw|ensemble`, finds none, and falls through to `return ds`. So every draw read
identical weather, and a 50-draw Monte Carlo would have produced 50 identical weather years varying only
in stochastic outages (of which the lake held 2).

The availability model's own docstring had already recorded this — *"the 50 projection draws vary only the
stochastic outages… kept for forward-compatibility with an ensemble cube"* — so the limitation was known
on one side of the interface and not the other.

### 2. A regenerated cube would have broken step iv

`weathergen/config.yaml` pointed `simulate.wind100_model` at `models/wind100.pkl`; the file had been
renamed to `.json`. `_append_wind100` tests the RAW path for existence and returns the cube unchanged when
it is absent — so co-generation was skipped silently and the cube came out with **7 variables**.
`res_model.load_weather_synthetic` raises outright without `wind_speed_100m_ms`. The shipped cube predates
the rename, so this would only have fired the first time anyone regenerated. (`Wind100Model.load` already
normalises the suffix; only the existence check was wrong.) Config now names the real file.

### The design

Rather than build an ensemble cube (50 × 472 MB = 23 GB), each draw generates its own cube and every
consumer is redirected at it by ONE env var:

* `powersim_core.weather_cube.open_cube` honours `POWERSIM_WEATHER_CUBE`. Four configs name this file
  (demand, res, availability, dispatch); overriding the shared reader redirects all four at once, which is
  what makes a draw *coherent* rather than four unrelated randomisations.
* `effective_cube_path` fixes a companion trap: the demand and RES driver caches test freshness by
  comparing their mtime against the **configured** cube. Under an override that compares against the wrong
  file and declares a cache built from another draw's weather fresh — a wrong answer, not a slow one.
* Draw *d* runs step v on its own cube into lake layer `availability_mc`, partition `wdraw=d`, selected by
  `DISPATCH_AVAIL_WDRAW`. The canonical `availability` layer is never touched, so the single-run path is
  unchanged. Confirmed in the parallel smoke: each worker reports "1 nuclear draw(s)", not the shared 2.
  **THAT SENTENCE DOCUMENTED THE BUG AS PROOF OF HEALTH (corrected 2026-09-09).** "1 nuclear draw(s)" was
  the precise condition under which the lookup could not work: `nuclear_unavail_daily` asked for
  `frac.loc[(draw % nuc_draws, year)]`, and with one draw in the partition `draw % 1 == 0` for every
  caller, so it could only ever hit a partition whose single label was 0. Each `wdraw=d` is written with
  `draw_ids=[d]`, so the label is `d` and the lookup raised KeyError, returned None, and `fr_window`
  silently substituted the 2019 rolling-max proxy. Forty-nine of the fifty draws were priced on one
  identical 2019 nuclear shape; the ensemble had no nuclear-availability dimension at all. See the
  2026-09-09 entry below.
* The dispatch runs with `--draw d`, which keys the demand/RES driver caches (`realization=d`). Passing 0
  for every draw would make them share one cache entry and hand draw *d+1* the drivers of draw *d*
  whenever its cube predates that cache.

Nothing is shared and written, so draws parallelise. `--workers K` is bounded by RAM (a preload plus a
HiGHS model each), not cores: two concurrent preloads measured 774 s against ~500 s alone, ~65 % efficiency.

### Cost, measured

| step | per draw |
|---|---|
| weathergen simulate | ~60 s (fitted model loads once, 2.5 s) |
| step v availability | **5 s** |
| steps iii+iv | inside the preload |
| dispatch 20 y, flexibility on | ~4.4 h |

So the ensemble is dispatch-bound: N=50 is ~230 h serial, ~2.5 days at 3 workers. The driver is resumable
— a draw whose analysis workbook exists is skipped — which is a requirement, not a nicety, at that length.

### What the ensemble does and does not span

Weather only. Scenario capacities, demand trajectories, commodity prices, NTC structure and the fitted
markup are identical across draws. It is weather risk, not scenario risk, and the ENSEMBLE notice says so.

Validated end to end on two truncated draws: FR annual means differ by 32.5 €/MWh on average (inflated by
the truncation), HDD 1721 vs 1519, and each draw carries its own weather as a workbook tab.

## One central weather trajectory, and a sensitivity set that varies one assumption at a time (2026-09)

The 50-draw ensemble spans weather and nothing else — the notice in `mc50_consolidated.xlsx` says so
explicitly. The questions that follow it are all structural: what if carbon settles at 175 rather than
197.5, what if French solar under-builds by a fifth, what if the reactors are not extended past sixty
years. Those cannot be read off the ensemble, because in the ensemble every draw carries the same
assumptions.

A sensitivity therefore needs the opposite discipline from an ensemble: hold the weather **fixed** and move
one assumption, so the difference in the answer is the assumption. That requires a weather trajectory to
hold fixed, and it should not be an arbitrary draw.

### REFERENCE_WEATHER_20Y — the most typical year, twenty times

For each projected year the ensemble gives 50 French annual mean spot prices. Take, per year, the draw
whose price sits closest to the ensemble mean, and take that draw's weather **for that year**. Twenty
years, sixteen distinct draws, worst deviation 0.467 €/MWh and most under 0.10 — so every year of this
trajectory prices at essentially the ensemble mean for that year, which no single draw does (draw 0, for
one, is a low outlier in half the horizon).

The cubes had been deleted after each draw, 854 MB apiece. `generate_cube` seeds
`SeedSequence([master_seed, draw])`, so the sixteen needed were regenerated exactly, one at a time, and
deleted again once their year had been copied out — 852 MB of assembled output through a disk memmap,
because the machine has about 2 GB free and the array is 852 MB.

**The seam is real and is corrected to first order.** Consecutive years come from different simulations, so
31 December and 1 January are unrelated draws. Per station and variable the step is halved into each side
and tapered linearly to zero over 72 hours; bounded variables are re-clipped afterwards. What that removes
is the discontinuity, not the independence: a storm that ends where a calm begins still ends there.

**What this trajectory is not.** It is not a physically sampled twenty-year sequence. Year-to-year
persistence — a dry winter after a dry autumn, reservoir carry-over across a New Year — is broken by
construction. Anything that depends on multi-year weather memory must be read from the ensemble.

### `POWERSIM_SCENARIOS` — one switch for four models

Four configs name `../scenarios.xlsx` (demand, RES, availability, dispatch) and every one of them reads it
through `powersim_core.scenario`. So the redirect goes there, in `resolve()`, and one environment variable
moves all four at once — for the process that sets it and no other, which is what lets three variants run
side by side on three different workbooks with the reference file untouched. Read at call time, never
cached at import, for the same reason every other flag in this repo is: a module-level constant would
freeze whichever variant imported first.

A named-but-missing file raises. A sensitivity that quietly reruns the base case is indistinguishable from
one that legitimately moved nothing, and that is the worst failure this set can have.

### Where the line falls between a cell and a flag

Fifteen of the nineteen sensitivities are workbook edits, each on exactly one tab (verified by diffing
every variant against the reference: one tab changed, all others byte-identical). Four are not, and the
reason is that the cell that looks right is not read:

* **FR demand.** `dispatch_tyndp` carries `demand_twh` for France, `zfac("FR")` reads it — and with
  `weather_coherent: true` it is never applied, because FR load is the step-iii projection carried on the
  draw's weather. Editing that cell moves nothing while looking authoritative. `DISPATCH_DEMAND_SCALE`
  scales the projected load instead. The structural tabs *do* drive step iii, but "FR demand 10 % lower"
  is not any one of them — reproducing it there means choosing which of electrification, efficiency and
  datacentres gives way, which is a different question.
* **FR nuclear availability.** It comes from the step-v lake, not from a cell. `DISPATCH_FR_NUC_AVAIL_SCALE`
  multiplies the availability *fraction*, so the derate follows the shape: a fleet already 40 % out for
  maintenance in May loses less absolute MW than one at full availability in January, which is how a worse
  forced-outage rate actually falls. Relative, not percentage points — the same reading as "solar −10 %".

### The French demand sensitivities, and the 160 TWh that made them proportional

The request was "demand rising to only 700 TWh by end of period instead of 740-750". Measuring first turned
that premise over. Step iii, on REFERENCE_WEATHER_20Y at realization 0, projects **556 TWh in 2046** — not
740. The workbook's own `dispatch_tyndp` `demand_twh` row for FR does say 540 / 616 / 680 / 740 at
2030 / 2035 / 2040 / 2050, but that row is read for France and never applied, so the scenario's stated
demand has never touched a French price. The engine's path is the one that prices, and it runs
−63 / −105 / −137 / −160 TWh under the scenario the workbook asserts. The demand tabs are byte-identical to
git HEAD, so this is not a stray edit; it is how the model has been pricing all along, including through the
50-draw ensemble. Diagnosis is a separate piece of work.

**Diagnosed on 2026-09-14**, surfaced by the golden recapture (`REVIEW.md` §13). The gap is the workbook, not the
engine.
- **The commit:** aba6a5d (2026-08-20), whose message is about LP volumes and flexibility, carried a growth
  moderation of four demand tabs: `demand_macro`, `demand_mobility`, `demand_new_large_loads` and
  `demand_residential_tertiary`.
- **How it was made:** `dispatch_model/scratchpad/solve_balanced.py` solved it on 2026-08-17 to reach 590 TWh in
  2050. Each driver's growth from its 2025 value is scaled by a factor λ:
  - λ = 0.456587 for GDP, steel, chemicals, cement, data centres and electrolysis, set for GDP +1.1 %/yr;
  - λ = 0.518555 for heat pumps and the EV fleets.
- **Where it shows:** those four tabs changed in aba6a5d and are identical from there to the working tree. On the
  12 August tabs (8be1a62, PPE3) the same engine gives 760 TWh in 2046, against 567 on today's.

So "how the model has been pricing all along" means since 20 August, and the choice is only now documented.

**Kept as the reference (decision, 2026-09-14).** The 760 TWh path of 8be1a62 is PPE3's HIGH case, not a
central scenario; the 590 TWh trajectory is the more credible reference, and the new ensemble prices it.

Against 556, an absolute 700 TWh target would have been a **26 % increase** — the opposite sign of the
sensitivity asked for. So the two variants are built as the PROPORTIONAL cuts that 700 and 650 represent
against the 745 the request was framed on (0.940 and 0.873), transposed onto the measured path: 523 and
485 TWh in 2046, with growth held to 69 % and 34 % of the reference's. Both labels carry both numbers,
because a variant called "700 TWh" that lands at 523 would be the same class of quiet lie this whole set
exists to avoid.

The third demand variant needed no such transposition, because it names a physical quantity rather than a
total: electrolysis capacity frozen at 2.0 GW from 2030 instead of 2.3 rising to 4.8. The capacity is
frozen and the load factor is not — a stalled build-out says nothing about how hard the units that were
built are run — so the frozen fleet still consumes more each year. Measured end to end at the demand level,
the workbook edit propagates exactly: −1.1 TWh in 2030, −8.1 in 2035, −14.4 in 2046, matching
capacity × load-factor × 8760 in every year of the horizon.

Two conventions are choices, not deductions, and are stated as such in `make_sensitivities.py`: commodity
sensitivities move the **long-run anchors only** (≥ 2031; 2027-2030 are quoted forwards, so "CO₂ 175"
means the 197.5 €/t plateau becomes 175 with the interpolation from 2030 following), and RES capacity moves
the **projected years only** (≥ 2027; 2025-2026 are built or building and sit outside the horizon).

One sensitivity cannot be run as named. "Non-FR onshore wind +10 %" has no onshore/offshore split to move:
the neighbour model carries a single `cap_wind_gw` aggregate, so `s16` is +10 % on all neighbour wind, and
the manifest says so.

### What is shared, and why it is computed before any variant starts

Three artefacts are weather-only and cost minutes each: `demand/projection_features`, `res/proj_drivers`
(both keyed by realization, both invalidated by the cube's mtime) and the step-v availability partition.
Computed inside the variants they would be computed by three workers at once on the same lake path, and
recomputed by every variant that started before the first finished. The runner computes them once, alone,
before any variant starts; the variants only read.

The availability partition is `availability_mc/wdraw=900`, deliberately outside the archived ensemble's
0-49. The sensitivities are not draw 0 of anything, and writing over that partition would silently
redefine an archived result. Setting `DISPATCH_AVAIL_WDRAW` before the runner starts also makes
`run_projection_20y` skip its own step-v block — the same deference that makes it a no-op under
`run_montecarlo`.

### Resumability, because this is designed to be killed every morning

Two levels, the same as the ensemble: a variant whose `.sens_complete` marker exists is skipped whole, and
inside a variant any year whose `prices_<y>.parquet` exists is skipped — that file is written LAST and
atomically, so its presence means the year's dispatch and flows landed too. `sens_night.ps1` carries every
defence `mc_night.ps1` earned: kill the process **tree** before the task's own limit fires (an execution
limit kills the wrapper and leaves the pool workers orphaned, which cost a full night at 35.6 GB of commit
against 15.7 GB of RAM), a deadline strictly ahead of now (a catch-up at 09:26 once resolved its deadline
to that same morning and killed the run in the same second), and no daytime catch-up at all.

## Le nucléaire français n'a jamais varié d'un tirage à l'autre : quatre défauts empilés dans la chaîne de disponibilité (2026-09)

The 50-draw ensemble was built to span weather risk. It also claimed to span nuclear availability — each
draw projecting step v onto its own weather. It did not. Forty-nine of the fifty draws were priced on one
identical 2019 nuclear shape, and the four defects that made that possible had all been reported as working.

### D1 — a count used as an identifier, failing silently into a proxy

`nuclear_unavail_daily` resolved the draw as `frac.loc[(draw % nuc_draws, target_year)]`. `nuc_draws` is a
COUNT; the index carries IDENTIFIERS. Every `availability_mc/wdraw=d` is written by
`project(n_draws=1, draw_ids=[d])`, so it holds one series labelled `d` and `nuc_draws == 1` — making
`draw % nuc_draws` the constant 0 for every caller. The lookup could only ever hit a partition whose single
label happened to be 0. Everywhere else it raised `KeyError`, returned `None`, and `fr_window` substituted
the reference-year rolling-max-of-output proxy without a word.

Only draw 0 resolved, and only by coincidence. It sits **11.53 €/MWh below the ensemble mean** (mean z
−2.27, rank 1 of 50 in eleven of twenty years) — an anomaly noted weeks earlier as "a low outlier with no
weather explanation". This was the explanation.

**Four layers of silence stacked**, and the runs printed reassurance throughout: `run_projection_20y` said
"reusing availability_mc/wdraw=13" (true — the partition existed), `_preload` said "1 nuclear draw(s)"
(true — and precisely the condition that broke the lookup), and this file recorded that second line as
positive evidence of correctness. Labels are now resolved against what the partition carries, and both the
applied path and the fallback announce themselves.

### D2 — a ratio whose denominator was its own numerator

The availability fraction was `available_mw / available_mw.groupby(draw, year).max()`. The best day of a
year is never a full-availability day, so the fraction sat **6.8 points high — about 4.3 GW of French
nuclear never removed from the dispatch stack, every hour of every year**. The tell was 2027, whose bias
was only 1.05 points against 6.5–8.0 everywhere else: `_assemble_draw` seeds every unit at full capacity
before applying outages, so year one's maximum does reach true installed. The justification recorded for
the maximum — "captures a fleet that shrinks as reactors retire" — did not hold either, since
`closure_year` was NaN for all 168 registry rows and nothing ever retired.

Step v now writes `installed_mw` beside `available_mw`, both from one `_operating_mask`, so numerator and
denominator cannot describe different fleets. A partition without the column is refused, not silently
degraded.

### D3 — a column named capacity that was not capacity

`avail_fleet_registry` carried 259 GW of French nuclear against a real ~63 GW, and the error was per-unit,
not a scale: Cattenom 1 at 32 470 MW for a 1310 MW reactor (12.5 % of fleet weight against a true 2.1 %),
while Penly 1, Civaux 1, Golfech 1 and Flamanville 3 were exactly right. A column correct for some rows and
24× wrong for others is a SUM OVER AN OBSERVED SERIES, not a nameplate. Since `_assemble_draw` seeds each
unit at `capacity_mw`, outage weight was misattributed across the fleet — Herfindahl effective unit count
31.1 against 59 reactors.

`gen_nuclear_trajectory.py` had documented this in August ("the registry's own `capacity_mw` column is not
capacity … so only the years are taken from it") and worked around it locally, without anyone propagating
the finding to step v, which consumes it as the basis of every outage.

The sheet is regenerated wholesale from `io/fleet.py::build_fleet_registry`, which was correct all along —
same 168 unit_ids in the same order, nuclear 65.76 GW, largest unit 1596 MW, and `closure_year` 2020 for
Fessenheim 1/2 where the workbook had it null for every row. `FLEET_REGISTRY` gained a 2 GW ceiling;
`Check.gt(0)` alone had let 32 470 MW validate for the project's whole life.

### D4 — a fleet that began life on the first day of the projection

Refuelling was scheduled from the horizon start, each unit's first outage drawn uniformly in
`[hstart, hstart + cycle)`, so on 1 January of year one **no reactor was mid-outage**. Measured across every
partition, year one ran 0.8 % to 9.7 % more available than that draw's own remaining nineteen years — about
3.8 GW of phantom nuclear in the highest-priced year of the horizon. It never reached a price before,
because year one was on the proxy in 49 of 50 draws; after D1 it would have. Scheduling now starts three
years before the horizon and is clipped to it, leaving units mid-cycle on day one in the proportion the
cycle implies. Year one's excess: **+8.5 % → +0.7 %**.

### The same defect shape, five more times

D3's retirement schedule exposed a family: `planned_scheduler` and `forced.py` both excluded units on
`closure_year.notna()` rather than "closed before the horizon", so the moment retirements existed,
twenty-two reactors silently stopped refuelling (fleet availability jumped 0.744 → 0.80) and would have
lost forced outages entirely. Three Kd diagnostics divided by a denominator that dropped the retiring
fleet, reporting **Kd 0.900 for a fleet running at 0.747**. All now use the operating window.

### Step v and the dispatch now price the same fleet

Step v modelled 59 reactors that never retire and never gain a sibling — `installed_mw` flat at 64 009 MW —
while the dispatch scaled `cap_nuclear_gw` to 56.5 GW by 2045 and folded in ~10 GW of EPR2. D2 amplified
that mismatch: the fraction fell 0.823 → 0.744, so the absolute outage MW carried onto the dispatch's fleet
grew about 45 %. The registry now carries the dispatch's own `glide_closures` retirements and the six EPR2
units, and the two fleets track to within 2 % across the horizon.

**The new units get their own outage rate, from physics rather than assertion.** Forced-outage age creep
accrued as `y - ref_year` — with the calendar, so every reactor aged from the same instant regardless of
when it was built, and a 2038 EPR2 carried ~25 years of creep by 2046 having run for eight. It now accrues
from each unit's own commissioning against the age the calibrated fleet had when calibrated. Planned
outages use palier `EPR`, Flamanville 3's class, calibrated at an 18.0-month cycle against 12.0–17.0 for
the legacy paliers. Measured over 2038–2046 on operating units: **EPR2 0.782, existing fleet 0.739**.

### What it costs, measured end to end

A full 20-year run on REFERENCE_WEATHER_20Y, step-v availability applied in 20 of 20 years with zero
fallbacks: FR mean **65.5 €/MWh** against the old ensemble's 63.4, i.e. **+2.1** — but that mean hides the
shape. By period: **−3.5 / −2.9 / +2.6 / +13.1** €/MWh for 2027-30 / 2031-35 / 2036-42 / 2043-46. The 2019
proxy was a fixed MW envelope divided by a shrinking `nuc_cap`, so its implied fraction climbed mechanically
towards 1.0 in the late horizon while the true fraction stays near 0.74. The repair also moves the model
toward the record: observed ex-crisis Kd 0.738, model was 0.765, now 0.748.

**The backtest gate is untouched.** `run_backtest` builds FR nuclear outage from REMIT, and the only step-v
consumer in the repo is `rolling/projection.py`. No backtest needed re-running.

### And a test was the producer of a production artefact

`availability_model/tests/test_projection.py` called `project(cfg, n_draws=2)`, which defaults to
`layer="availability"` — so running the suite rewrote the canonical availability tables the dispatch
consumes and `tools/golden.py` fingerprints. The layer had no provenance: it was whatever a two-draw test
fixture last wrote, on whatever cube happened to be configured. The suite is now isolated behind a session
`POWERSIM_LAKE` redirect, and the canonical layer is produced only by
`availability_model/scripts/project_canonical.py`, which stamps a `PROVENANCE.json` naming the cube, the
realization, the registry digest and the commit.

## Un nom de répertoire n'est pas une empreinte : le second ensemble perdu par réutilisation de prix périmés (2026-09-10)

The 50-draw ensemble launched on the evening of 2026-09-09 reported 50/50 complete at 07:10 the next
morning, in eleven hours. It should have taken forty-four. **Nine draws were computed; the other
forty-one reused the price parquets of the ensemble that had just been discarded as wrong.**

### The mechanism, and why the existing guard did not fire

`run_projection_20y` skips any year whose `prices_<y>.parquet` exists. That is deliberate and worth
keeping — it is what lets an ensemble survive a reboot. But *the file exists* is not *the file came from
this model*, and the two had been conflated once before: in August a fresh ensemble inherited a smoke
run's years, and the fix was to namespace the work directory on the output directory name:

```python
work = Path("scratchpad/mc") / out_root.name / f"proj_{draw:03d}"
```

with a comment stating that this "stops it leaking BETWEEN ensembles". It does not. It stops it leaking
between ensembles *with different names*. On 2026-09-09 the four-defect availability repair landed,
`reports/mc50` and the fifty `availability_mc` partitions were deleted, and the ensemble was relaunched
into `reports/mc50` — the same name. `scratchpad/mc/mc50` had not been deleted, because it is a scratch
directory and nothing pointed at it. The name matched, and forty-one draws found twenty years each of
pre-repair prices waiting for them.

**A guard that asks the operator to remember something is not a guard.** That is the whole finding. The
August fix was correct in mechanism and wrong in kind: it moved the failure from "will anyone notice" to
"will anyone remember", which is the same bet at longer odds.

### Four layers of reassurance, again

Every surface said the ensemble was fine. `.draw_complete` was written for all fifty — correctly, since
the deliverables *were* built; they were built from old parquet. `CountMarkers` in `run_all.ps1` counted
fifty. The progress bar reached 50/50. Each stale draw even re-projected step v onto its own fresh
weather and wrote a correct `availability_mc/wdraw=d` partition — which was then never read, because the
dispatch had no year left to solve. Eleven hours instead of forty-four was the only visible tell, and it
reads as good news.

The one line that would have given it away was in the log all along:

```
[20y] horizon 2027-2046 (20 years),  0 to run, draw=0
```

forty-one times. Corroborated by `[fr_availability]` printing exactly 180 applied lines — nine draws ×
twenty years — and by only labels `wdraw=2..10` ever appearing.

### What it was worth, measured

Splitting the ensemble by provenance, FR mean spot, the nine computed draws against the forty-one reused:

| period | computed | reused | diff |
|---|---|---|---|
| 2027-30 | 62.4 | 64.0 | −1.6 |
| 2031-35 | 58.6 | 60.5 | −1.9 |
| 2036-42 | 65.5 | 63.6 | +1.9 |
| 2043-46 | **78.1** | **66.0** | **+12.1** |
| horizon | 65.67 | 63.38 | +2.29 |

That is the availability repair's own signature — it reproduces the single validation run on
REFERENCE_WEATHER_20Y (+2.1 horizon, +13.1 over 2043-46) almost exactly. The ensemble was a mixture of
two models whose late-horizon prices differ by twelve euros, presented as one distribution.

### The repair: hash the model, not the name

`scripts/model_fingerprint.py` digests everything that decides a price — every `*.py` in the five model
packages and `powersim_core`, the four scripts that carry a draw end to end, every `config.yaml`, and the
resolved scenario workbook byte for byte — plus the scalars passed in (master seed, `n_weeks`). 190
components. The work directory becomes `scratchpad/mc/<ensemble>-<digest>/`, so a year produced by a
different model is not rejected, it is simply **not found**.

Two details earn their keep:

  * **The `.draw_complete` marker carries the digest too.** Without that the guard has a hole one level
    above itself: a marker from another model skips the draw entirely, before the fingerprinted work
    directory is ever consulted. A marker naming a different model now prints `RECOMPUTING`.
  * **`FINGERPRINT.json` records the full component map**, not just the digest. A bare hash says
    something moved; `python scripts/model_fingerprint.py a/FINGERPRINT.json b/FINGERPRINT.json` names
    the file. That is the difference between diagnosing a cache miss in five seconds and in an afternoon.

A missing package tree is recorded as `ABSENT` rather than skipped, so a truncated checkout cannot hash
identical to a complete one. The weather cube is deliberately not hashed: for the Monte-Carlo it does not
exist yet and the seed already determines it; a caller running on a fixed cube passes it through `extra`.

`tests/test_model_fingerprint.py` covers it, including a mutation check — reverting the marker comparison
to the pre-fix behaviour fails `test_draw_marker_is_not_trusted_across_models` and nothing else.

### The nine computed draws were kept, and the keeping was verified rather than asserted

Draws 2-10 were computed by the current model and are sound, so they were moved into the fingerprinted
work directory instead of being recomputed — about ten hours saved on a forty-four hour job. But the only
thing that had changed since they ran was `run_montecarlo.py` itself, edited by hand in the same session
to add this guard, and "I know what I changed" is precisely the class of claim this file exists to
distrust.

So it was measured: draw 2's weather cube was regenerated from `SeedSequence([20260629, 2])`, year 2044
recomputed from scratch under the current code, and the result compared against the cached parquet.
**8736 hours x 24 columns, maximum absolute difference 1.8e-12 EUR/MWh** -- HiGHS round-off, not a
behavioural difference -- with FR mean 77.6776734062 on both sides. The nine draws stand, and
`GRANDFATHERED.json` in the work directory records which draws, why, and the measurement.

### What this costs and what it buys

Forty-one draws to recompute at ~3.3 h each, three at a time: about forty-four hours. The sensitivity set
is deferred until the ensemble is rebuilt, because six workers on this machine thrashes and the two stages
must not share it.

The guard is currently wired into `run_montecarlo` only. `run_sensitivities` writes straight into
`reports/sens/<id>` and was verified clean for this run -- the directory was empty when it started -- but
it has the same shape of exposure and should take the same fingerprint before the next sensitivity set.

## Les exports horaires entrelaçaient les vingt années, et corriger un formatage aurait re-résolu tout l'ensemble (2026-09-14)

### The defect

Every per-year `prices_<y>.parquet` written by `run_projection_20y` is indexed on the REFERENCE calendar
(2019-01-01 .. 2019-12-30 23:00, 8736 h), because `projection._align` lays hour k of the target year's weather
onto hour k of the reference year so the weekly-window machinery can slice it. `build_projection_deliverables`
concatenated the twenty years and called `sort_index()`. The years were therefore interleaved hour by hour —
and pandas' default sort is not stable, so even their order within one timestamp was arbitrary.

`hourly_spot_prices.csv` and the "Hourly spot" sheet of `projection_20y_spot_prices.xlsx` held 174,624 rows over
**8,736 distinct timestamps, every one stamped 2019**, adjacent rows from different years: draw 0's first rows read
FR 110.24 -> 1.99 -> 105.51 -> 158.79 -> 76.35 within `2019-01-01 00:00`. No per-year hourly series could be
recovered from either file. Found by another session building an IRR study, which had already switched to the
per-year parquets.

### What it did not touch, verified rather than argued

Every yearly table — price summary, revenue and capture by technology, congestion rent, negative hours, adequacy —
is computed per year inside the loop, before the join. Recomputed from the per-year parquets for all 21 completed
draws x 20 years x 12 zones, `price_summary.csv` agrees to **5.7e-14** on mean, median, max and min. The boxplots,
the sensitivity overlays and the IRR study read those tables or the parquets, never the hourly export.
`archive_mc50.py` already excluded both files from the summary archive as bulky.

Affected: the two hourly exports in every `reports/mc50/draw_XXX` and in `reports/projection_20y`.

### The repair

`to_target_calendar` inverts `_align`: the same offset from 1 January, onto the year's own calendar. That keeps a
leap day where the target year has one and leaves the hours of a window dropped on the solve budget out, rather
than sliding the rest of the year onto them. The years are joined in order with no sort, and the joined index is
asserted strictly increasing. The Notice sheet now says what the timestamps mean.

`tests/test_build_projection_deliverables.py` builds two synthetic years on the 2019 calendar — one leap, one with a
dropped 24 h window — and checks the export is increasing, stamped per year, not interleaved, gap-preserving, value-
faithful, and that the yearly tables are unchanged. Against the unfixed builder 5 of 6 fail; against a mutant that
restores only the old join, the 4 calendar tests fail and the aggregate test passes, as it should.

### The defect behind the defect: the fingerprint hashed something that cannot change a price

`model_fingerprint` listed `build_projection_deliverables.py` among the scripts that "carry one draw end to end".
It does not carry a solve. It runs after the dispatch, in its own process, reads the per-year parquets and formats
them; nothing on the solve path imports it (checked: its only callers are `run_montecarlo` and
`run_sensitivities`, both by subprocess after the solve). But because it was hashed, this one-line formatting fix
would have renamed the ensemble's work directory at the next restart — and with Windows Update rebooting the
machine twice in three days, a restart is not hypothetical — and re-solved all 50 draws, **~6 days**, to reproduce
parquets that were already correct.

A guard that makes every harmless fix expensive teaches people to route around it. So:

  * the builder moves to `model_fingerprint.NON_SOLVE`: the solve digest keys the work directory on what can change
    a solved year and nothing else;
  * deliverables carry their own `.deliverables_fingerprint` (the builder's digest), written LAST;
  * `scripts/migrate_ensemble.py` bridges the one-time change of digest. `migrate` renames an earlier work
    directory ONLY when its recorded `FINGERPRINT.json` is identical to the current files on every solve component
    — a machine-checked equivalence, here `2424d74deccf` on both sides — restamps the draw markers, and rebuilds
    stale deliverables. It refuses (exit 2) when earlier work exists that the current solve model did not produce,
    and (exit 3) while any solver is alive. `rebuild` re-formats completed draws while the ensemble runs: into a
    temp dir, weather tab re-attached from `weather_summary.csv`, hourly export verified against the solved
    parquets, files moved in, stamp last;
  * `run_all.ps1` runs `migrate` at startup, under the lock and after the orphan sweep, and does NOT start
    `run_montecarlo` if it fails: a scheduled task must never decide on its own to re-solve an ensemble.

The migration has not run yet and does not need to: the ensemble in flight keeps its in-memory digest
`723f1f75b645` and its work directory until it stops. At the next driver start the rename happens with no solver
alive. `tests/test_migrate_ensemble.py` covers the rename, the refusal on a solve change, the refusal under live
solvers, the dry run, the no-op, and the recovery of a migration interrupted between rename and restamp.

### Two gaps the new Notice row now names

Every 2046 carries 8,640 hours, not 8,736: the weather cube's 175,200 hours do not cover the horizon's five leap
days, so it ends on 26 December 2046, identically in every draw. And draw 18 / 2037 carries 8,568: reference week
42 (22-28 October) exceeded the 300 s window budget and was dropped. `run_projection_20y` prints `DROPPED` and still
writes the year's completion marker, and nothing downstream checks an hour count; the log that recorded it was
overwritten by later restarts and it was found only by scanning row counts across all 1,368 year files.

## Le vent à 100 m culminait l'après-midi au lieu du soir, et l'empreinte du modèle ne l'aurait pas vu (2026-09-14)

### The defect, in price terms

The weather generator co-generates the 100 m wind that step iv turns into French onshore and offshore output.
Its national diurnal cycle peaked at 13-14 UTC; ERA5 peaks at 20 UTC and observed RTE onshore production at
21 UTC. So every projected year put wind output into the solar hours — deepening the midday surplus the model
already prices near zero — and took it out of the evening ramp, where prices form. Cause, measurements and the
repair are in `weathergen/DECISIONS.md` ("wind100 diurnal shear"): the transfer had no hour term, and the
stability-driven shear cycle sat in a residual simulated as stationary noise. Refit, the co-generated national
cycle matches ERA5 to within 0.4 h in every season on a held-out generator realization (profile corr 0.97-0.99).

### The fingerprint would not have noticed

`model_fingerprint` hashed the five model packages, the solve scripts, the configs and the workbook. It did NOT
hash:

  * the `weathergen` package — `mc_weather.generate_cube` imports it for every draw;
  * any FITTED ARTEFACT: `weathergen/models/wind100.json` and `fitted.json(.npz)`, `res_model/models/*`,
    `demand_model/models/*`, `availability_model/models/*`, `reports/markup_model.json`.

A refit changes nothing but those files. Had the ensemble been relaunched after this refit, the digest would have
been unchanged, and every year already solved on the old wind field would have been reused as if it belonged to
the new one — the failure this module was written to make impossible, through a door it had left open.

They are now hashed. Zip archives above 256 MB (the 2.8 GB `fitted.json.npz`) are hashed on size, head and
tail: an npz is a zip, whose central directory at the tail carries every member's CRC32, so any change to any
array moves the tail. Any other file is hashed in full (as first written this sampled every large file; the review
entry below narrowed it). Backups live in sub-directories (`models/backup_pre_diurnal/`), outside the top-level
globs, so keeping or deleting one never moves the digest. Tests:
`test_fitted_artefacts_and_the_weather_generator_are_part_of_the_digest`,
`test_a_large_npz_is_sampled_and_a_change_to_a_middle_member_still_moves_the_digest` and
`test_a_large_file_that_is_not_a_zip_is_hashed_in_full`.

### What it invalidates

Everything simulated on the old wind field:

  * the 21 completed Monte-Carlo draws (work directory `scratchpad/mc/mc50-723f1f75b645`, deliverables
    `reports/mc50/draw_000..020`) — the ensemble must be recomputed from scratch;
  * `archive/REFERENCE_WEATHER_20Y.nc`, stitched from the old draw cubes and selected on the old ensemble's
    prices — it must be re-selected once the new ensemble exists, and the sensitivities run after that;
  * the default cube `weathergen/output/simulation.nc` (regenerated; every other variable verified bit-identical).

The guard behaves as intended: with the digest changed and the old work directory present,
`migrate_ensemble.py migrate` refuses (exit 2) and `run_all.ps1` does not start a solve until that directory and
the old deliverables are moved aside deliberately. The deliverables rebuild for the old draws was stopped: it
was re-formatting results that no longer stand.

## La revue adverse du correctif du vent : ce qu'une relance aurait encore réutilisé de l'ancien modèle (2026-09-14)

An adversarial review of the wind100 refit ran five independent dimensions, and each serious finding was checked by a skeptic told to refute it. Nothing that was checked was refuted outright. Most of what it found was not in the refit itself but in the path a relaunch would take: places where the ensemble's own guard could be walked around.

### Three cubes of the old wind, one step from being reused

`run_draw` reused any cube at `scratchpad/mc/cube_<d>.nc` that existed. The draws interrupted on 2026-09-11 had left `cube_021/022/023.nc` behind, all 853.6 M bytes and so all above the truncation guard in `run_all.ps1`. Their national 100 m wind peaked at 14 UTC, the pre-fix phase, so a relaunch would have priced draws 21-23 of the NEW ensemble on the OLD wind under the NEW digest.

Their driver caches (`res/proj_drivers` and `demand/projection_features`, realizations 21-23) were NEWER than the cubes, so the mtime freshness test would have called them fresh too. Three verifiers measured it independently. All six artefacts were deleted.

For the structure, the cube now lives in the fingerprinted work directory, `scratchpad/mc/<ensemble>-<digest>/cube_<d>.nc`. `generate_cube` writes it to a temp name and renames it, and stamps `model_fp`. It is reused only when its attributes name this model, this master seed and this draw; any other cube is regenerated.

### A recomputing draw no longer looks complete

When a marker named another model, `run_draw` recomputed, but the old `.draw_complete` stayed until the very end. Meanwhile `consolidate_mc50.py`, `archive_mc50.py` and `mk_sens_overlay_charts.py` counted any marker whose file existed. So during a three-day relaunch every consolidation would have merged recomputed and not-yet-recomputed draws. Worse, a draw that failed kept its old marker for good.

The marker and the deliverables stamp are now retired before any work. The three scripts select only draws whose marker names one model: the model on disk by default, or `--fingerprint`. They report what they leave out. The archive now hashes only those draws' files rather than everything under `reports/mc50`, and records the fingerprint in its manifest.

### The digest is checked on every draw, not once per ensemble

`fp` was computed at launch, while each draw imports whatever is on disk, in fresh interpreters. An edit during a multi-day run would have put two models under one digest. The stated reason for hashing once was that it was too expensive. Measured, the full component map costs 0.7 s, against about 3.3 h per draw. Each draw now re-hashes and refuses to write under a changed model. The digest and `FINGERPRINT.json` are also derived from one component map, so they can no longer describe two different states.

### What the fingerprint still did not see

- **The SQLite database (17.7 GB).** It is read throughout the solve. It is identified by size and modification time, never content-hashed. Nothing on the solve path writes it (checked: only ETL and ERA5 ingestion do), so it cannot move under a run.
- **About twenty behaviour switches in the environment**, `DISPATCH_*` and `POWERSIM_*`: flexibility VOM, demand scale, GB capacity, unclassified generation and the like. Every one that is set is now hashed. The per-draw values `run_montecarlo` sets itself are excluded, and so is `POWERSIM_SCENARIOS`, whose effect the workbook's bytes already carry.
- **Library versions:** HiGHS, numpy, scipy, pandas, xarray, netCDF4, pyarrow and Python. A HiGHS upgrade can change which vertex a degenerate LP returns, and so the prices.
- **`weathergen/output/zone_hist.parquet`**, the observed per-zone weather the neighbour response is fitted on.

The large-file sampling claim was verified on the real file. `fitted.json.npz` is a zip64 archive with 1005 stored members and a central directory 57 KB from the end. But the scheme applied to ANY file above 256 MB. It now samples zips only, and hashes anything else in full.

The old sampling test flipped a byte inside the sampled tail, so any head/tail scheme would pass it. It is replaced by two tests. The first edits a middle member of a real `np.savez` at the same size and checks the digest moves. The second checks a large non-zip is hashed in full. Environment and database components are tested too. The live-repository test now skips on a checkout without the gitignored artefacts, where it used to fail.

### Driver caches remember which cube built them

`projection_drivers` and `projection_features` were keyed on the realization number, with "cache newer than cube" as the freshness test. Three different cubes use realization 0: the default cube, `REFERENCE_WEATHER_20Y`, and Monte-Carlo draw 0. Whichever was written last looked fresh for the other two. `golden/baseline.json` recorded a `w100_nat` mean of 5.668613, which is the French mean of the REFERENCE cube, not of `simulation.nc`.

Each partition now carries a `source_cube.json` sidecar with the cube's resolved path, size and mtime (ns). A cache is fresh only when the sidecar names the cube that will really be read. Partitions from before the sidecar have none and are rebuilt. Test: `powersim_core/tests/test_cache_identity.py`.

### The sensitivities had no guard at all

- **No fingerprint.** `reports/sens/s00/s01/s02` held 27 pre-fix years that a relaunch would have resumed from.
- **Stale reference weather.** `REFERENCE_WEATHER_20Y.nc` is itself pre-fix: its 100 m wind peaks at 14 UTC.
- **Stage 2 ran after a refused stage 1.** `run_all.ps1` started it anyway.

Now:
- **Each variant is keyed on its own digest**: the model, plus the reference cube's identity, the variant's environment and its workbook's hash.
- **Another model's results are moved to `reports/sens/_stale/`**, never deleted and never resumed.
- **`.sens_complete` carries the digest.**
- **A reference cube older than `weathergen/models` is refused** unless `--allow-stale-reference` is passed.
- **`run_all.ps1` skips stage 2 when stage 1 was refused.**

### The readers counted what the runner would recompute

`consolidate_sensitivities.py` and `mk_sens_overlay_charts.py` counted any `.sens_complete`. Until each stale variant's slot came round, a relaunch would have published deltas between a new-model baseline and old-model variants. They now count a variant only when its marker names the fingerprint from `run_sensitivities.variant_fingerprints`, the one definition the runner writes with. `check_reference_weather.py` likewise compares a reference run only against draws of the model on disk.

`build_reference_weather.py` trusted both of its inputs:
- **Cubes:** it reused any cube left in `scratchpad/refweather/` (none is there today). It now reuses one only when its attributes name this model, seed and draw.
- **The year map:** it took `ref_weather_map.json` as given. It now refuses a map that names a draw not complete under the model on disk, unless `--allow-unverified-map` is passed. That condition is necessary, not sufficient: no committed tool writes the map yet.
- **Its output:** written through a temp name and stamped `model_fp`. A killed write can no longer leave a newer, truncated reference that would pass the sensitivities' age check.

The truncated-cube guard in `run_all.ps1` now recurses into the fingerprinted work directories, where the cubes moved.

### And one mislabelled summary

`mc_weather.weather_summary`, the "Weather (this draw)" tab of every draw, averaged all 76 European stations as if they were France. On the 2026-08 cube that gave a 100 m wind of 5.58 m/s against 5.69, and a temperature of 12.4 °C against 13.2. It now uses the French stations.

### Left open, deliberately

- **Nothing in the repository selects `REFERENCE_WEATHER_20Y`.** `build_reference_weather.py` reads a `ref_weather_map.json` that no committed script writes. Re-selecting it on the new ensemble needs that tool first.
- **The old year map was moved aside, to `scratchpad/ref_weather_map_pre_wind100_723f1f75b645.json`.** It was selected on the pre-fix ensemble's prices. The builder's only check is that every draw the map names is complete under the current model, and once the new ensemble finishes, every draw it names will pass. The builder would then have stitched new-wind years onto an old-price selection without a warning. With the map moved, the builder stops on a missing file until a map is selected from the new ensemble.
- **The golden baseline was recaptured**, after rebuilding the realization-0 projections from the default cube (backup `golden/baseline_pre_wind100_20260914.json.bak`). Five of fourteen artefacts moved, and not all of it is the wind:
  - the drivers and features also carried another cube's temperature (the realization-0 sharing above);
  - `res/production` had been frozen since 2026-07-15;
  - `demand/projection_hourly` (−17.5 %) had been frozen since 2026-08-12, before the demand moderation that aba6a5d committed (see "The French demand sensitivities" above).

  Quantified in `REVIEW.md` §13.
- **`reports/irr_nancy_mc21/site_weather.py`**, the other session's IRR study, re-implements the pre-fix 100 m formula as `a + b·log w10` and ignores the refit's terms. Its guard checks only `c is None`. It is flagged, not edited. After its per-site quantile mapping the effect on capture timing is small (hourly CF correlation 0.985), but its docstring's claim of an exact replay no longer holds.

Tests: `tests/test_montecarlo_relaunch.py` (5), `tests/test_sensitivity_markers.py` (2), the updated `tests/test_model_fingerprint.py`, and `powersim_core/tests/test_cache_identity.py`.

### The ensemble's offshore conversion

`run_montecarlo.py` sets `POWERSIM_RES_OFFSHORE_LOSSES=1` for every draw, and `run_sensitivities.py` for every variant. With it, step iv applies the availability and wake loss its offshore level scale was fitted with. Without it, the loss-free curve is clipped at 1.0 and held 25 % of winter hours on a flat ceiling (`res_model/DECISIONS.md`, 2026-09-14). Both scripts are part of the digest, so the ensemble's fingerprint moves with the change. A standalone `run_projection_20y.py` run without the flag still prices the legacy form.
