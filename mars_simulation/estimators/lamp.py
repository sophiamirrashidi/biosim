"""Checks the lamps and the light sensor against each other and against the power the lamps drew.

Two independent clues, so a weak lamp and a lying sensor can be told apart:

    lamp fault          the power the lamps drew (power.crop_draw) differs from what the
                        controller asked for, by more than lamp_tol (fraction) and lamp_tol_W,
                        for lamp_ticks ticks in a row. Reports the ratio drawn / asked
                        (0.6 for an `efficiency` 0.6 fault, 0 for a dead circuit). Paused while
                        the battery is below min_battery_Wh: the power supervisor may be
                        cutting the lamps on purpose then.
    light sensor fault  the PAR reading (crop.s{i}.ppf_delivered) disagrees with the light the
                        drawn power gives (bindings.delivered_ppf). This is the same sensor-vs-
                        model problem as the root zone, so RootZoneMonitor does it: dropout,
                        stuck (exact repeats; readings pinned at 0 with the lamps off don't
                        count) and offset (CUSUM on the residual).

Timing: the state at tick k reports the draw during the step k-1 -> k, which the command
issued at k-1 drove; pass that command as asked_W, like ReservoirEstimator's cap_L.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from interface import bindings

from .root_zone import RootZoneMonitor

PAR_MAX = 4000.0

_SENSOR_FAULTS = {"dropout": "light_sensor_dropout", "stuck": "light_sensor_stuck",
                  "offset": "light_sensor_offset", "estimator": "power_reading_missing"}


class LampMonitor:
    def __init__(self, sensor_sigma: float = 1.5, model_sigma: float = 1.0, shift: float = 10.0,
                 crop_area_m2: float = 10.0, ppf_per_W: float = bindings.LED_PPF_PER_W,
                 lamp_tol: float = 0.05, lamp_tol_W: float = 5.0, lamp_ticks: int = 2,
                 min_battery_Wh: float = 20000.0, shelf: int = 0):
        self.sensor_check = RootZoneMonitor(sensor_sigma_L=sensor_sigma, estimate_sigma_L=model_sigma,
                                            shift_L=shift, capacity_L=PAR_MAX)
        self.crop_area_m2 = float(crop_area_m2)
        self.ppf_per_W = float(ppf_per_W)
        self.lamp_tol = float(lamp_tol)
        self.lamp_tol_W = float(lamp_tol_W)
        self.lamp_ticks = int(lamp_ticks)
        self.min_battery_Wh = float(min_battery_Wh)
        self.prefix = f"crop.s{shelf}."
        self.reset()

    def reset(self) -> None:
        self.sensor_check.reset()
        self._mismatch = 0
        self.ratio: Optional[float] = None

    def _check_lamps(self, draw: Optional[float], asked_W: Optional[float],
                     battery: Optional[float]) -> bool:
        if draw is None or asked_W is None or (battery is not None and battery < self.min_battery_Wh):
            self._mismatch = 0
            return False
        if abs(draw - asked_W) > max(self.lamp_tol * asked_W, self.lamp_tol_W):
            self._mismatch += 1
            self.ratio = draw / asked_W if asked_W > self.lamp_tol_W else None
        else:
            self._mismatch = 0
            self.ratio = None
        return self._mismatch >= self.lamp_ticks

    def update(self, obs: Dict[str, Any], asked_W: Optional[float]) -> Dict[str, Any]:
        draw = obs.get("power.crop_draw")
        lamp_fault = self._check_lamps(draw, asked_W, obs.get("power.store_level"))

        expected = None
        if draw is not None:
            expected = bindings.delivered_ppf(float(draw), self.crop_area_m2,
                                              float(obs.get(self.prefix + "ppf_needed") or 295.14), self.ppf_per_W)
        out = self.sensor_check.update(obs.get(self.prefix + "ppf_delivered"),
                                       {"est.reservoir_L": expected or 0.0, "est.degraded": expected is None})
        faults = (["lamp"] if lamp_fault else []) + [_SENSOR_FAULTS[f] for f in out["rz.faults"]]
        return {
            "lamp.faults": faults,
            "lamp.ratio": self.ratio if lamp_fault else None,
            "lamp.sensor_ok": not any(f.startswith("light_sensor") for f in faults),
            "lamp.ppf": out["rz.level_L"],          # best estimate of the light the crop got
            "lamp.expected_ppf": expected,
            "lamp.residual": out["rz.residual_L"],
        }
