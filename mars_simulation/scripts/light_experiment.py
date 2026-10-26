"""Open-loop light experiment: how lettuce yield and survival respond to light in BioSim.

Runs one lettuce cycle per condition with the lamps on a fixed schedule and everything
else held steady, then reports harvest mass and whether the crop died first.

    python scripts/light_experiment.py [--groups level photoperiod blackout] [--reps 2]

Groups:
  level        constant PPF as a fraction of what the crop needs
  photoperiod  full PPF, lamps off for N hours at the end of every sol
  blackout     full PPF except one outage of N hours starting at a given tick

CO2 is held near CO2_TARGET_PPM by a proportional injector. Without it the crop pulls the
greenhouse to 0 ppm within ~12 sols and CO2, not light, limits growth. This hold is part of
the experiment rig, not the atmosphere controller.

BioSim does not report crop death over REST. With autoHarvestAndReplant a dead crop is
harvested at once, so a harvest before maturity (harvest_interval days) counts as a death.
But Plant.kill() resets the crop before that harvest, so it adds 0 kg and nothing in the
REST state changes. The ground truth is BioSim's own log: after each run the script reads
"died from ..." and "Harvested ..." lines from the container (skip with --no-docker-log).
Death is random in BioSim (Plant.java), so each condition runs --reps times.
"""
from __future__ import annotations

import argparse
import csv
import re
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from interface import BioSimClient, BioSimPlant, FakePlant, TickLogger, run, shelf_key  # noqa: E402
from interface import bindings  # noqa: E402

CROP_AREA_M2 = 10.0
PPF_NEEDED = 295.14
# lamp power at which BioSim's shelf delivers exactly PPF_NEEDED (~2.42 W for 10 m2)
FULL_LAMP_W = PPF_NEEDED * CROP_AREA_M2 / bindings.LAMP_PPF_PER_W
CO2_TARGET_PPM = 1200.0
GH_TOTAL_MOLES = 2050.0
WATER_L = 20.0
N_TICKS = 700        # one 672-tick lettuce cycle plus margin for the harvest to show up
COMPOSE_FILE = ROOT.parent / "docker-compose.yml"


@dataclass
class Condition:
    group: str
    name: str
    level: float = 1.0                  # fraction of FULL_LAMP_W while lamps are on
    off_hours: float = 0.0              # lamps off this many hours at the end of each sol
    blackout_start: Optional[int] = None
    blackout_len: int = 0


@dataclass
class Result:
    group: str
    name: str
    rep: int
    harvest_tick: Optional[int]
    harvest_kg: float
    died: bool
    death_cause: Optional[str]          # from the BioSim log; None if no log was read
    death_tick: Optional[int]
    mean_ppf: float
    mean_co2_ppm: float
    min_co2_ppm: float


def lamp_fraction(c: Condition, tick: int, hour_of_day: float, day_length: float) -> float:
    if c.blackout_start is not None and c.blackout_start <= tick < c.blackout_start + c.blackout_len:
        return 0.0
    if c.off_hours and hour_of_day >= day_length - c.off_hours:
        return 0.0
    return c.level


def conditions(groups: List[str]) -> List[Condition]:
    out: List[Condition] = []
    if "level" in groups:
        for f in [1.0, 0.9, 0.75, 0.5, 0.25, 0.1, 0.0]:
            out.append(Condition("level", f"level={f:.2f}", level=f))
    if "photoperiod" in groups:
        for h in [0, 4, 8, 12, 16, 20]:
            out.append(Condition("photoperiod", f"off={h}h", off_hours=h))
    if "blackout" in groups:
        for start in [100, 336, 550]:
            for length in [24, 72, 168, 336]:
                if start + length <= 672:
                    out.append(Condition("blackout", f"start={start} len={length}h",
                                         blackout_start=start, blackout_len=length))
    return out


def controllers(c: Condition, day_length: float) -> Dict[str, Callable]:
    def lamps(obs):
        f = lamp_fraction(c, int(obs["time.tick"]), obs["time.hour_of_day"], day_length)
        return {"cmd.crop_power_W": f * FULL_LAMP_W}

    def co2_hold(obs):
        mol = (CO2_TARGET_PPM - obs["gh.co2_ppm"]) * 1e-6 * GH_TOTAL_MOLES
        return {"cmd.co2_inject_mol": max(0.0, min(5.0, mol))}

    def water(obs):
        return {"cmd.crop_water_L": WATER_L}

    return {"lamps": lamps, "co2_hold": co2_hold, "water": water}


_DIED = re.compile(r"crops have died from (.+?) at ")
_HARVESTED = re.compile(r"Harvested ([\d.Ee+-]+)kg .* on tick (\d+)")


def parse_deaths(lines: List[str]) -> List[Tuple[str, int]]:
    """(cause, tick) for each death in BioSim log lines. Plant logs the death without a
    tick; the Shelf harvest line right after it carries the tick."""
    deaths, pending = [], None
    for line in lines:
        m = _DIED.search(line)
        if m:
            pending = m.group(1)
            continue
        m = _HARVESTED.search(line)
        if m and pending:
            deaths.append((pending, int(m.group(2))))
            pending = None
    return deaths


def server_log_since(since: str) -> Optional[List[str]]:
    try:
        out = subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "logs", "--no-color",
                              "--since", since, "biosim-server"],
                             capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.splitlines()


def run_condition(plant, c: Condition, rep: int = 0, day_length: float = 24.66,
                  docker_log: bool = False) -> Result:
    started = datetime.now(timezone.utc).isoformat()
    log = TickLogger()
    final = run(plant, controllers(c, day_length), N_TICKS, logger=log)
    states = [r["truth"] for r in log.records] + [final]
    maturity_tick = states[0][shelf_key(0, "harvest_interval")] * 24

    harvest_tick, harvest_kg = None, 0.0
    for prev, cur in zip(states, states[1:]):
        gained = cur["crop.biomass_store_kg"] - prev["crop.biomass_store_kg"]
        if gained > 0:
            harvest_tick, harvest_kg = int(cur["time.tick"]), gained
            break
    died = harvest_tick is not None and harvest_tick < maturity_tick
    death_cause, death_tick = None, None
    lines = server_log_since(started) if docker_log else None
    if lines is not None:
        # only the first crop; a replanted crop dying after the harvest doesn't count
        deaths = [d for d in parse_deaths(lines) if d[1] <= maturity_tick]
        if deaths:
            # kill() resets the crop before the shelf harvests it, so a death yields 0 kg
            died, (death_cause, death_tick) = True, deaths[0]
            harvest_tick, harvest_kg = death_tick, 0.0
    elif docker_log:
        print("  warning: could not read the BioSim container log; 0 kg deaths will be missed")

    grown = states[:harvest_tick] if harvest_tick else states
    co2 = [s["gh.co2_ppm"] for s in grown]
    return Result(
        group=c.group, name=c.name, rep=rep,
        harvest_tick=harvest_tick, harvest_kg=harvest_kg,
        died=died, death_cause=death_cause, death_tick=death_tick,
        mean_ppf=statistics.fmean(s[shelf_key(0, "ppf_delivered")] for s in grown),
        mean_co2_ppm=statistics.fmean(co2), min_co2_ppm=min(co2),
    )


def summarize(results: List[Result]) -> None:
    by_cond: Dict[tuple, List[Result]] = {}
    for r in results:
        by_cond.setdefault((r.group, r.name), []).append(r)
    print(f"\n{'condition':28s} {'mean PPF':>9s} {'harvest kg':>18s} {'deaths':>7s} {'harvest ticks':>16s}  causes")
    for (group, name), rs in by_cond.items():
        kgs = [r.harvest_kg for r in rs]
        ticks = ",".join(str(r.harvest_tick) for r in rs)
        kg = f"{statistics.fmean(kgs):6.2f} ({min(kgs):.1f}-{max(kgs):.1f})"
        causes = ",".join(sorted({r.death_cause for r in rs if r.death_cause}))
        print(f"{name:28s} {statistics.fmean(r.mean_ppf for r in rs):9.1f} {kg:>18s} "
              f"{sum(r.died for r in rs)}/{len(rs):<5d} {ticks:>16s}  {causes}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8009")
    ap.add_argument("--groups", nargs="+", default=["level", "photoperiod", "blackout"],
                    choices=["level", "photoperiod", "blackout"])
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--out", type=Path, default=ROOT / "logs" / "light_experiment.csv")
    ap.add_argument("--fake", action="store_true", help="run against FakePlant (no server)")
    ap.add_argument("--no-docker-log", action="store_true",
                    help="don't read deaths from the container log (misses 0 kg deaths)")
    args = ap.parse_args()
    docker_log = not (args.fake or args.no_docker_log)

    if args.fake:
        plant = FakePlant()
    else:
        client = BioSimClient(args.url)
        try:
            client.list_sims()
        except Exception as e:
            print(f"Cannot reach BioSim at {args.url}: {e}\nStart it with: docker compose up biosim-server")
            return 1
        plant = BioSimPlant(client)

    conds = conditions(args.groups)
    print(f"{len(conds)} conditions x {args.reps} reps, full lamp power {FULL_LAMP_W:.3f} W")
    results = []
    for c in conds:
        for rep in range(args.reps):
            r = run_condition(plant, c, rep, docker_log=docker_log)
            results.append(r)
            print(f"  {c.name:28s} rep {rep}: {r.harvest_kg:6.2f} kg at tick {r.harvest_tick}"
                  f"{f'  DIED ({r.death_cause})' if r.died else ''}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(results[0])))
        w.writeheader()
        w.writerows(asdict(r) for r in results)
    summarize(results)
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
