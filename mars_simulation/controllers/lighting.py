"""Lighting controller for the lettuce bed: picks a brightness from battery charge and crop age.

Levels are fractions of full light (295 umol/m2/s, ~729 W on the LED shelf):
    FULL 1.0    REDUCED 0.75    SURVIVAL 0.30

Why age matters (results/light_experiment.md): a 72 h blackout costs 42 % of the yield at
tick 100 but only 5 % at tick 550, and the crop only died when light stayed at or below 10 %
for most of a cycle. So a young crop keeps full light down to a low battery, a crop near
harvest is dimmed first, and SURVIVAL (30 %) stays well above the death threshold.

Battery state of charge (power.store_level / power.store_capacity) is compared with
per-phase thresholds. Dimming happens as soon as charge falls below a threshold; brightening
again needs `band` extra charge, so the lamps don't flicker when charge hovers at a threshold.
Crop age restarts at each harvest (crop.biomass_store_kg going up). A missing battery
reading keeps the current level.

Lamp faults: a LampMonitor compares the power the lamps drew with what was asked. When it
flags a lamp fault with ratio r (fraction of the power getting through, e.g. 0.6 for failed
LED drivers), the request is multiplied by 1/r, capped at max_gain = 1.5: the real fixtures
are 1.5x the 'average lamp' power (2/3 duty for the 16 h photoperiod). When the fault
clears the gain returns to 1. The correction follows measured power, not the light sensor:
light saturates at full, so a sensor-driven boost would never come back down, and a biased
sensor can't push the lamps around. A dead circuit (r = 0) is flagged, not compensated.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from estimators import LampMonitor
from interface import bindings

LEVELS = (("FULL", 1.0), ("REDUCED", 0.75), ("SURVIVAL", 0.30))

# (end of phase as a fraction of the cycle, charge below which to go REDUCED, below which SURVIVAL)
PHASES = (
    (0.40, 0.25, 0.12),   # young: light now sets canopy closure, protect it
    (0.75, 0.40, 0.20),   # middle
    (1.01, 0.55, 0.30),   # near harvest: light matters least, dim first
)


class LightingController:
    def __init__(self, crop_area_m2: float = 10.0, band: float = 0.05,
                 default_capacity_Wh: float = 200000.0, shelf: int = 0,
                 max_gain: float = 1.5, monitor: Optional[LampMonitor] = None):
        self.crop_area_m2 = float(crop_area_m2)
        self.max_gain = float(max_gain)
        self.monitor = monitor or LampMonitor(crop_area_m2=crop_area_m2, shelf=shelf)
        self.band = float(band)
        self.default_capacity_Wh = float(default_capacity_Wh)
        self.prefix = f"crop.s{shelf}."
        self.reset()

    def reset(self) -> None:
        self.level = 0                       # index into LEVELS
        self.planted_tick = 0
        self.age_frac = 0.0
        self.soc: Optional[float] = None
        self._prev_biomass: Optional[float] = None
        self._capacity = self.default_capacity_Wh
        self.gain = 1.0
        self.status: Dict[str, Any] = {}
        self._asked: Optional[float] = None
        self.monitor.reset()

    @property
    def mode(self) -> str:
        return LEVELS[self.level][0]

    def thresholds(self) -> Tuple[float, float]:
        for end, reduced, survival in PHASES:
            if self.age_frac < end:
                return reduced, survival
        return PHASES[-1][1:]

    def _update_age(self, obs: Dict[str, Any]) -> None:
        tick = int(obs.get("time.tick", 0))
        biomass = obs.get("crop.biomass_store_kg")
        if biomass is not None:
            if self._prev_biomass is not None and biomass > self._prev_biomass:
                self.planted_tick = tick
            self._prev_biomass = biomass
        cycle_ticks = 24.0 * float(obs.get(self.prefix + "harvest_interval") or 28.0)
        self.age_frac = max(0.0, (tick - self.planted_tick) / cycle_ticks)

    def _choose(self, soc: float) -> int:
        reduced, survival = self.thresholds()
        want = 0 if soc >= reduced else 1 if soc >= survival else 2
        if want >= self.level:
            return want                      # dimming (or staying): immediately
        # brightening: only as far as charge clears the threshold plus the band
        if soc >= reduced + self.band:
            return 0
        if soc >= survival + self.band:
            return min(self.level, 1)
        return self.level

    def __call__(self, obs: Dict[str, Any]) -> Dict[str, float]:
        self._update_age(obs)
        capacity = obs.get("power.store_capacity")
        if capacity:
            self._capacity = float(capacity)
        level_Wh = obs.get("power.store_level")
        if level_Wh is not None and self._capacity > 0:
            self.soc = float(level_Wh) / self._capacity
            self.level = self._choose(self.soc)
        self._update_gain(obs)
        full_W = bindings.full_light_W(self.crop_area_m2, float(obs.get(self.prefix + "ppf_needed") or 295.14))
        self._asked = LEVELS[self.level][1] * full_W * self.gain
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
