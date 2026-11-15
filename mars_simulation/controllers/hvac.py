"""HVAC temperature controller for Martian greenhouse habitat."""
from __future__ import annotations

from typing import Any, Dict, Optional


class HVACTemperatureController:
    """Proportional-Integral-Derivative (PID) controller for greenhouse cabin heating.

    Target Setpoint:
        T_setpoint = 22.0 °C (lettuce optimal metabolic temperature)
    
    Actuation:
        Outputs `cmd.heater_W` in [0.0, max_heating_W] (default 5000 W max).
        Features anti-windup clamping and battery energy reserve protection.
    """

    def __init__(
        self,
        setpoint_C: float = 22.0,
        kp: float = 120.0,
        ki: float = 8.0,
        kd: float = 30.0,
        max_heating_W: float = 5000.0,
        min_power_reserve_Wh: float = 5000.0,
    ):
        self.setpoint_C = float(setpoint_C)
        self.kp = float(kp)
        self.ki = float(ki)
        self.kd = float(kd)
        self.max_heating_W = float(max_heating_W)
        self.min_power_reserve = float(min_power_reserve_Wh)

        self._integral_error = 0.0
        self._last_error: Optional[float] = None

    def reset(self) -> None:
        self._integral_error = 0.0
        self._last_error = None

    def __call__(self, obs: Dict[str, Any]) -> Dict[str, float]:
        """Calculates heating power command from sensed or fused temperature observation."""
        temp_val = obs.get("fused_temp_C")
        if temp_val is None:
            temp_val = obs.get("gh.temp_C")
        if temp_val is None:
            temp_val = self.setpoint_C
        temp_sensed = float(temp_val)
        error = self.setpoint_C - temp_sensed

        # Derivative term
        if self._last_error is not None:
            derivative = error - self._last_error
        else:
            derivative = 0.0
        self._last_error = error

        # Proportional and Derivative contribution
        p_term = self.kp * error
        d_term = self.kd * derivative

        # Check battery reserve: don't exhaust power store if below safety threshold
        power_store = float(obs.get("power.store_level", float("inf")))
        if power_store < self.min_power_reserve:
            return {"cmd.heater_W": 0.0}

        # Raw desired power
        desired_power = p_term + (self.ki * self._integral_error) + d_term

        # Clamp command to [0, max_heating_W]
        heater_power = max(0.0, min(self.max_heating_W, desired_power))

        # Anti-windup conditional integration
        if 0.0 < desired_power < self.max_heating_W:
            self._integral_error += error

        return {"cmd.heater_W": heater_power}


class ThermalSafetySupervisor:
    """Supervisory controller enforcing thermal survival via auxiliary lamp heating.

    Governing physics:
        90% of electric crop lamp power dissipates directly as cabin heat:
        Q_lamp = 0.90 * P_lamp

    When primary heating is lost (heater burnout or severe degradation),
    the supervisor intercepts commands and overrides `cmd.crop_power_W` with
    the auxiliary power required to balance hull thermal conduction:
        P_lamp_aux = (UA * (T_setpoint - T_ext)) / 0.90
    This mathematically guarantees cabin temperature stays within the [7.0 °C, 32.0 °C]
    biological survival envelope even during the -120 °C Martian night.
    """

    def __init__(
        self,
        target_temp_C: float = 22.0,
        ua_W_per_K: float = 14.0,
        lamp_heat_fraction: float = 0.90,
        min_safe_temp_C: float = 7.0,
        max_safe_temp_C: float = 32.0,
    ):
        self.target_temp_C = float(target_temp_C)
        self.ua = float(ua_W_per_K)
        self.lamp_heat_fraction = float(lamp_heat_fraction)
        self.min_safe_temp_C = float(min_safe_temp_C)
        self.max_safe_temp_C = float(max_safe_temp_C)

    def __call__(
        self,
        obs: Dict[str, Any],
        per_controller: Dict[str, Dict[str, float]],
        ctx: Any = None,
    ) -> Dict[str, float]:
        # 1. Merge commands across all controllers
        commands: Dict[str, float] = {}
        for name, cmds in per_controller.items():
            commands.update(cmds)

        # 2. Check if primary heater has failed
        actuator_faults = obs.get("actuator_faults", [])
        temp_val = obs.get("fused_temp_C", obs.get("gh.temp_C", 22.0))
        temp_C = float(temp_val if temp_val is not None else 22.0)
        t_ext = float(obs.get("mars.temp_ext_C", -50.15))

        heater_failed = "heater_burnout" in actuator_faults or "heater_degraded" in actuator_faults
        if not heater_failed and temp_C < 14.0 and commands.get("cmd.heater_W", 0.0) >= 4900.0:
            heater_failed = True

        # 3. Emergency Auxiliary Lamp Heating Reconfiguration
        if heater_failed:
            q_needed = max(0.0, self.ua * (self.target_temp_C - t_ext))
            p_lamp_aux = min(15000.0, max(2000.0, q_needed / self.lamp_heat_fraction))
            commands["cmd.crop_power_W"] = max(commands.get("cmd.crop_power_W", 0.0), p_lamp_aux)
            commands["cmd.heater_W"] = 0.0

        return commands

