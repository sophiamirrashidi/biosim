"""Lighting controller for the lettuce bed: dims the lamps only when that actually saves energy
the greenhouse will need, and least for a young crop.

Levels are fractions of full light (295 umol/m2/s, ~729 W on the LED shelf):
    FULL 1.0    REDUCED 0.75    SURVIVAL 0.30

Two findings from full-system storm runs (results/storm_run.md) shape it:
  - Lamp power is also greenhouse heat (90 % of it, thermal model). While the heater is running,
    dimming only makes the heater replace that heat: about 10 % saved, all the light lost. So
    the lamps dim only while the heater is idle (power.heater_draw below heater_idle_W).
  - Dimming on battery charge alone cost 3.5 kg in a 14-day storm the battery rode out anyway.
    So the trigger is how long the battery will last: the drain over the last window_h ticks
    (a full sol, so day and night even out) against the charge above the supervisor's reserve.

Hours left are compared with a horizon that depends on crop age (results/light_experiment.md:
a 72 h blackout costs 42 % of the yield at tick 100 but 5 % at tick 550): a young crop dims
only when the battery is close to empty, a crop near harvest well before. Below the horizon the
lamps go REDUCED, below half of it SURVIVAL (30 %, well above the 10 % at which crops died).
Brightening needs band_h more hours, so the level doesn't flicker. Crop age restarts at each
harvest (crop.biomass_store_kg going up). With no battery reading, or less than half a window
of history, the level is kept.

Lamp faults: a LampMonitor compares the power the lamps drew with what was asked. When it
flags a lamp fault with ratio r (fraction of the power getting through, e.g. 0.6 for failed
LED drivers), the request is multiplied by 1/r, capped at max_gain = 1.5: the real fixtures
are 1.5x the 'average lamp' power (2/3 duty for the 16 h photoperiod). When the fault
clears the gain returns to 1. The correction follows measured power, not the light sensor:
light saturates at full, so a sensor-driven boost would never come back down, and a biased
sensor can't push the lamps around. A dead circuit (r = 0) is flagged, not compensated.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, Optional

from estimators import LampMonitor
from interface import bindings

LEVELS = (("FULL", 1.0), ("REDUCED", 0.75), ("SURVIVAL", 0.30))

# (end of phase as a fraction of the cycle, hours of battery left below which to dim)
PHASES = (
    (0.40, 48.0),    # young: light now sets canopy closure, protect it
    (0.75, 96.0),    # middle
    (1.01, 168.0),   # near harvest: light matters least, dim first
)


class LightingController:
    def __init__(self, crop_area_m2: float = 10.0, reserve_Wh: float = 5000.0, window_h: int = 24,
                 band_h: float = 12.0, heater_idle_W: float = 50.0, shelf: int = 0,
                 max_gain: float = 1.5, monitor: Optional[LampMonitor] = None):
        self.crop_area_m2 = float(crop_area_m2)
        self.reserve_Wh = float(reserve_Wh)        # match the power supervisor's
        self.window_h = int(window_h)
        self.band_h = float(band_h)
        self.heater_idle_W = float(heater_idle_W)
        self.max_gain = float(max_gain)
        self.monitor = monitor or LampMonitor(crop_area_m2=crop_area_m2, shelf=shelf)
        self.prefix = f"crop.s{shelf}."
        self.reset()

    def reset(self) -> None:
        self.level = 0                       # index into LEVELS
        self.planted_tick = 0
        self.age_frac = 0.0
        self.hours_left: Optional[float] = None
        self.heater_on = False
        self._prev_biomass: Optional[float] = None
        self._battery = deque(maxlen=self.window_h + 1)
        self.gain = 1.0
        self.status: Dict[str, Any] = {}
        self._asked: Optional[float] = None
        self.monitor.reset()

    @property
    def mode(self) -> str:
        return LEVELS[self.level][0]

    def horizon_h(self) -> float:
        for end, hours in PHASES:
            if self.age_frac < end:
                return hours
        return PHASES[-1][1]

    def _update_age(self, obs: Dict[str, Any]) -> None:
        tick = int(obs.get("time.tick", 0))
        biomass = obs.get("crop.biomass_store_kg")
        if biomass is not None:
            if self._prev_biomass is not None and biomass > self._prev_biomass:
                self.planted_tick = tick
            self._prev_biomass = biomass
        cycle_ticks = 24.0 * float(obs.get(self.prefix + "harvest_interval") or 28.0)
        self.age_frac = max(0.0, (tick - self.planted_tick) / cycle_ticks)

    def _update_hours_left(self, level_Wh: Optional[float]) -> None:
        if level_Wh is None:
            return
        self._battery.append(float(level_Wh))
        if len(self._battery) <= self.window_h // 2:
            return                           # too little history to tell a storm from a night
        drain_W = (self._battery[0] - self._battery[-1]) / (len(self._battery) - 1)
        above = max(0.0, self._battery[-1] - self.reserve_Wh)
        self.hours_left = above / drain_W if drain_W > 0 else float("inf")

    def _choose(self) -> int:
        if self.hours_left is None:
            return self.level
        horizon = self.horizon_h()
        want = 0 if self.hours_left >= horizon else 1 if self.hours_left >= horizon / 2 else 2
        if want >= self.level:
            return want                      # dimming (or staying): immediately
        # brightening: only as far as the hours left clear the threshold plus the band
        if self.hours_left >= horizon + self.band_h:
            return 0
        if self.hours_left >= horizon / 2 + self.band_h:
            return min(self.level, 1)
        return self.level

    def __call__(self, obs: Dict[str, Any]) -> Dict[str, float]:
        self._update_age(obs)
        self._update_hours_left(obs.get("power.store_level"))
        self.level = self._choose()
        heater = obs.get("power.heater_draw")
        self.heater_on = heater is not None and float(heater) > self.heater_idle_W
        self._update_gain(obs)
        full_W = bindings.full_light_W(self.crop_area_m2, float(obs.get(self.prefix + "ppf_needed") or 295.14))
        # with the heater on, lamp heat replaces heater heat: dimming would save ~10 % and lose light
        fraction = 1.0 if self.heater_on else LEVELS[self.level][1]
        self._asked = fraction * full_W * self.gain
        return {"cmd.crop_power_W": self._asked}

    def _update_gain(self, obs: Dict[str, Any]) -> None:
        # the state reports the draw during the last step, which the last request drove
        self.status = self.monitor.update(obs, self._asked)
        ratio = self.status["lamp.ratio"]
        if "lamp" not in self.status["lamp.faults"]:
            self.gain = 1.0
        elif ratio is not None and ratio > 0:
            self.gain = min(self.max_gain, max(1.0, 1.0 / ratio))
        # a dead circuit (ratio 0) keeps the gain: more power can't help, the fault is flagged
