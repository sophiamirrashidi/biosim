
"""Shared power budgeting for the Mars greenhouse.

Uses existing battery state and existing command keys.
Does not modify BioSim, its configuration, or the controllers.

Each simulation tick represents one hour.
"""

from __future__ import annotations

import math
from typing import Any, Dict

from interface.loop import merge_commands


DEFAULT_PRIORITY = (
    "cmd.heater_W",
    "cmd.crop_power_W",
    "cmd.recycler_power_W",
)


class PowerBudgetSupervisor:
    """Allocate electrical power while protecting battery reserves."""

    def __init__(
        self,
        reserve_Wh: float = 5000.0,
        priority=DEFAULT_PRIORITY,
    ):
        self.reserve_Wh = float(reserve_Wh)

        if not math.isfinite(self.reserve_Wh) or self.reserve_Wh < 0:
            raise ValueError("Battery reserve must be nonnegative and finite")

        if len(priority) != len(DEFAULT_PRIORITY) or set(priority) != set(DEFAULT_PRIORITY):
            raise ValueError("Priority must contain all three power commands exactly once")

        self.priority = tuple(priority)

        # Diagnostics for experiments and debugging.
        self.last_budget_W = 0.0
        self.last_allocated_W: Dict[str, float] = {}

    def __call__(
        self,
        obs: Dict[str, Any],
        per_controller: Dict[str, Dict[str, float]],
        ctx,
    ) -> Dict[str, float]:

        # 1. Merge existing controller requests.
        # Also detects conflicting commands.
        commands = merge_commands(obs, per_controller, ctx)

        # 2. Read the battery energy from the existing state.
        reading = obs.get("power.store_level")

        if reading is None:
            raise ValueError("Valid power.store_level is required")

        battery_Wh = float(reading)

        if not math.isfinite(battery_Wh) or battery_Wh < 0:
            raise ValueError("Invalid battery energy measurement")

        # 3. Calculate the safe power budget.
        # One simulation tick = one hour.
        # Do not assume future solar production is guaranteed.
        available_Wh = max(0.0, battery_Wh - self.reserve_Wh)
        budget_W = available_Wh / 1.0

        remaining_W = budget_W
        approved = dict(commands)
        allocations = {}

        # 4. Allocate electricity in priority order.
        for key in self.priority:
            if key not in commands:
                continue

            requested_W = float(commands[key])

            if not math.isfinite(requested_W) or requested_W < 0:
                raise ValueError(f"Invalid power request for {key}")

            granted_W = min(requested_W, remaining_W)

            approved[key] = granted_W
            allocations[key] = granted_W
            remaining_W -= granted_W

        # 5. Store diagnostics without adding unsupported command keys.
        self.last_budget_W = budget_W
        self.last_allocated_W = allocations

        return approved
