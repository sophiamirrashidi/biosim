"""Closed-loop runs with the full safety stack, shared by the tests and scripts/sensor_safety_check.py.

    sensors (AtmosphereSensorLayer: lag, noise, injected faults)
      -> model-based monitor (AtmosphereTemperatureMonitor via MonitorInLoop: labels + fused values)
      -> controllers (irrigation, HVAC, CO2 hold)
      -> FailSafeSupervisor (SensorGuard + mode automaton + protocols P1-P6)
      -> actuators (AtmosphereActuatorLayer: injected actuator faults)
      -> plant
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from actuators import AtmosphereActuatorLayer
from controllers import HVACTemperatureController, IrrigationController
from estimators import AtmosphereTemperatureMonitor, MonitorInLoop
from interface import FakePlant, Fault, TickLogger, run
from interface.plant import HybridPlant
from sensors import AtmosphereSensorLayer, chain_sensor_layers

from .failsafe import FailSafeSupervisor


def hold_co2(obs: Dict[str, Any]) -> Dict[str, float]:
    """CO2 hold at 1200 ppm with 200 W of lamps (same as tests/test_fault_requirements.py)."""
    co2 = obs.get("gh.co2_ppm")
    mol = 0.0 if co2 is None else (1200.0 - co2) * 1e-6 * 2050.0
    return {"cmd.crop_power_W": 200.0, "cmd.co2_inject_mol": max(0.0, min(5.0, mol))}


# Derived requirement HSR-02 (found by scripts/sensor_safety_check.py): a heater losing 10-20 %
# of its output went unflagged at the monitor's default 0.8 ratio, the cabin cooled, and the
# monitor blamed the temperature sensor instead. Drawn and commanded power are both measured
# exactly, so a 5 % loss can be flagged without false alarms.
HEATER_DEGRADED_RATIO = 0.95


def safety_layers(failsafe: bool = True, seed: int = 42,
                  heater_degraded_ratio: float = HEATER_DEGRADED_RATIO, **failsafe_kw):
    """(sensor_layer, supervisor, failsafe_supervisor_or_None) for run()."""
    fs = FailSafeSupervisor(**failsafe_kw) if failsafe else None
    loop = MonitorInLoop(AtmosphereTemperatureMonitor(temp_sensor_sigma_C=0.15, temp_model_sigma_C=0.25,
                                                      temp_shift_C=1.0, temp_h_sigmas=4.0,
                                                      heater_degraded_ratio=heater_degraded_ratio),
                         inner_supervisor=fs)
    return chain_sensor_layers(AtmosphereSensorLayer(seed=seed), loop.sensor_layer), loop.supervisor, fs


def run_scenario(faults: Sequence[Fault], n_ticks: int = 460, failsafe: bool = True, seed: int = 42,
                 make_plant: Optional[Callable[[], Any]] = None,
                 **failsafe_kw) -> Tuple[List[Dict[str, Any]], Optional[FailSafeSupervisor]]:
    sensor_layer, supervisor, fs = safety_layers(failsafe, seed, **failsafe_kw)  # monitor config shared
    plant = make_plant() if make_plant else HybridPlant(plant=FakePlant(), sync_biosim_temperature=False)
    log = TickLogger()
    run(plant,
        {"irrigation": IrrigationController(), "hvac": HVACTemperatureController(), "atmo": hold_co2},
        n_ticks, sensor_layer=sensor_layer, supervisor=supervisor,
        actuator_layer=AtmosphereActuatorLayer(), fault_schedule=list(faults), logger=log)
    return log.records, fs
