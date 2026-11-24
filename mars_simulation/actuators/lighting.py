"""Lamp actuator faults: what actually reaches the lettuce bed's lamps.

Reuses AtmosphereActuatorLayer's fault logic for cmd.crop_power_W. A failed LED driver string
is an `efficiency` fault (factor = fraction of the commanded power that gets through), a lamp
relay welded shut or open is `stuck` (value in W), and a dead lamp circuit is `dropout`.
BioSim turns the power that reaches the shelf straight into light (Shelf.lightPlants), so
each of these lowers both power.crop_draw and the delivered light.

Chain it with the atmosphere layer through chain_actuator_layers.
"""
from __future__ import annotations

from .atmosphere import AtmosphereActuatorLayer

LAMP_KEY = "cmd.crop_power_W"
LAMP_MAX_W = 15200.0   # most the shelf draws (1520 W/m2 x 10 m2); see Shelf.getPowerNeeded


def LightingActuatorLayer(max_W: float = LAMP_MAX_W) -> AtmosphereActuatorLayer:
    return AtmosphereActuatorLayer(limits={LAMP_KEY: float(max_W)}, keys=(LAMP_KEY,))
