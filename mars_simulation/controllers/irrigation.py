
"""Irrigation and water recycling controller for the lettuce bed.

The controller maintains a small water buffer in the lettuce reservoir
instead of filling the entire reservoir.

Original behavior:
    ReservoirEstimator -> irrigation decision

Optional fault-aware behavior:
    RootZoneSensor + ReservoirEstimator
        -> RootZoneMonitor
        -> irrigation decision

The RootZoneMonitor identifies unreliable sensor measurements and
provides a combined or fallback reservoir-level estimate.

The existing controller still works without the monitor.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from estimators import ReservoirEstimator, RootZoneMonitor


# Water recycler: 1540 W processes 4.26 L per simulation tick.
RECYCLER_W_PER_L = 1540.0 / 4.26
RECYCLER_MAX_W = 1540.0


class IrrigationController:

    def __init__(
        self,
        buffer_hours: float = 24.0,
        min_buffer_L: float = 5.0,
        kp: float = 0.1,
        max_cap_L: float = 200.0,
        tank_reserve_L: float = 100.0,
        uptake_smoothing: float = 0.1,
        run_recycler: bool = True,
        estimator: Optional[ReservoirEstimator] = None,
        root_zone_monitor: Optional[RootZoneMonitor] = None,
    ):
        self.buffer_hours = float(buffer_hours)
        self.min_buffer_L = float(min_buffer_L)
        self.kp = float(kp)
        self.max_cap_L = float(max_cap_L)
        self.tank_reserve_L = float(tank_reserve_L)
        self.alpha = float(uptake_smoothing)

        self.run_recycler = run_recycler

        # Predicts reservoir water from observable flows.
        self.estimator = estimator or ReservoirEstimator()

        # Optional fault-aware sensor/estimator fusion.
        self.root_zone_monitor = root_zone_monitor

        self.reset()

    def reset(self) -> None:
        """Reset controller state at the start of an experiment."""

        self.estimator.reset()

        if self.root_zone_monitor is not None:
            self.root_zone_monitor.reset()

        self.u_avg = 0.0
        self.last_cap = 0.0
        self.last_recycler_W = 0.0

        # Useful for logging and debugging.
        self.last_estimate: Dict[str, Any] = {}
        self.last_root_zone: Dict[str, Any] = {}

    def __call__(self, obs: Dict[str, Any]) -> Dict[str, float]:
        """Compute irrigation and recycling commands from observations."""

        # STEP 1: Update the model-based reservoir estimate.
        # last_cap is the previous command because the simulation
        # applies commands one tick after they are computed.
        est = self.estimator.update(obs, self.last_cap)
        self.last_estimate = est

        # Smooth crop water uptake to reduce command fluctuations.
        self.u_avg += self.alpha * (
            est["est.uptake_L"] - self.u_avg
        )

        # STEP 2: Determine which reservoir level to trust.
        # By default, preserve the original estimator-based behavior.
        reservoir_level = est["est.reservoir_L"]
        self.last_root_zone = {}

        if self.root_zone_monitor is not None:

            # The sensor must be connected to the simulation.
            # A value of None is allowed: it represents dropout.
            if "crop.s0.root_zone_L" not in obs:
                raise KeyError(
                    "Root-zone sensor reading missing from observations"
                )

            # Compare the physical sensor reading against the estimator.
            self.last_root_zone = self.root_zone_monitor.update(
                sensor_L=obs["crop.s0.root_zone_L"],
                est=est,
                co2_ppm=obs.get("gh.co2_ppm"),
                biomass_kg=obs.get("crop.biomass_store_kg"),
            )

            # Use the monitor's selected level if at least
            # one information source is considered healthy.
            if self.last_root_zone["rz.trusted"] != "none":
                reservoir_level = self.last_root_zone["rz.level_L"]

            # If neither source is trusted, retain the model
            # estimate as a temporary fallback.
            # A conservative safety policy is future work.

        # STEP 3: Calculate the target reservoir buffer.
        target = max(
            self.min_buffer_L,
            self.buffer_hours * self.u_avg,
        )

        # Conserve tank water when reserves are low.
        tank = obs.get("water.potable_L")

        if tank is not None and tank < self.tank_reserve_L:
            target = self.min_buffer_L

        # STEP 4: Compute the irrigation command.
        # Increase water when the reservoir is below target.
        # Decrease water when the reservoir is above target.
        cap = self.u_avg + self.kp * (
            target - reservoir_level
        )

        # Respect the configured command limits.
        cap = min(self.max_cap_L, max(0.0, cap))

        cmds = {
            "cmd.crop_water_L": cap
        }

        # STEP 5: Control water recycling independently.
        if self.run_recycler:

            dirty = obs.get("water.dirty_L")

            # Missing reading: keep the last recycler setting.
            if dirty is None:
                recycler = self.last_recycler_W
            else:
                recycler = min(
                    RECYCLER_MAX_W,
                    dirty * RECYCLER_W_PER_L,
                )

            cmds["cmd.recycler_power_W"] = recycler
            self.last_recycler_W = recycler

        # Remember this command for the next tick.
        self.last_cap = cap

        return cmds

