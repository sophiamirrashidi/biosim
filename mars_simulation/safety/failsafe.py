"""Sensor validation and fail-safe supervision for the Mars greenhouse.

Two pieces, both pure Python and both usable with interface.loop.run():

SensorGuard
    Per-channel plausibility checks that do not need a physics model: missing/NaN value,
    reading outside what the measurand can physically do, a tick-to-tick jump faster than the
    physics allows, and staleness (no valid reading for too long). It also folds in the labels
    the model-based monitors already raise (temp_faults, co2_faults, ..., rz.faults). A channel
    is *healthy* only if every check passes. The guard keeps the last known good (LKG) value
    and its age for each channel.

FailSafeSupervisor
    A supervisor (the run() hook) that wraps another supervisor. It is a hybrid automaton
    (lecture 3) with four discrete modes, ordered by severity:

        NOMINAL  -> DEGRADED -> SAFE_HOLD -> EMERGENCY

    Escalation is immediate (the guard is evaluated every tick). De-escalation goes down one
    level at a time and only after the lower mode's condition has held for `clear_ticks`
    consecutive ticks. That dwell time makes every execution non-Zeno (at most one downward
    switch per clear_ticks ticks) and stops the mode chattering on a noisy threshold.

    In each mode it applies the fail-safe protocols (P1-P6 in sensor_safety_requirements.md)
    to the commands the controllers asked for, then sanitizes every command into its range in
    interface.schema.COMMAND_KEYS.

The emergency heat protocol uses a discrete-time control barrier function (lecture 10,
barrier certificates) on the habitat's own exponential thermal model:

    T+ = T_eq + (T - T_eq) d,   d = exp(-UA dt / C),   T_eq = T_ext + Q / UA
    h(T) = T - T_floor,         require h(T+) >= (1 - gamma) h(T),   0 < gamma <= 1

which, solved for the heat input Q, gives the minimum heat that keeps {T >= T_floor} forward
invariant (and pulls T back above T_floor geometrically if it is already below):

    Q >= UA * ( (T_floor + (1-gamma)(T - T_floor) - d T) / (1 - d)  -  T_ext_worst )

T_ext_worst is the coldest exterior temperature over the coming tick, so the bound holds for
the whole step, not only its start.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from interface.loop import merge_commands
from interface.schema import COMMAND_KEYS
from thermal.model import GreenhouseThermalModel


class Mode(IntEnum):
    NOMINAL = 0
    DEGRADED = 1
    SAFE_HOLD = 2
    EMERGENCY = 3


@dataclass(frozen=True)
class ChannelSpec:
    """Plausibility envelope of one sensor channel.

    lo/hi       physically possible range of the measurand (not the alarm band)
    max_step    largest change between two consecutive ticks the physics allows
    stale_ticks a channel with no valid reading for longer than this is lost, not just noisy
    flag_keys   obs keys where model-based monitors put fault labels for this channel
    critical    a control loop depends on it (losing it changes what actuators may do)
    """
    key: str
    lo: float
    hi: float
    max_step: float
    stale_ticks: int = 3
    flag_keys: Tuple[str, ...] = ()
    critical: bool = False


# Bounds come from the physics in thermal/model.py and interface/plant.py:
#  - cabin temperature: the Mars exterior swings -120..+20 °C; with every heat source on
#    (5 kW heater + 0.9 x 15.2 kW lamps) the cabin settles far below 80 °C. Max rise per
#    hour is (Q_max + UA*140 K) * 3600 / C_th, about 40 °C, so 40 °C/tick is a hard ceiling.
#  - CO2: 5 mol/tick injector cap into 2050 mol of air is ~2400 ppm/tick.
#  - O2 and pressure: the crop moves ~0.3 mol/tick of 2050 mol; a 1 %/tick or 10 kPa/tick
#    change is not physical without a hull breach, which pressure would also show.
DEFAULT_CHANNELS: Tuple[ChannelSpec, ...] = (
    ChannelSpec("gh.temp_C", -130.0, 80.0, 40.0, stale_ticks=2,
                flag_keys=("temp_faults",), critical=True),
    ChannelSpec("gh.co2_ppm", 0.0, 50000.0, 2500.0, stale_ticks=2,
                flag_keys=("co2_faults",), critical=True),
    ChannelSpec("gh.o2_frac", 0.0, 1.0, 0.01, stale_ticks=3, flag_keys=("o2_faults",)),
    ChannelSpec("gh.pressure_kPa", 0.0, 200.0, 10.0, stale_ticks=3,
                flag_keys=("pressure_faults",), critical=True),
    ChannelSpec("crop.s0.root_zone_L", 0.0, 500.0, 60.0, stale_ticks=6, flag_keys=("rz.faults",)),
)

HEATER_FAULTS = frozenset({"heater_burnout", "heater_degraded"})


@dataclass
class ChannelHealth:
    healthy: bool
    reasons: List[str] = field(default_factory=list)
    value: Optional[float] = None          # this tick's reading, if it passed the checks
    lkg: Optional[float] = None            # last known good reading
    age: int = 0                           # ticks since the last good reading
    present: bool = True                   # the channel exists in obs at all

    def to_dict(self) -> Dict[str, Any]:
        return {"healthy": self.healthy, "reasons": list(self.reasons), "lkg": self.lkg, "age": self.age}


class SensorGuard:
    def __init__(self, channels: Sequence[ChannelSpec] = DEFAULT_CHANNELS):
        self.channels = {c.key: c for c in channels}
        self.reset()

    def reset(self) -> None:
        self._lkg: Dict[str, Optional[float]] = {k: None for k in self.channels}
        self._age: Dict[str, int] = {k: 0 for k in self.channels}
        self._prev: Dict[str, Optional[float]] = {k: None for k in self.channels}
        self._gap: Dict[str, int] = {k: 1 for k in self.channels}

    def check(self, obs: Mapping[str, Any]) -> Dict[str, ChannelHealth]:
        out: Dict[str, ChannelHealth] = {}
        for key, spec in self.channels.items():
            if key not in obs and not any(fk in obs for fk in spec.flag_keys):
                # channel not instrumented in this configuration: nothing to judge
                out[key] = ChannelHealth(True, present=False)
                continue
            reasons: List[str] = []
            raw = obs.get(key)
            value: Optional[float] = None
            if raw is None:
                reasons.append("missing")
            else:
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    value = None
                    reasons.append("not_numeric")
            if value is not None and not math.isfinite(value):
                reasons.append("not_finite")
                value = None
            if value is not None and not (spec.lo <= value <= spec.hi):
                reasons.append("out_of_range")
            # compare with the last plausible reading; over k ticks the physics allows k steps
            prev, gap = self._prev[key], self._gap[key]
            jumped = value is not None and prev is not None and abs(value - prev) > spec.max_step * gap
            if jumped:
                reasons.append("rate_of_change")
            for fk in spec.flag_keys:
                labels = obs.get(fk) or []
                if isinstance(labels, (list, tuple)):
                    reasons.extend(f"monitor:{lab}" for lab in labels)
            if value is not None and not jumped and "out_of_range" not in reasons:
                self._prev[key], self._gap[key] = value, 1
            else:
                self._gap[key] += 1

            healthy = not reasons
            if healthy:
                self._lkg[key] = value
                self._age[key] = 0
            else:
                self._age[key] += 1
            if not healthy and self._age[key] > spec.stale_ticks:
                reasons.append("stale")
            out[key] = ChannelHealth(healthy, reasons, value if healthy else None,
                                     self._lkg[key], self._age[key])
        return out


def cbf_min_heat_W(temp_C: float, t_ext_worst_C: float, floor_C: float, gamma: float,
                   ua: float = GreenhouseThermalModel.UA,
                   c_th: float = GreenhouseThermalModel.HEAT_CAPACITY,
                   dt_s: float = GreenhouseThermalModel.TICK_SECONDS) -> float:
    """Smallest heat input (W) that satisfies the discrete barrier condition
    h(T+) >= (1 - gamma) h(T) for h(T) = T - floor over one tick. Can be <= 0 (no heat needed)."""
    d = math.exp(-ua * dt_s / c_th)
    target_next = floor_C + (1.0 - gamma) * (temp_C - floor_C)
    t_eq_needed = (target_next - d * temp_C) / (1.0 - d)
    return ua * (t_eq_needed - t_ext_worst_C)


def next_temp_C(temp_C: float, t_ext_C: float, q_W: float,
                ua: float = GreenhouseThermalModel.UA,
                c_th: float = GreenhouseThermalModel.HEAT_CAPACITY,
                dt_s: float = GreenhouseThermalModel.TICK_SECONDS) -> float:
    """One tick of the thermal model with constant exterior temperature."""
    d = math.exp(-ua * dt_s / c_th)
    t_eq = t_ext_C + q_W / ua
    return t_eq + (temp_C - t_eq) * d


def sanitize(commands: Mapping[str, Any]) -> Tuple[Dict[str, float], List[str]]:
    """P6: every command finite and inside its schema range. Unknown keys pass through."""
    out: Dict[str, float] = {}
    notes: List[str] = []
    for key, v in commands.items():
        spec = COMMAND_KEYS.get(key)
        try:
            x = float(v)
        except (TypeError, ValueError):
            x = math.nan
        if spec is None:
            out[key] = x
            continue
        lo = spec.lo if spec.lo is not None else -math.inf
        hi = spec.hi if spec.hi is not None else math.inf
        if not math.isfinite(x):
            notes.append(f"{key}:non_finite->{lo:g}")
            x = lo if math.isfinite(lo) else 0.0
        elif x < lo or x > hi:
            clipped = min(hi, max(lo, x))
            notes.append(f"{key}:{x:g}->{clipped:g}")
            x = clipped
        out[key] = x
    return out, notes


class FailSafeSupervisor:
    """Fail-safe supervisor. Use as run(..., supervisor=FailSafeSupervisor()) or, with the
    model-based monitor in the loop, as MonitorInLoop(monitor, inner_supervisor=FailSafeSupervisor())
    so the monitor's prediction model is stepped with the commands actually sent.

    history[tick] records the mode, why, channel health and every override, so requirements
    can be checked on the log afterwards (safety.evaluate).
    """

    def __init__(
        self,
        inner_supervisor=merge_commands,
        guard: Optional[SensorGuard] = None,
        setpoint_C: float = 22.0,
        emergency_low_C: float = 12.0,
        emergency_high_C: float = 30.0,
        barrier_floor_C: float = 16.0,
        barrier_gamma: float = 0.5,
        hold_gain_W_per_K: float = 300.0,
        open_loop_limit_ticks: int = 48,
        clear_ticks: int = 6,
        co2_interlock_ppm: float = 2000.0,
        co2_emergency_ppm: float = 3000.0,
        hold_band_W: float = 150.0,
        heater_probe_W: float = 600.0,
        lamp_max_W: float = 15200.0,
        heater_max_W: float = 5000.0,
        thermal: Optional[GreenhouseThermalModel] = None,
    ):
        self.inner = inner_supervisor
        self.guard = guard or SensorGuard()
        self.setpoint_C = float(setpoint_C)
        self.emergency_low_C = float(emergency_low_C)
        self.emergency_high_C = float(emergency_high_C)
        self.barrier_floor_C = float(barrier_floor_C)
        self.barrier_gamma = float(barrier_gamma)
        if not 0.0 < self.barrier_gamma <= 1.0:
            raise ValueError("barrier_gamma must be in (0, 1]")
        self.hold_gain = float(hold_gain_W_per_K)
        self.open_loop_limit = int(open_loop_limit_ticks)
        self.clear_ticks = int(clear_ticks)
        self.co2_interlock_ppm = float(co2_interlock_ppm)
        self.co2_emergency_ppm = float(co2_emergency_ppm)
        self.hold_band_W = float(hold_band_W)
        self.heater_probe_W = float(heater_probe_W)
        self.lamp_max_W = float(lamp_max_W)
        self.heater_max_W = float(heater_max_W)
        self.thermal = thermal or GreenhouseThermalModel()
        self.reset()

    def reset(self) -> None:
        self.guard.reset()
        self.mode = Mode.NOMINAL
        self._calm = 0
        self._last_heater_cmd = 0.0
        self._latched: set = set()   # which emergency protocols are engaged (cold, hot, co2)
        self.history: Dict[int, Dict[str, Any]] = {}

    # ---- mode logic -------------------------------------------------------------------------

    def _temperature_estimate(self, obs: Mapping[str, Any], health: Dict[str, ChannelHealth]) -> Optional[float]:
        """Best cabin temperature: the monitor's fused value (it already falls back to its model
        when the sensor is faulty), else the sensor if healthy, else None (unknown)."""
        fused = obs.get("fused_temp_C")
        if fused is not None and math.isfinite(float(fused)):
            return float(fused)
        th = health.get("gh.temp_C")
        return th.value if th is not None and th.healthy else None

    def _target_mode(self, obs, health, temp_hat) -> Tuple[Mode, List[str]]:
        reasons: List[str] = []
        actuator_faults = set(obs.get("actuator_faults") or [])
        heater_fault = actuator_faults & HEATER_FAULTS
        if heater_fault:
            reasons += [f"actuator:{f}" for f in sorted(heater_fault)]
        if temp_hat is not None and temp_hat < self.emergency_low_C:
            reasons.append(f"temp_low:{temp_hat:.1f}")
        if temp_hat is not None and temp_hat > self.emergency_high_C:
            reasons.append(f"temp_high:{temp_hat:.1f}")
        co2 = health.get("gh.co2_ppm")
        co2_now = co2.value if co2 is not None and co2.healthy else obs.get("fused_co2_ppm")
        if co2_now is not None and float(co2_now) >= self.co2_emergency_ppm:
            reasons.append(f"co2_high:{float(co2_now):.0f}")
        if "co2_injector_stuck_open" in actuator_faults:
            reasons.append("actuator:co2_injector_stuck_open")
        if reasons:
            return Mode.EMERGENCY, reasons

        bad = {k: h for k, h in health.items() if h.present and not h.healthy}
        crit_bad = [k for k in bad if self.guard.channels[k].critical]
        temp = health.get("gh.temp_C")
        if temp_hat is None:
            reasons.append("temp_unknown")
        if temp is not None and not temp.healthy and temp.age > self.open_loop_limit:
            reasons.append(f"temp_open_loop>{self.open_loop_limit}")
        if "gh.temp_C" in crit_bad and "gh.co2_ppm" in crit_bad:
            reasons.append("temp_and_co2_lost")
        if reasons:
            return Mode.SAFE_HOLD, reasons

        if bad:
            return Mode.DEGRADED, [f"{k}:{','.join(h.reasons)}" for k, h in bad.items()]
        return Mode.NOMINAL, []

    def _step_mode(self, target: Mode) -> None:
        if target >= self.mode:
            self.mode = target
            self._calm = 0
            return
        self._calm += 1
        if self._calm >= self.clear_ticks:
            self.mode = Mode(self.mode - 1)
            self._calm = 0

    # ---- protocols --------------------------------------------------------------------------

    def _ext_worst(self, obs, hour: float) -> float:
        now = obs.get("mars.temp_ext_C")
        nxt = self.thermal.outside_temp_C(hour + 1.0)
        return min(float(now), nxt) if now is not None else nxt

    def __call__(self, obs: Dict[str, Any], per_controller: Dict[str, Dict[str, float]], ctx) -> Dict[str, float]:
        tick = int(getattr(ctx, "tick", obs.get("time.tick", 0)) or 0)
        commands = dict(self.inner(obs, per_controller, ctx))
        health = self.guard.check(obs)
        temp_hat = self._temperature_estimate(obs, health)
        target, reasons = self._target_mode(obs, health, temp_hat)
        self._step_mode(target)
        if self.mode != Mode.EMERGENCY:
            self._latched = set()
        elif target == Mode.EMERGENCY:
            for r in reasons:
                if r.startswith(("actuator:heater", "temp_low")):
                    self._latched.add("cold")
                elif r.startswith("temp_high"):
                    self._latched.add("hot")
                elif r.startswith(("co2_high", "actuator:co2")):
                    self._latched.add("co2")
            if "hot" in self._latched and any(r.startswith(("actuator:heater", "temp_low")) for r in reasons):
                self._latched.discard("hot")
            if "cold" in self._latched and any(r.startswith("temp_high") for r in reasons):
                self._latched.discard("cold")
        overrides: List[str] = []

        def override(key: str, value: float, why: str) -> None:
            old = commands.get(key)
            if old is None or abs(float(old) - value) > 1e-9:
                overrides.append(f"{why}:{key}={value:.4g}")
            commands[key] = value

        hour = float(obs.get("time.hour_of_day") or 0.0)
        t_ext_worst = self._ext_worst(obs, hour)
        actuator_faults = set(obs.get("actuator_faults") or [])

        # P2 CO2 injection: never inject on an untrusted gas reading (fail closed to the crop's
        # measured uptake, which cannot raise the concentration), and a hard high-CO2 interlock.
        co2_h, p_h = health.get("gh.co2_ppm"), health.get("gh.pressure_kPa")
        if "cmd.co2_inject_mol" in commands or "crop.s0.co2_inhaled_mol" in obs:
            inject = float(commands.get("cmd.co2_inject_mol", 0.0))
            co2_reading = co2_h.value if co2_h is not None else None
            if (co2_h is not None and co2_h.present and not co2_h.healthy) or \
               (p_h is not None and p_h.present and not p_h.healthy):
                # mass-balance hold: replace exactly what the crop took up last tick. This keeps
                # the concentration where it was (the crop is not starved) and cannot raise it.
                uptake = obs.get("crop.s0.co2_inhaled_mol")
                hold = max(0.0, float(uptake)) if uptake is not None else 0.0
                override("cmd.co2_inject_mol", hold, "P2_co2_mass_balance")
            elif co2_reading is not None and co2_reading >= self.co2_interlock_ppm and inject > 0:
                override("cmd.co2_inject_mol", 0.0, "P2_co2_interlock")
            if "co2_injector_stuck_open" in actuator_faults and commands["cmd.co2_inject_mol"] > 0:
                override("cmd.co2_inject_mol", 0.0, "P2_injector_isolate")

        lamp = float(commands.get("cmd.crop_power_W", 0.0))
        heater = float(commands.get("cmd.heater_W", 0.0))

        # P3 safe hold: temperature unknown or open-loop for too long. The heater command is kept
        # inside a band around the model's hull-loss feedforward (the heat that holds the
        # setpoint), so a wrong temperature estimate can neither run the heater away nor let
        # the cabin freeze. Without any estimate the command is the feedforward itself.
        if self.mode == Mode.SAFE_HOLD:
            t_ext_now = obs.get("mars.temp_ext_C")
            t_ext_now = float(t_ext_now) if t_ext_now is not None else t_ext_worst
            ff = self.thermal.ua * (self.setpoint_C - t_ext_now) - self.thermal.LAMP_HEAT_FRACTION * lamp
            ff = min(self.heater_max_W, max(0.0, ff))
            if temp_hat is None or "cmd.heater_W" not in commands:
                held = ff
            else:
                held = min(ff + self.hold_band_W, max(ff - self.hold_band_W, heater))
            override("cmd.heater_W", max(0.0, held), "P3_safe_hold")

        # P4 / P5 emergency: heater fault or cabin near the survival limits.
        if self.mode == Mode.EMERGENCY and self._latched & {"hot", "cold"}:
            if "hot" in self._latched:
                # P5 overheat: heater off, lamps limited to what the hull can shed at setpoint
                override("cmd.heater_W", 0.0, "P5_overheat")
                lamp_cap = max(0.0, self.thermal.ua * (self.setpoint_C - t_ext_worst)) / self.thermal.LAMP_HEAT_FRACTION
                if lamp > lamp_cap:
                    override("cmd.crop_power_W", lamp_cap, "P5_overheat")
            else:
                # P4 backup heat: lamps become the heater. Heat needed is the larger of the
                # barrier bound and a setpoint hold; the heater's expected share is subtracted.
                t_now = temp_hat if temp_hat is not None else self.barrier_floor_C
                q_cbf = cbf_min_heat_W(t_now, t_ext_worst, self.barrier_floor_C, self.barrier_gamma,
                                       self.thermal.ua, self.thermal.c_th)
                q_hold = self.thermal.ua * (self.setpoint_C - t_ext_worst) + self.hold_gain * (self.setpoint_C - t_now)
                q_need = max(0.0, q_cbf, q_hold)
                # Keep a probe current on the faulty heater instead of switching it off: the
                # monitor can only see a burnout while the heater is being asked for power, and
                # the probe is how a recovered heater shows itself (draw comes back).
                if heater < self.heater_probe_W:
                    override("cmd.heater_W", self.heater_probe_W, "P4_heater_probe")
                    heater = self.heater_probe_W
                heater_share = self._heater_share(obs, heater)
                lamp_need = (q_need - heater_share) / self.thermal.LAMP_HEAT_FRACTION
                lamp_need = min(self.lamp_max_W, max(0.0, lamp_need))
                if lamp_need > lamp:
                    override("cmd.crop_power_W", lamp_need, "P4_backup_heat")

        commands, clipped = sanitize(commands)
        overrides += [f"P6_clip:{c}" for c in clipped]
        self._last_heater_cmd = float(commands.get("cmd.heater_W", 0.0))

        self.history[tick] = {
            "mode": self.mode.name,
            "target_mode": target.name,
            "reasons": reasons,
            "temp_hat_C": temp_hat,
            "emergency": sorted(self._latched),
            "overrides": overrides,
            "health": {k: h.to_dict() for k, h in health.items() if h.present},
        }
        return commands

    def _heater_share(self, obs: Mapping[str, Any], heater_cmd: float) -> float:
        """Heat the (degraded) heater is expected to deliver: this tick's command scaled by the
        delivered/commanded ratio seen last tick."""
        draw = obs.get("power.heater_draw")
        if draw is None or self._last_heater_cmd <= 1.0:
            return 0.0
        ratio = max(0.0, min(1.0, float(draw) / self._last_heater_cmd))
        return ratio * heater_cmd
