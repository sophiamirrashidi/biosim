"""The sensor safety requirements of sensor_safety_requirements.md, as STL formulas.

Each Requirement carries the classification used in CSCI 513 lecture 6 (Requirements):
kind (hard safety / hard liveness / soft performance / security) and granularity (system,
subsystem, function, timing). Its formula is evaluated on a *requirement signal*, one sample
per tick, built from a run's log by `signal_from_run`:

    T, co2, o2, p, E, rz     true cabin temperature, CO2 ppm, O2 fraction, pressure, battery
                             energy, root-zone reservoir (from the log's truth: what really
                             happened, not what the sensors said)
    fault.<ch>               ground truth: a sensor fault is injected on channel <ch>
    flag.<ch>                the safety layer considers <ch> unhealthy this tick
    heater_fault             ground truth: a heater actuator fault is injected
    mode                     FailSafeSupervisor mode, 0 NOMINAL .. 3 EMERGENCY
    co2_untrusted            the CO2 or pressure channel is unhealthy
    inject_excess            applied CO2 injection minus the crop's measured uptake (mol)
    cmd_in_range             every applied command is finite and inside its schema range
    any_fault                any injected fault is active

Ticks are hours. `evaluate` returns one RequirementResult per requirement with its robustness
(rho > 0 satisfied, rho < 0 violated; see safety.stl).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from interface.schema import COMMAND_KEYS
from .failsafe import Mode
from .stl import And, Between, Flag, Formula, G, F, Ge, Le, Not

# channel name -> sensor state key, in obs and in Fault.target
CHANNELS: Dict[str, str] = {
    "temp": "gh.temp_C",
    "co2": "gh.co2_ppm",
    "o2": "gh.o2_frac",
    "pressure": "gh.pressure_kPa",
    "rz": "crop.s0.root_zone_L",
}
SENSOR_FAULT_KINDS = frozenset({"noise", "bias", "drift", "stuck", "dropout"})
# a noise fault only widens the spread; a small one is not a requirement to detect
DETECTABLE_KINDS = frozenset({"bias", "drift", "stuck", "dropout"})

# Thresholds (sensor_safety_requirements.md section 4)
T_SURVIVE = (7.0, 32.0)        # CRITICAL band edge in interface.sensors.temp_alarm_band
T_NOMINAL = (18.0, 25.0)
T_RECOVER = 15.0
CO2_MAX_PPM = 5000.0           # long-term crop/crew exposure ceiling (hard)
CO2_NOMINAL = (1000.0, 1500.0)
O2_RANGE = (0.195, 0.235)
P_MIN_KPA = 65.0
E_RESERVE = 10000.0
RZ_MIN_L = 2.0
RZ_PRIME_TICKS = 24          # the bed reservoir starts empty and is primed in the first sol
# Smallest fault a detection requirement covers (the input pre-condition, lecture 7 slide 29).
# Smaller biases and drifts sit inside the sensor + model noise; they are tolerated, and their
# effect on the plant is bounded by the system-level requirements (SSR-01..05) instead.
DETECT_MIN = {
    ("temp", "bias"): 2.0, ("temp", "drift"): 0.1,          # degC, degC/tick
    ("co2", "bias"): 150.0, ("co2", "drift"): 5.0,          # ppm, ppm/tick
    ("o2", "bias"): 0.01, ("o2", "drift"): 0.001,
    ("pressure", "bias"): 2.0, ("pressure", "drift"): 0.2,  # kPa, kPa/tick
    ("rz", "bias"): 2.0, ("rz", "drift"): 0.1,              # L, L/tick
}
DETECT_DEADLINE = {"dropout": 1, "stuck": 4, "bias": 6, "drift": 24}
REACT_TICKS = 1
RECOVER_WITHIN, RECOVER_HOLD = 2, 6
RETURN_WITHIN = 30


@dataclass(frozen=True)
class Requirement:
    id: str
    title: str
    kind: str          # "hard-safety" | "hard-liveness" | "soft-performance" | "security"
    level: str         # "system" | "subsystem" | "function" | "timing"
    formula: Formula
    text: str
    protocol: str = ""  # fail-safe protocol that discharges it (P1..P6), if any
    needs: Sequence[str] = ()  # signal keys that must exist for the requirement to apply


@dataclass
class RequirementResult:
    req: Requirement
    rho: float
    applicable: bool = True

    @property
    def passed(self) -> bool:
        return (not self.applicable) or self.rho > 0

    def row(self) -> str:
        if not self.applicable:
            return f"{self.req.id:7s} {'n/a':>9s}  {self.req.title}"
        verdict = "PASS" if self.passed else "FAIL"
        return f"{self.req.id:7s} {self.rho:9.3f}  {verdict}  {self.req.title}"


def _detect(ch: str, kind_deadline: int) -> Formula:
    # whenever the fault is on, the channel is flagged within the deadline
    return G(Flag(f"fault.{ch}").implies(F(Flag(f"flag.{ch}"), 0, kind_deadline)))


REQUIREMENTS: List[Requirement] = [
    # --- system level, hard safety (what must never happen) -------------------------------
    Requirement("SSR-01", "Cabin temperature stays in the crop survival band", "hard-safety", "system",
                G(Between("T", *T_SURVIVE)),
                "G[0,T] (7 <= T_cabin <= 32 degC)", "P3,P4,P5", ("T",)),
    Requirement("SSR-02", "Cabin CO2 never reaches the exposure ceiling", "hard-safety", "system",
                G(Le("co2", CO2_MAX_PPM)),
                "G[0,T] (CO2 <= 5000 ppm)", "P2", ("co2",)),
    Requirement("SSR-03", "O2 fraction and total pressure stay habitable", "hard-safety", "system",
                G(And(Between("o2", *O2_RANGE), Ge("p", P_MIN_KPA))),
                "G[0,T] (0.195 <= O2 <= 0.235 and P >= 65 kPa)", "P1", ("o2", "p")),
    Requirement("SSR-04", "Battery keeps the life-support reserve", "hard-safety", "system",
                G(Ge("E", E_RESERVE)),
                "G[0,T] (E_battery >= 10 kWh)", "P4", ("E",)),
    Requirement("SSR-05", "Root-zone reservoir never runs dry", "hard-safety", "system",
                And(F(Ge("rz", RZ_MIN_L), 0, RZ_PRIME_TICKS), G(Ge("rz", RZ_MIN_L), RZ_PRIME_TICKS)),
                "F[0,24] (reservoir >= 2 L) and G[24,T] (reservoir >= 2 L)  (the shelf starts empty)",
                "P1", ("rz",)),

    # --- function level, timing: detection of sensor faults --------------------------------
    *[Requirement(f"SSR-1{i}", f"{ch} sensor fault flagged in time", "hard-liveness", "timing",
                  _detect(ch, DETECT_DEADLINE["stuck"]),  # deadline set per run from the fault kind
                  f"G (fault_{ch} -> F[0,d_kind] flag_{ch}),  d = {DETECT_DEADLINE}", "P1",
                  (f"fault.{ch}",))
      for i, ch in enumerate(CHANNELS)],

    Requirement("SSR-20", "No false sensor alarms on a healthy run", "soft-performance", "function",
                G(Not(Flag("any_flag"))),
                "if no fault is ever injected: G[0,T] !flag_any", "", ()),

    # --- subsystem level: the fail-safe reaction --------------------------------------------
    Requirement("SSR-21", "An unhealthy sensor takes the supervisor out of NOMINAL within 1 tick",
                "hard-safety", "timing",
                G(Flag("flag.any").implies(F(Ge("mode", 0.5), 0, REACT_TICKS))),
                "G (flag_any -> F[0,1] mode >= DEGRADED)", "P1", ()),
    Requirement("SSR-22", "CO2 is never injected beyond replacement on an untrusted reading",
                "hard-safety", "function",
                G(Flag("co2_untrusted").implies(Le("inject_excess", 1e-6))),
                "G (co2_untrusted -> u_co2 <= uptake)", "P2", ()),
    Requirement("SSR-23", "Heater fault: cabin back above 15 degC within 2 h and held 6 h",
                "hard-liveness", "subsystem",
                G(Flag("heater_fault").implies(F(G(Ge("T", T_RECOVER), 0, RECOVER_HOLD), 0, RECOVER_WITHIN))),
                "G (heater_fault -> F[0,2] G[0,6] T >= 15 degC)", "P4", ("T",)),
    Requirement("SSR-24", "Every applied command is finite and within its range", "security", "function",
                G(Flag("cmd_in_range")),
                "G (for all u: u_min <= u <= u_max)", "P6", ()),
    Requirement("SSR-25", "After all faults clear the supervisor returns to NOMINAL", "hard-liveness",
                "subsystem",
                G(Flag("fault_cleared").implies(F(Le("mode", 0.5) | Flag("any_fault"), 0, RETURN_WITHIN))),
                "G (fault_cleared -> F[0,30] (mode = NOMINAL or a new fault))", "P1", ()),

    # --- soft performance -------------------------------------------------------------------
    Requirement("SSR-30", "Cabin temperature stays in the growth band while fault-free",
                "soft-performance", "system",
                G(Flag("any_fault") | Between("T", *T_NOMINAL)),
                "G (!fault -> 18 <= T <= 25 degC)", "", ("T",)),
]

_BY_ID = {r.id: r for r in REQUIREMENTS}


def requirement(rid: str) -> Requirement:
    return _BY_ID[rid]


# ---- building the requirement signal -----------------------------------------------------------

def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _must_detect(ch: str, fault: Mapping[str, Any]) -> bool:
    kind = fault["kind"]
    if kind not in DETECTABLE_KINDS:
        return False
    if kind in ("stuck", "dropout"):
        return True
    param = "offset" if kind == "bias" else "rate"
    size = abs(_f(fault.get("params", {}).get(param)) or 0.0)
    return size >= DETECT_MIN.get((ch, kind), 0.0)


def _cmd_in_range(applied: Mapping[str, Any]) -> bool:
    for key, v in applied.items():
        spec = COMMAND_KEYS.get(key)
        x = _f(v)
        if x is None:
            return False
        if spec is not None and ((spec.lo is not None and x < spec.lo) or (spec.hi is not None and x > spec.hi)):
            return False
    return True


def signal_from_run(records: Sequence[Mapping[str, Any]], supervisor=None) -> List[Dict[str, Any]]:
    """One requirement sample per logged tick. `supervisor` is the FailSafeSupervisor used in
    the run (for mode and channel health); without it the flags come from the monitors' labels."""
    history = getattr(supervisor, "history", {}) if supervisor is not None else {}
    ever = any(rec.get("faults") for rec in records)
    sig: List[Dict[str, Any]] = []
    had_fault = False
    for rec in records:
        truth, obs, applied = rec.get("truth", {}), rec.get("obs", {}), rec.get("applied", {})
        h = history.get(rec["tick"], {})
        faults = rec.get("faults", [])
        s: Dict[str, Any] = {
            "tick": rec["tick"],
            "T": _f(truth.get("gh.temp_truth_C", truth.get("gh.temp_C"))),
            "co2": _f(truth.get("gh.co2_ppm")),
            "o2": _f(truth.get("gh.o2_frac")),
            "p": _f(truth.get("gh.pressure_kPa")),
            "E": _f(truth.get("power.store_level")),
            "rz": _f(truth.get("crop.s0.reservoir_L")),
            "mode": Mode[h["mode"]].value if "mode" in h else None,
            "any_fault": bool(faults),
            "any_fault_ever": ever,
            "heater_fault": any(f["target"] == "cmd.heater_W" for f in faults),
            "cmd_in_range": _cmd_in_range(applied),
        }
        health = h.get("health", {})
        any_flag = False
        for ch, key in CHANNELS.items():
            mine = [f for f in faults if f["target"] == key and _must_detect(ch, f)]
            s[f"fault.{ch}"] = bool(mine)
            s[f"fault_kind.{ch}"] = mine[0]["kind"] if mine else None
            if key in health:
                flagged = not health[key]["healthy"]
            else:
                labels = obs.get({"temp": "temp_faults", "co2": "co2_faults", "o2": "o2_faults",
                                  "pressure": "pressure_faults", "rz": "rz.faults"}[ch]) or []
                flagged = bool(labels) or (key in obs and obs.get(key) is None)
            s[f"flag.{ch}"] = flagged
            any_flag = any_flag or flagged
        s["any_flag"] = any_flag
        s["flag.any"] = any_flag
        s["co2_untrusted"] = s["flag.co2"] or s["flag.pressure"]
        uptake = _f(obs.get("crop.s0.co2_inhaled_mol")) or 0.0
        inject = _f(applied.get("cmd.co2_inject_mol")) or 0.0
        s["inject_excess"] = inject - uptake
        s["fault_cleared"] = had_fault and not faults
        had_fault = bool(faults)
        sig.append(s)
    return sig


def _detection_formula(sig: Sequence[Mapping[str, Any]], ch: str) -> Formula:
    kinds = {s.get(f"fault_kind.{ch}") for s in sig} - {None}
    deadline = max((DETECT_DEADLINE.get(k, 24) for k in kinds), default=1)
    return _detect(ch, deadline)


def evaluate(records: Sequence[Mapping[str, Any]], supervisor=None,
             ids: Optional[Iterable[str]] = None) -> List[RequirementResult]:
    sig = signal_from_run(records, supervisor)
    chosen = [r for r in REQUIREMENTS if ids is None or r.id in set(ids)]
    out: List[RequirementResult] = []
    for req in chosen:
        applicable = bool(sig) and all(any(s.get(k) is not None and s.get(k) is not False for s in sig)
                                       for k in req.needs)
        if req.id in ("SSR-21", "SSR-25") and supervisor is None:
            applicable = False
        if req.id == "SSR-20" and sig and sig[0]["any_fault_ever"]:
            applicable = False
        formula = req.formula
        if req.id.startswith("SSR-1"):
            ch = list(CHANNELS)[int(req.id[-1])]
            formula = _detection_formula(sig, ch)
        rho = formula.rho(sig, 0) if applicable else math.nan
        out.append(RequirementResult(req, rho, applicable))
    return out


def report(results: Sequence[RequirementResult]) -> str:
    return "\n".join(r.row() for r in results)
