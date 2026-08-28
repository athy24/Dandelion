"""Britain's fleet is FROZEN AT 1 JANUARY 2019 in the lake, and this is the trajectory that unfreezes it.

`entsoe_installed_capacity` holds exactly TWELVE rows for GB, all stamped `2019-01-01`, and
`load_installed_capacity` serves them for every year queried. GB is the only zone with this problem — FR,
DE_LU, BE, NL, ES, CH and PT all move between 2019 and 2025 — because ENTSO-E stopped publishing British
capacity after Brexit and Elexon, which replaced it for generation and load, publishes no capacity register.

So the 2025 backtest was running Britain's 2019 fleet:

    tech       model (every year)   reality 2025
    nuclear          9.23 GW           ~6.04 GW    Hunterston B and Hinkley Point B both closed 2022
    coal             8.28 GW            0.00 GW    Ratcliffe-on-Soar, the last plant, closed Sept 2024
    gas             36.15 GW           ~35 GW      (about right)

THE NUCLEAR ERROR IS THE ONE THAT BITES, and it points the right way for the defect it helps explain.
3.2 GW of phantom baseload is ~28 TWh/yr of cheap must-run in a 287 TWh system, sitting below gas in merit
and displacing it from exactly the hours where gas should be setting the price. Measured against observed
2025, gas is marginal in 28.2 % of modelled GB hours against 60.4 % observed, and GB's mean is 59.6 EUR/MWh
against 93.4. Phantom COAL matters less for price — at 2025 commodity prices coal SRMC is ~200 against gas
~113, so it sits above gas and rarely runs — but 8 GW of capacity that does not exist still props up
adequacy and suppresses scarcity.

ANCHORS ARE PUBLISHED CLOSURE AND COMMISSIONING DATES, NOT A FIT. Values are interpolated between anchors.

NUCLEAR — retirements:
  * Dungeness B      1040 MW  off-load since 2018, closure confirmed June 2021
  * Hunterston B      965 MW  closed January 2022
  * Hinkley Point B   965 MW  closed August 2022
  * Hartlepool       1185 MW  \
  * Heysham 1        1155 MW   |  all four now to MARCH 2030. Heysham 1 and Hartlepool were extended from
  * Heysham 2        1250 MW   |  March 2027 to March 2030 in July 2026, after the 2025 inspections;
  * Torness          1250 MW  /   Heysham 2 and Torness were already at March 2030.
  * Sizewell B       1198 MW  to 2035. A 20-year extension (2035-2055) is under active negotiation with
                              ~GBP 800m of investment attached, but no commercial model is agreed, so the
                              STATED schedule is modelled and the extension is left as a sensitivity.
                              `DISPATCH_GB_SZB_EXTENDED=1` keeps it to 2055 instead.

NUCLEAR — new build:
  * Hinkley Point C  2 x 1630 MW  unit 1 first power 2030 (worst case 2031), unit 2 about a year later.
  * Sizewell C       2 x 1600 MW  FID taken July 2025; commercial operation mid-to-late 2030s. Modelled
                              2036 and 2037, which is the early end of "mid-to-late" and therefore the
                              assumption most likely to need revisiting.

The 2030 trough is real and is the point of modelling this: four AGRs leave in one March, HPC unit 1 is the
only thing replacing them that year, and GB nuclear drops from ~6.0 GW to ~2.8 GW before recovering past
6 GW only once Sizewell C is complete around 2037.

COAL: 8.28 GW in 2019 to zero by 2025 — Cottam (2019), Aberthaw B and Fiddler's Ferry (2020), West Burton A
(2023), Ratcliffe-on-Soar (Sept 2024, the last coal plant in Great Britain).

Opt out with `DISPATCH_GB_CAPACITY=0`, which restores the frozen 2019 snapshot exactly.
"""
from __future__ import annotations

import os

import numpy as np

#: {tech: {year: MW}}. Anchors only; `_interp` fills between them and clamps flat outside.
_TRAJECTORY: dict[str, dict[int, float]] = {
    "nuclear": {
        2019: 9229.0,     # the lake's own 2019 snapshot — the trajectory starts from what is measured
        2020: 9229.0,
        2021: 8189.0,     # Dungeness B confirmed closed
        2022: 6259.0,     # Hunterston B (Jan) and Hinkley Point B (Aug)
        2023: 6038.0,     # settles on the surviving unit sum
        2029: 6038.0,
        2030: 2828.0,     # four AGRs leave end-March; Sizewell B 1198 + HPC unit 1 1630
        2031: 2828.0,
        2032: 4458.0,     # HPC unit 2
        2035: 4458.0,
        2036: 4860.0,     # Sizewell B retires, Sizewell C unit 1 arrives
        2037: 6460.0,     # Sizewell C unit 2 — HPC 3260 + SZC 3200
        2050: 6460.0,
    },
    "coal": {
        2019: 8280.0, 2020: 5000.0, 2021: 4000.0, 2022: 3000.0,
        2023: 2000.0, 2024: 1000.0, 2025: 0.0, 2050: 0.0,
    },
}

#: Sizewell B under the agreed 20-year extension (2035 -> 2055) instead of its stated 2035 end.
_SZB_MW = 1198.0


#: GB INTERCONNECTION TO ZONES THIS MODEL DOES NOT HAVE (MW of import capability, by commissioning year).
#:
#: Great Britain is wired to Norway and Ireland as well as to the continent, and neither is a modelled zone.
#: The consequence was measured on the 2025 backtest, reading the primal in GB's 37 VoLL hours: GB imports
#: 5,281 MW in EVERY one of them, identical to the megawatt, with all four modelled borders binding at
#: 100 % — FR 2,951, DK 1,073, NL 722, BE 532. GB is not short of PLANT (max net load 34.5 GW against a
#: 43.1 GW fully-available stack, zero hours short in any year); it is short of CABLE. 36 of the 37 hours
#: are in January, daytime.
#:
#: Real GB import capability is ~9.3 GW, of which ~2.9 GW sits on borders the model cannot represent:
#:     North Sea Link (Norway)        1400 MW   commissioned Oct 2021
#:     Moyle + EWIC (Ireland)         1000 MW   long established
#:     Greenlink (Ireland)             500 MW   commissioned 2025
#:
#: WHY IT IS PRICED ABOVE GB's OWN GAS rather than at Nordic hydro cost. These are the last cables to fire.
#: Nordic reservoirs are managed against their own scarcity and NO2 prices spike alongside GB in exactly
#: the cold, still, January periods that produce these hours, while the Irish fleet is gas peaking — so the
#: hours when GB needs this block are the hours it is expensive. Pricing it cheap would import Nordic hydro
#: into GB's ordinary merit order and make a zone that is ALREADY 36 % below the market cheaper still.
#: Modelled as gas at 0.33 efficiency, below the worst of `EFF_RANGE["gas"]` (0.40), so it indexes to the
#: commodity deck instead of being a fixed bid that would fall below gas under a high-CO2 2046 deck. At the
#: 2025 deck that is ~180 EUR/MWh against ~148 for GB's own worst gas: it runs only once the domestic stack
#: is exhausted, which is the whole intent.
#:
#: KNOWN IMPRECISION: being tagged `gas` in a GB stack, it also picks up the Carbon Price Support, which a
#: Norwegian or Irish import does not pay. That makes the block slightly too dear, so it runs slightly too
#: rarely — conservative in the direction that matters, and small against the efficiency assumption above.
#:
#: This is a STAND-IN for two unmodelled zones, not a representation of them. It cannot export, it has no
#: hydrology, and it cannot be scarce when Norway is scarce. `DISPATCH_GB_EXT_IMPORT=0` removes it.
_EXT_IMPORT_MW: dict[int, float] = {2019: 1000.0, 2020: 1000.0, 2021: 2400.0, 2024: 2400.0,
                                    2025: 2900.0, 2050: 2900.0}
#: Efficiency the block is priced at — see the note above.
_EXT_IMPORT_EFF = 0.33


def ext_import_mw(year: int) -> float:
    """→ import capability (MW) from zones this model does not carry, for `year`. 0 when disabled."""
    if os.environ.get("DISPATCH_GB_EXT_IMPORT", "1") in ("0", "false", "False"):
        return 0.0
    return _interp(_EXT_IMPORT_MW, int(year))


def enabled() -> bool:
    """Read at each call site so an A/B arm can flip it without reimporting."""
    return os.environ.get("DISPATCH_GB_CAPACITY", "1") not in ("0", "false", "False")


def _szb_extended() -> bool:
    return os.environ.get("DISPATCH_GB_SZB_EXTENDED", "0") not in ("0", "false", "False")


def _interp(series: dict[int, float], year: int) -> float:
    ys = np.array(sorted(series))
    return float(np.interp(year, ys, np.array([series[y] for y in ys], float)))


def capacity_mw(tech: str, year: int) -> float | None:
    """→ GB capacity (MW) for `tech`/`year`, or None where this module has no opinion."""
    t = _TRAJECTORY.get(tech)
    if t is None:
        return None
    mw = _interp(t, int(year))
    if tech == "nuclear" and _szb_extended() and int(year) >= 2036:
        mw += _SZB_MW                     # the sensitivity: Sizewell B runs to 2055 rather than 2035
    return mw


def apply(zone: str, year: int, installed: dict) -> dict:
    """Overlay the GB trajectory on a frozen `installed` dict. Identity for every other zone, and for GB
    when disabled — so the pre-trajectory behaviour is one env var away."""
    if str(zone) != "GB" or not installed or not enabled():
        return installed
    out = dict(installed)
    for tech in _TRAJECTORY:
        mw = capacity_mw(tech, year)
        if mw is not None:
            out[tech] = float(mw)
    return out
