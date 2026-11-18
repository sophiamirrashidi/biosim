"""Simulated quantum (PAR) sensor over each shelf: what the lamps actually deliver.

Measures crop.s{i}.ppf_delivered (umol/m2/s) and, like the atmosphere and water/power
sensors, replaces that key in the observations with the sensed value; the true value stays
in the truth log for scoring. The per-channel physics (baseline noise, clamp, and the shared
noise/bias/drift/stuck/dropout faults) is AtmosphereChannelSensor's, reused as is.

Default noise from the Apogee SQ-500 quantum sensor spec: repeatability < 0.5 %, so about
1.5 umol/m2/s at full lettuce light (295). Its +-5 % calibration uncertainty is a fixed
offset per sensor, so model it as a `bias` fault (offset ~ +-15 at full light), not noise.
Range 0-4000 umol/m2/s.
"""
from __future__ import annotations

from typing import Dict

from interface.loop import State, TickContext
from interface.schema import shelf_key

from .atmosphere import AtmosphereChannelSensor

PAR_MAX = 4000.0


def light_key(shelf: int = 0) -> str:
    return shelf_key(shelf, "ppf_delivered")


class LightSensorLayer:
    """Sensor layer for run() and chain_sensor_layers: one PAR channel per shelf."""

    def __init__(self, sigma_umol: float = 1.5, shelves: int = 1, seed: int = 0):
        self.channels: Dict[str, AtmosphereChannelSensor] = {
            light_key(i): AtmosphereChannelSensor(light_key(i), baseline_sigma=sigma_umol,
                                                  min_val=0.0, max_val=PAR_MAX, seed=seed + i)
            for i in range(shelves)
        }

    def reset(self) -> None:
        for ch in self.channels.values():
            ch.reset()

    def __call__(self, state: State, ctx: TickContext) -> State:
        obs = dict(state)
        for key, ch in self.channels.items():
            if state.get(key) is not None:
                obs[key] = ch.read(float(state[key]), ctx.tick, ctx.active_faults)
        return obs
