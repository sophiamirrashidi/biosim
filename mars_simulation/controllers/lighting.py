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
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

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
                 default_capacity_Wh: float = 200000.0, shelf: int = 0):
        self.crop_area_m2 = float(crop_area_m2)
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
        full_W = bindings.full_light_W(self.crop_area_m2, float(obs.get(self.prefix + "ppf_needed") or 295.14))
        return {"cmd.crop_power_W": LEVELS[self.level][1] * full_W}
