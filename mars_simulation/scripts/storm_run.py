"""Full-system dust storm runs: does the lighting controller protect yield and the greenhouse?

One lettuce cycle per scenario on HybridPlant (BioSim + thermal model), with every subsystem
in the loop: HVAC heater, lighting (fixed full or LightingController), irrigation with water
recycling, a CO2 hold, the PAR light sensor, lamp and atmosphere actuator faults, and the
power budget supervisor (heater first, then lamps, then recycler).

    python scripts/storm_run.py                 # live server
    python scripts/storm_run.py --fake          # HybridPlant(FakePlant): no crop growth from light
    python scripts/storm_run.py --only storm-early-fixed storm-early-ctl

A storm is four stacked SEVERE malfunctions on Solar_Array for --storm-days: solar drops to
about 6 % (checked live, about the 2018 Opportunity storm) and recovers when they clear.
Deaths come from BioSim's container log (see light_experiment.py), since a dead crop is
harvested at 0 kg and leaves no trace over REST.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from actuators import LAMP_KEY, AtmosphereActuatorLayer, LightingActuatorLayer, chain_actuator_layers  # noqa: E402
from controllers import HVACTemperatureController, IrrigationController, LightingController  # noqa: E402
from interface import BioSimClient, BioSimPlant, FakePlant, Fault, TickLogger, bindings, run  # noqa: E402
from interface.plant import HybridPlant  # noqa: E402
from sensors import LightSensorLayer, light_key  # noqa: E402
from supervisors import PowerBudgetSupervisor  # noqa: E402

import light_experiment  # noqa: E402  (BioSim log parsing for crop deaths)

N_TICKS = 700
STORM_DAYS = 14         # real Mars dust storms last weeks; the 2018 one about two months
RESERVE_WH = 5000.0
FULL_W = bindings.full_light_W(10.0, 295.14)
PAR = light_key(0)


def storm(start: int, days: float = STORM_DAYS) -> List[Fault]:
    return [Fault(tick=start, kind="malfunction", target="Solar_Array",
                  params={"intensity": "SEVERE_MALF", "length": "PERMANENT_MALF"}, duration=int(24 * days))
            for _ in range(4)]


@dataclass
class Scenario:
    name: str
    controller: bool                    # LightingController, else fixed full light
    faults: List[Fault] = field(default_factory=list)
    storm_start: Optional[int] = None


def scenarios(days: float = STORM_DAYS, late: int = 400) -> List[Scenario]:
    lamp_weak = Fault(tick=200, kind="efficiency", target=LAMP_KEY, params={"factor": 0.7}, duration=72)
    sensor_bias = Fault(tick=350, kind="bias", target=PAR, params={"offset": -15.0}, duration=72)
    return [
        Scenario("baseline", True),
        Scenario("storm-early-fixed", False, storm(100, days), 100),
        Scenario("storm-early-ctl", True, storm(100, days), 100),
        Scenario("storm-late-fixed", False, storm(late, days), late),
        Scenario("storm-late-ctl", True, storm(late, days), late),
        Scenario("lamp+sensor-faults", True, [lamp_weak, sensor_bias]),
    ]


def co2_hold(obs: Dict[str, Any]) -> Dict[str, float]:
    co2 = obs.get("gh.co2_ppm")
    mol = 0.0 if co2 is None else (1200.0 - co2) * 1e-6 * 2050.0
    return {"cmd.co2_inject_mol": max(0.0, min(5.0, mol))}


def run_scenario(make_plant: Callable, sc: Scenario, docker_log: bool) -> Dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    lighting = LightingController()
    statuses: List[Dict[str, Any]] = []

    def lamps(obs):
        if not sc.controller:
            return {LAMP_KEY: FULL_W}
        cmds = lighting(obs)
        statuses.append({"mode": lighting.mode, "gain": lighting.gain, "faults": list(lighting.status["lamp.faults"])})
        return cmds

    log = TickLogger()
    run(make_plant(), {"hvac": HVACTemperatureController(), "lamps": lamps,
                       "irrigation": IrrigationController(), "co2": co2_hold},
        N_TICKS, sensor_layer=LightSensorLayer(), supervisor=PowerBudgetSupervisor(RESERVE_WH),
        actuator_layer=chain_actuator_layers(AtmosphereActuatorLayer(), LightingActuatorLayer()),
        fault_schedule=sc.faults, logger=log)
    return score(log.records, statuses, sc, started if docker_log else None)


def score(records: List[Dict[str, Any]], statuses: List[Dict[str, Any]], sc: Scenario,
          log_since: Optional[str]) -> Dict[str, Any]:
    truth = [r["truth"] for r in records]
    maturity = truth[0]["crop.s0.harvest_interval"] * 24
    harvest_tick, harvest_kg = None, 0.0
    for prev, cur in zip(truth, truth[1:]):
        if cur["crop.biomass_store_kg"] > prev["crop.biomass_store_kg"]:
            harvest_tick, harvest_kg = int(cur["time.tick"]), cur["crop.biomass_store_kg"] - prev["crop.biomass_store_kg"]
            break
    death = None
    if log_since:
        lines = light_experiment.server_log_since(log_since)
        deaths = [d for d in light_experiment.parse_deaths(lines or []) if d[1] <= maturity]
        if deaths:
            death, harvest_tick, harvest_kg = deaths[0], deaths[0][1], 0.0
    grow = records[1:harvest_tick or len(records)]

    asked_heat = [r["commands"]["hvac"].get("cmd.heater_W", 0.0) for r in grow]
    got_heat = [r["applied"].get("cmd.heater_W", 0.0) for r in grow]
    temps = [r["truth"]["gh.temp_truth_C"] for r in grow]
    out = {
        "scenario": sc.name,
        "harvest_kg": harvest_kg,
        "death": death[0] if death else None,
        "mean_ppf": statistics.fmean(r["truth"][PAR] for r in grow),
        "dark_h": sum(r["truth"][PAR] < 1.0 for r in grow),
        "battery_min_kWh": min(r["truth"]["power.store_level"] for r in grow) / 1000.0,
        "heater_short_h": sum(g < a - 1.0 for a, g in zip(asked_heat, got_heat)),
        "temp_min": min(temps),
        "temp_max": max(temps),
        "lamp_kWh": sum(r["truth"]["power.crop_draw"] for r in grow) / 1000.0,
    }
    if statuses:
        out["survival_h"] = sum(s["mode"] == "SURVIVAL" for s in statuses[1:harvest_tick or None])
        for f in sc.faults:
            if f.kind == "malfunction":
                continue
            label = "lamp" if f.target == LAMP_KEY else "light_sensor_offset"
            hit = next((i for i, s in enumerate(statuses) if i >= f.tick and label in s["faults"]), None)
            out[f"{label}_caught_after_h"] = None if hit is None else hit - f.tick
            if label == "lamp":
                window = [r["truth"][PAR] for r in records[f.tick + 3:f.tick + f.duration]]
                out["light_during_lamp_fault"] = statistics.fmean(window)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8009")
    ap.add_argument("--fake", action="store_true", help="HybridPlant(FakePlant): no server, no growth from light")
    ap.add_argument("--only", nargs="+", help="scenario names to run")
    ap.add_argument("--storm-days", type=float, default=STORM_DAYS)
    ap.add_argument("--late-start", type=int, default=400, help="tick the late storm starts")
    args = ap.parse_args()

    if args.fake:
        make_plant = lambda: HybridPlant(plant=FakePlant(), sync_biosim_temperature=False)  # noqa: E731
    else:
        client = BioSimClient(args.url)
        try:
            client.list_sims()
        except Exception as e:
            print(f"Cannot reach BioSim at {args.url}: {e}\nStart it with: docker compose up biosim-server")
            return 1
        make_plant = lambda: HybridPlant(plant=BioSimPlant(client))  # noqa: E731

    results = []
    for sc in scenarios(args.storm_days, args.late_start):
        if args.only and sc.name not in args.only:
            continue
        r = run_scenario(make_plant, sc, docker_log=not args.fake)
        results.append(r)
        print(f"  {sc.name:20s} harvest {r['harvest_kg']:5.1f} kg" + (f"  DIED ({r['death']})" if r["death"] else ""))

    print(f"\n{'scenario':20s} {'kg':>5s} {'PPF':>5s} {'dark h':>6s} {'surv h':>6s} {'bat min':>7s} "
          f"{'heat short h':>12s} {'temp C':>11s} {'lamp kWh':>8s}")
    for r in results:
        print(f"{r['scenario']:20s} {r['harvest_kg']:5.1f} {r['mean_ppf']:5.0f} {r['dark_h']:6d} "
              f"{r.get('survival_h', '-'):>6} {r['battery_min_kWh']:7.1f} {r['heater_short_h']:12d} "
              f"{r['temp_min']:5.1f}-{r['temp_max']:<5.1f} {r['lamp_kWh']:8.0f}")
    for r in results:
        extra = {k: v for k, v in r.items() if k.endswith("caught_after_h") or k == "light_during_lamp_fault"}
        if extra:
            print(f"{r['scenario']}: " + ", ".join(f"{k} {v:.1f}" if isinstance(v, float) else f"{k} {v}" for k, v in extra.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
