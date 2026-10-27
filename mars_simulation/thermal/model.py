"""Simple lumped thermal model for the Mars lettuce greenhouse."""

import math


class GreenhouseThermalModel:
    # Thermal parameters established for the greenhouse
    HEAT_CAPACITY = 1_771_225.0  # J/K
    UA = 14.0                    # W/K
    TICK_SECONDS = 3600.0        # 1 BioSim tick = 1 hour

    LAMP_HEAT_FRACTION = 0.90
    HEATER_EFFICIENCY = 1.0

    def __init__(self, initial_temp_C=20.0):
        self.temp_C = float(initial_temp_C)

    def outside_temp_C(self, hour):
        """Approximate Martian outside temperature."""
        return -60.0 + 25.0 * math.cos(
            2.0 * math.pi * (hour - 14.0) / 24.66
        )

    def step(self, hour, lamp_power_W, heater_power_W):
        """Advance greenhouse temperature by one BioSim tick."""
        outside = self.outside_temp_C(hour)

        lamp_heat_W = self.LAMP_HEAT_FRACTION * lamp_power_W
        heater_heat_W = self.HEATER_EFFICIENCY * heater_power_W
        heat_loss_W = self.UA * (self.temp_C - outside)

        net_heat_W = lamp_heat_W + heater_heat_W - heat_loss_W

        self.temp_C += (
            net_heat_W * self.TICK_SECONDS / self.HEAT_CAPACITY
        )

        return self.temp_C
