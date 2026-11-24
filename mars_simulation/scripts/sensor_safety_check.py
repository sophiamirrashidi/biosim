"""Falsification of the sensor safety requirements (CSCI 513 lecture 7, "Falsification using
optimization").

The input space is a finite parameterization of fault schedules: which sensor or actuator
fails, how (kind), how badly (magnitude), when (start tick) and for how long, plus optionally a
second simultaneous fault. Each sample is simulated with the full safety stack
(safety.harness) and scored by STL robustness of every hard requirement. The search is random
sampling followed by stochastic local search around the lowest-robustness sample; any sample
with rho < 0 is a counterexample and is printed so it can become a regression test.

    python scripts/sensor_safety_check.py                 # FakePlant, 60 samples
    python scripts/sensor_safety_check.py -n 300 --seed 7
    python scripts/sensor_safety_check.py --no-failsafe   # the same search without the supervisor
    python scripts/sensor_safety_check.py --include-residual   # also stuck-open CO2 injector
    python scripts/sensor_safety_check.py --live           # BioSim on localhost:8009 (slow)

Exits 1 if a counterexample was found.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interface import Fault  # noqa: E402
from safety.harness import run_scenario  # noqa: E402
from safety.requirements import REQUIREMENTS, evaluate  # noqa: E402

HARD = [r.id for r in REQUIREMENTS if r.kind != "soft-performance"]

# target -> (kinds, magnitude parameter per kind with its range)
SPACE: Dict[str, Dict[str, Tuple[str, float, float]]] = {
    "gh.temp_C":       {"bias": ("offset", -6.0, 6.0), "drift": ("rate", -0.4, 0.4),
                        "stuck": ("", 0, 0), "dropout": ("", 0, 0)},
    "gh.co2_ppm":      {"bias": ("offset", -600.0, 600.0), "drift": ("rate", -20.0, 20.0),
                        "stuck": ("", 0, 0), "dropout": ("", 0, 0)},
    "gh.o2_frac":      {"bias": ("offset", -0.03, 0.03), "stuck": ("", 0, 0), "dropout": ("", 0, 0)},
    "gh.pressure_kPa": {"bias": ("offset", -10.0, 10.0), "stuck": ("", 0, 0), "dropout": ("", 0, 0)},
    "cmd.heater_W":    {"stuck": ("value", 0.0, 0.0), "efficiency": ("factor", 0.0, 0.9)},
}
RESIDUAL = {"cmd.co2_inject_mol": {"stuck": ("value", 0.3, 1.0)}}


def sample_fault(rng: random.Random, space) -> Fault:
    target = rng.choice(sorted(space))
    kind = rng.choice(sorted(space[target]))
    pname, lo, hi = space[target][kind]
    params = {pname: rng.uniform(lo, hi)} if pname else {}
    return Fault(tick=rng.randint(200, 400), kind=kind, target=target, params=params,
                 duration=rng.randint(6, 96))


def perturb(f: Fault, rng: random.Random, space) -> Fault:
    pname, lo, hi = space[f.target][f.kind]
    params = dict(f.params)
    if pname and hi > lo:
        params[pname] = min(hi, max(lo, params[pname] + rng.gauss(0, 0.15 * (hi - lo))))
    return Fault(tick=min(400, max(200, f.tick + rng.randint(-12, 12))), kind=f.kind, target=f.target,
                 params=params, duration=min(96, max(6, (f.duration or 24) + rng.randint(-12, 12))))


def score(faults: List[Fault], failsafe: bool, make_plant=None) -> Tuple[float, Dict[str, float]]:
    end = max(f.tick + (f.duration or 0) for f in faults) + 40
    records, fs = run_scenario(faults, n_ticks=end, failsafe=failsafe, make_plant=make_plant)
    res = evaluate(records, fs, ids=HARD)
    rhos = {r.req.id: r.rho for r in res if r.applicable}
    # Boolean requirements have rho in {-1, +1}; physical ones are in their units. The search
    # minimizes the sign-preserving squash so one huge margin can't dominate.
    worst = min((math.tanh(v) for v in rhos.values()), default=math.inf)
    return worst, rhos


def describe(faults: List[Fault]) -> str:
    return " + ".join(f"{f.kind}({f.target}{', ' + ', '.join(f'{k}={v:.3g}' for k, v in f.params.items()) if f.params else ''}) "
                      f"@{f.tick} for {f.duration}" for f in faults)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=60, help="random samples")
    ap.add_argument("--local", type=int, default=20, help="local-search steps around the worst sample")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-failsafe", action="store_true")
    ap.add_argument("--include-residual", action="store_true")
    ap.add_argument("--live", action="store_true", help="use BioSim through HybridPlant")
    args = ap.parse_args()

    space = dict(SPACE, **(RESIDUAL if args.include_residual else {}))
    rng = random.Random(args.seed)
    make_plant = None
    if args.live:
        from interface import BioSimPlant
        from interface.plant import HybridPlant
        make_plant = lambda: HybridPlant(plant=BioSimPlant())  # noqa: E731

    counterexamples: List[Tuple[float, List[Fault], Dict[str, float]]] = []
    worst_per_req: Dict[str, Tuple[float, str]] = {}
    best: Tuple[float, List[Fault]] = (math.inf, [])

    def consider(faults: List[Fault]) -> float:
        nonlocal best
        s, rhos = score(faults, not args.no_failsafe, make_plant)
        for rid, v in rhos.items():
            if rid not in worst_per_req or v < worst_per_req[rid][0]:
                worst_per_req[rid] = (v, describe(faults))
        if s < 0:
            counterexamples.append((s, faults, rhos))
        if s < best[0]:
            best = (s, faults)
        return s

    for _ in range(args.n):
        faults = [sample_fault(rng, space)]
        if rng.random() < 0.3:
            second = sample_fault(rng, space)
            if second.target != faults[0].target:
                faults.append(second)
        consider(faults)

    # stochastic local search (hill climbing on robustness) from the worst sample
    current_s, current = best
    for _ in range(args.local):
        if not current:
            break
        cand = [perturb(f, rng, space) for f in current]
        s = consider(cand)
        if s < current_s:
            current_s, current = s, cand

    print(f"{args.n} random + {args.local} local samples, failsafe={'off' if args.no_failsafe else 'on'}")
    print(f"\n{'req':7s} {'min rho':>10s}  worst input")
    for rid in HARD:
        if rid in worst_per_req:
            v, d = worst_per_req[rid]
            print(f"{rid:7s} {v:10.3f}  {d if v < math.inf else ''}")
    if counterexamples:
        print(f"\n{len(counterexamples)} counterexample(s):")
        for s, faults, rhos in sorted(counterexamples, key=lambda c: c[0])[:10]:
            bad = ", ".join(f"{k}={v:.3g}" for k, v in rhos.items() if v <= 0)
            print(f"  {describe(faults)}  ->  {bad}")
        return 1
    print("\nno counterexample found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
