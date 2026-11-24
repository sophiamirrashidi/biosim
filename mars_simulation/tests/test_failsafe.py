"""Unit tests for SensorGuard, the fail-safe mode automaton, the protocols and the thermal barrier."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from safety.failsafe import (ChannelSpec, FailSafeSupervisor, Mode, SensorGuard, cbf_min_heat_W,
                             next_temp_C, sanitize)
from thermal.model import GreenhouseThermalModel

TEMP = ChannelSpec("gh.temp_C", -130.0, 80.0, 40.0, stale_ticks=2, flag_keys=("temp_faults",), critical=True)


def healthy_obs(**over):
    obs = {"time.tick": 0, "time.hour_of_day": 3.0, "mars.temp_ext_C": -100.0,
           "gh.temp_C": 22.0, "gh.co2_ppm": 1200.0, "gh.o2_frac": 0.21, "gh.pressure_kPa": 101.0,
           "temp_faults": [], "co2_faults": [], "o2_faults": [], "pressure_faults": [],
           "actuator_faults": [], "crop.s0.co2_inhaled_mol": 0.2, "power.heater_draw": 2000.0}
    obs.update(over)
    return obs


def ctx(tick=0):
    return SimpleNamespace(tick=tick, active_faults=[])


def single(cmds):
    return {"c": dict(cmds)}


# ---- SensorGuard -------------------------------------------------------------------------------

@pytest.mark.parametrize("value, reason", [
    (None, "missing"), (math.nan, "not_finite"), (math.inf, "not_finite"),
    ("abc", "not_numeric"), (150.0, "out_of_range"), (-200.0, "out_of_range"),
])
def test_guard_rejects_implausible_readings(value, reason):
    g = SensorGuard([TEMP])
    g.check({"gh.temp_C": 22.0})
    h = g.check({"gh.temp_C": value})["gh.temp_C"]
    assert not h.healthy and reason in h.reasons and h.lkg == 22.0


def test_guard_rejects_unphysical_jump_and_takes_monitor_labels():
    g = SensorGuard([TEMP])
    g.check({"gh.temp_C": 22.0})
    assert "rate_of_change" in g.check({"gh.temp_C": 75.0})["gh.temp_C"].reasons
    h = g.check({"gh.temp_C": 22.0, "temp_faults": ["stuck"]})["gh.temp_C"]
    assert h.reasons == ["monitor:stuck"]


def test_guard_marks_channel_stale_and_recovers():
    g = SensorGuard([TEMP])
    g.check({"gh.temp_C": 22.0})
    ages = [g.check({"gh.temp_C": None})["gh.temp_C"] for _ in range(3)]
    assert [h.age for h in ages] == [1, 2, 3]
    assert "stale" not in ages[1].reasons and "stale" in ages[2].reasons
    back = g.check({"gh.temp_C": 21.5})["gh.temp_C"]
    assert back.healthy and back.age == 0 and back.lkg == 21.5


def test_guard_ignores_channels_not_instrumented():
    h = SensorGuard().check({"gh.temp_C": 22.0})
    assert not h["crop.s0.root_zone_L"].present and h["crop.s0.root_zone_L"].healthy


# ---- mode automaton ----------------------------------------------------------------------------

def test_healthy_inputs_stay_nominal_and_commands_pass_through():
    fs = FailSafeSupervisor()
    cmds = {"cmd.heater_W": 1500.0, "cmd.crop_power_W": 200.0, "cmd.co2_inject_mol": 0.1}
    for t in range(10):
        out = fs(healthy_obs(), single(cmds), ctx(t))
    assert fs.mode == Mode.NOMINAL and out == cmds and fs.history[9]["overrides"] == []


def test_escalation_is_immediate_and_descent_waits_for_dwell():
    fs = FailSafeSupervisor(clear_ticks=4)
    fs(healthy_obs(actuator_faults=["heater_burnout"]), single({}), ctx(0))
    assert fs.mode == Mode.EMERGENCY
    modes = []
    for t in range(1, 20):
        fs(healthy_obs(), single({}), ctx(t))
        modes.append(fs.mode)
    # one level at a time, each after 4 calm ticks
    assert modes[:3] == [Mode.EMERGENCY] * 3
    assert modes[3] == Mode.SAFE_HOLD and modes[7] == Mode.DEGRADED and modes[11] == Mode.NOMINAL


def test_mode_switching_is_non_zeno_under_a_flickering_fault():
    """A fault label that toggles every tick must not toggle the mode every tick."""
    fs = FailSafeSupervisor(clear_ticks=6)
    switches, last = 0, fs.mode
    for t in range(200):
        obs = healthy_obs(o2_faults=["stuck"] if t % 2 == 0 else [])
        fs(obs, single({}), ctx(t))
        switches += fs.mode != last
        last = fs.mode
    assert switches <= 1 and fs.mode == Mode.DEGRADED


def test_temperature_unknown_goes_to_safe_hold():
    fs = FailSafeSupervisor()
    fs(healthy_obs(), single({}), ctx(0))
    fs(healthy_obs(**{"gh.temp_C": None, "temp_faults": ["dropout"]}), single({"cmd.heater_W": 0.0}), ctx(1))
    assert fs.mode == Mode.SAFE_HOLD and "temp_unknown" in fs.history[1]["reasons"]


def test_long_open_loop_on_fused_estimate_goes_to_safe_hold():
    fs = FailSafeSupervisor(open_loop_limit_ticks=5)
    for t in range(8):
        fs(healthy_obs(temp_faults=["stuck"], fused_temp_C=21.0), single({"cmd.heater_W": 1500.0}), ctx(t))
    assert fs.mode == Mode.SAFE_HOLD


# ---- protocols ---------------------------------------------------------------------------------

def test_p2_untrusted_co2_injects_only_the_measured_uptake():
    fs = FailSafeSupervisor()
    out = fs(healthy_obs(co2_faults=["low_bias_or_drift"]), single({"cmd.co2_inject_mol": 4.0}), ctx())
    assert out["cmd.co2_inject_mol"] == pytest.approx(0.2)
    out = fs(healthy_obs(pressure_faults=["dropout"], **{"gh.pressure_kPa": None}),
             single({"cmd.co2_inject_mol": 4.0}), ctx(1))
    assert out["cmd.co2_inject_mol"] == pytest.approx(0.2)


def test_p2_high_co2_interlock():
    fs = FailSafeSupervisor()
    out = fs(healthy_obs(**{"gh.co2_ppm": 2100.0}), single({"cmd.co2_inject_mol": 1.0}), ctx())
    assert out["cmd.co2_inject_mol"] == 0.0


def test_p3_safe_hold_keeps_heater_near_feedforward():
    fs = FailSafeSupervisor()
    # temperature lost: heater is the hull-loss feedforward, whatever the controller asked for
    out = fs(healthy_obs(**{"gh.temp_C": None, "temp_faults": ["dropout"]}),
             single({"cmd.heater_W": 5000.0, "cmd.crop_power_W": 200.0}), ctx())
    ff = 14.0 * (22.0 - (-100.0)) - 0.9 * 200.0
    assert out["cmd.heater_W"] == pytest.approx(ff)


def test_p4_heater_burnout_moves_heat_to_the_lamps():
    fs = FailSafeSupervisor()
    out = fs(healthy_obs(actuator_faults=["heater_burnout"], fused_temp_C=20.0),
             single({"cmd.heater_W": 5000.0, "cmd.crop_power_W": 200.0}), ctx())
    assert out["cmd.heater_W"] == 5000.0           # kept commanded so the burnout stays observable
    # no heater output was seen last tick, so none is counted on: lamps must cover at least the hull loss at the coldest exterior of the coming hour
    t_ext = min(-100.0, GreenhouseThermalModel().outside_temp_C(4.0))
    assert 0.9 * out["cmd.crop_power_W"] >= 14.0 * (20.0 - t_ext)


def test_p4_probes_an_idle_faulty_heater():
    fs = FailSafeSupervisor(heater_probe_W=600.0)
    out = fs(healthy_obs(actuator_faults=["heater_burnout"], fused_temp_C=22.0),
             single({"cmd.heater_W": 0.0, "cmd.crop_power_W": 200.0}), ctx())
    assert out["cmd.heater_W"] == 600.0


def test_p5_overheat_turns_heat_off():
    fs = FailSafeSupervisor()
    out = fs(healthy_obs(fused_temp_C=31.0, **{"mars.temp_ext_C": 0.0, "time.hour_of_day": 14.0}),
             single({"cmd.heater_W": 3000.0, "cmd.crop_power_W": 10000.0}), ctx())
    assert out["cmd.heater_W"] == 0.0 and out["cmd.crop_power_W"] < 10000.0


def test_p6_sanitize_clips_and_replaces_non_finite():
    out, notes = sanitize({"cmd.heater_W": 9000.0, "cmd.co2_inject_mol": math.nan, "cmd.crop_water_L": -1.0})
    assert out == {"cmd.heater_W": 5000.0, "cmd.co2_inject_mol": 0.0, "cmd.crop_water_L": 0.0}
    assert len(notes) == 3


# ---- barrier certificate for the emergency heat law (lecture 10) --------------------------------

def test_cbf_heat_makes_the_safe_set_forward_invariant():
    """Inductive-invariant check on a grid: from any T >= floor, with the CBF heat, the next
    state stays >= floor for every exterior temperature Mars can produce."""
    floor, gamma = 16.0, 0.5
    for t_ext in range(-121, 21, 7):
        for t in [16.0, 16.5, 18.0, 22.0, 30.0]:
            q = max(0.0, cbf_min_heat_W(t, t_ext, floor, gamma))
            t_next = next_temp_C(t, t_ext, q)
            assert t_next - floor >= (1 - gamma) * (t - floor) - 1e-9
            assert t_next >= floor - 1e-9


def test_cbf_pulls_the_state_back_when_below_the_floor():
    floor, gamma = 16.0, 0.5
    t = 10.0
    for _ in range(10):
        t = next_temp_C(t, -120.0, max(0.0, cbf_min_heat_W(t, -120.0, floor, gamma)))
    assert floor - t < 6.0 * 0.5 ** 10 + 1e-9
