"""Closed-loop check of the sensor safety requirements (safety.requirements) on FakePlant.

Requirements-based testing (CSCI 513 lecture 7): each scenario is one test input (a fault
schedule), the output is the logged run, and the property is every requirement in
sensor_safety_requirements.md evaluated with STL robustness. A test passes only if every
applicable requirement has rho > 0.

The fault starts at tick 300 (crop using water, recycler running) and lasts 72 ticks; runs are
460 ticks so the 30-tick return-to-NOMINAL deadline (SSR-25) fits inside the trace.
"""
from __future__ import annotations

from typing import List

import pytest

from interface import Fault
from safety.harness import run_scenario
from safety.requirements import evaluate, report

START, LENGTH = 300, 72


def fault(kind: str, target: str, **params) -> List[Fault]:
    return [Fault(tick=START, kind=kind, target=target, params=params, duration=LENGTH)]


SCENARIOS = {
    "healthy": [],
    "temp_bias": fault("bias", "gh.temp_C", offset=3.5),
    "temp_negative_bias": fault("bias", "gh.temp_C", offset=-3.5),
    "temp_drift": fault("drift", "gh.temp_C", rate=0.2),
    "temp_stuck": fault("stuck", "gh.temp_C"),
    "temp_dropout": fault("dropout", "gh.temp_C"),
    "co2_bias": fault("bias", "gh.co2_ppm", offset=-400.0),
    "co2_stuck": fault("stuck", "gh.co2_ppm"),
    "co2_dropout": fault("dropout", "gh.co2_ppm"),
    "o2_bias": fault("bias", "gh.o2_frac", offset=-0.02),
    "o2_stuck": fault("stuck", "gh.o2_frac"),
    "pressure_bias": fault("bias", "gh.pressure_kPa", offset=5.0),
    "pressure_dropout": fault("dropout", "gh.pressure_kPa"),
    "heater_burnout": fault("stuck", "cmd.heater_W", value=0.0),
    "heater_half_output": fault("efficiency", "cmd.heater_W", factor=0.5),
    "temp_and_co2_dropout": fault("dropout", "gh.temp_C") + fault("dropout", "gh.co2_ppm"),
    "heater_burnout_and_temp_dropout": fault("stuck", "cmd.heater_W", value=0.0) + fault("dropout", "gh.temp_C"),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_all_requirements_hold(name):
    records, fs = run_scenario(SCENARIOS[name])
    results = evaluate(records, fs)
    failed = [r for r in results if not r.passed]
    assert not failed, f"{name}:\n{report(results)}"


def test_healthy_run_never_leaves_nominal():
    records, fs = run_scenario([])
    assert {h["mode"] for h in fs.history.values()} == {"NOMINAL"}


def test_without_failsafe_heater_burnout_violates_survival():
    """Control: the same burnout without the supervisor freezes the cabin, so SSR-01/SSR-23
    passing above is the fail-safe's doing, not the scenario being easy."""
    records, _ = run_scenario(SCENARIOS["heater_burnout"], failsafe=False)
    by_id = {r.req.id: r for r in evaluate(records, None)}
    assert by_id["SSR-01"].rho < 0 and by_id["SSR-23"].rho < 0


def test_without_failsafe_co2_bias_is_injected_on_an_untrusted_reading():
    records, _ = run_scenario(SCENARIOS["co2_bias"], failsafe=False)
    by_id = {r.req.id: r for r in evaluate(records, None)}
    assert by_id["SSR-22"].rho < 0


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="residual hazard: a valve stuck open ignores every command; needs the "
                          "hardware isolation valve of HSR-01 (sensor_safety_requirements.md 6)")
def test_co2_injector_stuck_open_is_contained():
    records, fs = run_scenario(fault("stuck", "cmd.co2_inject_mol", value=0.5))
    results = evaluate(records, fs, ids=["SSR-02"])
    assert all(r.passed for r in results), report(results)


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="finding F-3: AtmosphereTemperatureMonitor skips its temperature bias test "
                          "while it reports heater_burnout, so a concurrent sensor bias goes unflagged")
def test_temperature_bias_during_heater_burnout_is_detected():
    # counterexample from scripts/sensor_safety_check.py --seed 2
    faults = [Fault(tick=334, kind="bias", target="gh.temp_C", params={"offset": 4.58}, duration=80),
              Fault(tick=332, kind="stuck", target="cmd.heater_W", params={"value": 0.0}, duration=21)]
    records, fs = run_scenario(faults, n_ticks=454)
    by_id = {r.req.id: r for r in evaluate(records, fs)}
    assert by_id["SSR-01"].passed, "cabin survival must hold even here"  # holds today
    assert by_id["SSR-10"].passed, report(list(by_id.values()))
