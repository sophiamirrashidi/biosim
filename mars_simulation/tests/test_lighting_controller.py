import pytest

from controllers import LightingController
from interface import bindings

FULL_W = bindings.full_light_W(10.0, 295.14)


def obs(battery_Wh, tick=0, biomass=0.0, heater_W=0.0):
    return {"time.tick": tick, "power.store_level": battery_Wh, "power.heater_draw": heater_W,
            "crop.biomass_store_kg": biomass, "crop.s0.harvest_interval": 28.0, "crop.s0.ppf_needed": 295.14}


def drain(c, start_tick, hours_left, drain_W=1000.0, hours=25, **kw):
    """Feeds `hours` ticks of a steady drain that ends with `hours_left` hours above the
    5 kWh reserve. Returns the last command."""
    end = 5000.0 + hours_left * drain_W
    out = None
    for i in range(hours):
        out = c(obs(end + (hours - 1 - i) * drain_W, tick=start_tick + i, **kw))
    return out["cmd.crop_power_W"]


def test_charging_battery_keeps_full_light():
    c = LightingController()
    for t in range(30):
        c(obs(50000.0 + 500.0 * t, tick=t))
    assert c.mode == "FULL" and c.hours_left == float("inf")


def test_too_little_history_keeps_the_level():
    c = LightingController()
    for t in range(12):
        c(obs(20000.0 - 1000.0 * t, tick=t))
    assert c.hours_left is None and c.mode == "FULL"


@pytest.mark.parametrize("tick,hours_left,mode", [
    # young crop: dims only when the battery is close to empty (horizon 48 h)
    (100, 60, "FULL"), (100, 40, "REDUCED"), (100, 20, "SURVIVAL"),
    # middle (96 h)
    (350, 120, "FULL"), (350, 70, "REDUCED"), (350, 40, "SURVIVAL"),
    # near harvest (168 h): the same hours left already dim
    (550, 120, "REDUCED"), (550, 60, "SURVIVAL"),
])
def test_level_depends_on_hours_left_and_crop_age(tick, hours_left, mode):
    c = LightingController()
    drain(c, tick - 24, hours_left)
    assert c.mode == mode
    assert c.hours_left == pytest.approx(hours_left)


def test_dims_only_while_the_heater_is_idle():
    c = LightingController()
    assert drain(c, 76, 20) == pytest.approx(0.30 * FULL_W), "heater idle: dimming saves real energy"
    assert c(obs(25000.0, tick=101, heater_W=1300.0))["cmd.crop_power_W"] == pytest.approx(FULL_W), \
        "heater on: lamp heat replaces heater heat, so keep the light"
    assert c.mode == "SURVIVAL", "the storm level is kept for when the heater goes idle"


def choose(c, hours_left):
    c.hours_left = hours_left
    c.level = c._choose()
    return c.mode


def test_no_flicker_near_a_threshold():
    c = LightingController()                # young crop: horizon 48 h, band 12 h
    assert choose(c, 40) == "REDUCED"
    assert [choose(c, h) for h in (50, 55, 59)] == ["REDUCED"] * 3, "inside the band"
    assert choose(c, 61) == "FULL"


def test_brightening_from_survival_goes_one_step_at_a_time():
    c = LightingController()
    assert choose(c, 10) == "SURVIVAL"
    assert choose(c, 45) == "REDUCED", "clears 24 + 12, not 48 + 12"


def test_harvest_restarts_the_crop_age():
    c = LightingController()
    drain(c, 626, 120)
    assert c.mode == "REDUCED", "120 h is inside the near-harvest horizon"
    drain(c, 673, 120, biomass=32.8)      # a harvest: a new crop was planted
    assert c.age_frac < 0.05 and c.mode == "FULL", "young crop again"


def test_missing_readings_keep_the_level_and_defaults():
    c = LightingController()
    drain(c, 76, 40)
    o = obs(0.0, tick=101)
    o["power.store_level"] = None
    del o["power.heater_draw"]
    assert c(o)["cmd.crop_power_W"] == pytest.approx(0.75 * FULL_W)
    assert LightingController()({"time.tick": 0})["cmd.crop_power_W"] == pytest.approx(FULL_W)


# in the loop: a long dust storm on HybridPlant(FakePlant) with HVAC and the power supervisor

from controllers import HVACTemperatureController  # noqa: E402
from interface import FakePlant, Fault, TickLogger, run  # noqa: E402
from interface.plant import HybridPlant  # noqa: E402
from supervisors import PowerBudgetSupervisor  # noqa: E402


def storm_run(lamps, ticks=24 * 30):
    # four stacked SEVERE malfunctions: solar x0.0625, about the 2018 Opportunity storm
    storm = [Fault(tick=100, kind="malfunction", target="Solar_Array",
                   params={"intensity": "SEVERE_MALF", "length": "PERMANENT_MALF"}, duration=24 * 21)
             for _ in range(4)]
    log = TickLogger()
    run(HybridPlant(plant=FakePlant(), sync_biosim_temperature=False),
        {"lamps": lamps, "hvac": HVACTemperatureController()}, ticks,
        supervisor=PowerBudgetSupervisor(5000.0), fault_schedule=storm, logger=log)
    return log.records


def test_long_storm_dims_by_day_and_keeps_more_heat():
    fixed = storm_run(lambda o: {"cmd.crop_power_W": FULL_W})
    ctl, modes, heater_on = LightingController(), [], []

    def lamps(o):
        cmds = ctl(o)
        modes.append(ctl.mode)
        heater_on.append(ctl.heater_on)
        return cmds

    adaptive = storm_run(lamps)
    dimmed = [i for i, m in enumerate(modes) if m != "FULL"]
    assert dimmed and min(dimmed) > 100, "dims only once the storm drains the battery"
    asked = [r["commands"]["lamps"]["cmd.crop_power_W"] for r in adaptive]
    assert all(asked[i] == pytest.approx(FULL_W) for i in dimmed if heater_on[i]), "full light while heating"
    assert any(asked[i] < FULL_W for i in dimmed if not heater_on[i]), "dimmed while the heater is idle"

    def short(recs):
        return sum(r["applied"].get("cmd.heater_W", 0.0) < r["commands"]["hvac"].get("cmd.heater_W", 0.0) - 1
                   for r in recs)
    assert short(adaptive) <= short(fixed), "the heater is starved no more often"
    assert modes[-1] == "FULL", "back to full light once the battery recharges"


# lamp faults in the loop: the controller compensates weak lamps, not a lying sensor

from actuators import LAMP_KEY, LightingActuatorLayer  # noqa: E402
from sensors import LightSensorLayer, light_key  # noqa: E402

PPF = light_key(0)


def fault_run(fault, ticks=80):
    ctl, log = LightingController(), TickLogger()
    gains, flags = [], []

    def lamps(o):
        cmds = ctl(o)
        gains.append(ctl.gain)
        flags.append(ctl.status["lamp.faults"])
        return cmds

    run(FakePlant(), {"lamps": lamps}, ticks, sensor_layer=LightSensorLayer(),
        actuator_layer=LightingActuatorLayer(), fault_schedule=[fault], logger=log)
    light = [r["truth"][PPF] for r in log.records]
    return gains, flags, light


def lamp_fault(kind, **params):
    return Fault(tick=20, kind=kind, target=LAMP_KEY, params=params, duration=30)


@pytest.mark.parametrize("factor,gain,light", [
    (0.8, 1.25, 295.14),          # fully restored
    (0.6, 1.5, 0.9 * 295.14),     # capped at the fixtures' 1.5x: 90 % of full light
])
def test_weak_lamps_are_compensated_then_released(factor, gain, light):
    gains, flags, truth = fault_run(lamp_fault("efficiency", factor=factor))
    assert gains[19] == 1.0
    assert all(g == pytest.approx(gain) for g in gains[25:50])
    assert all(t == pytest.approx(light, rel=1e-3) for t in truth[26:50])
    assert all(g == 1.0 for g in gains[55:]), "back to normal power after the fault"
    assert all(t == pytest.approx(295.14, rel=1e-3) for t in truth[56:])


def test_dead_lamps_are_flagged_not_chased():
    gains, flags, _ = fault_run(lamp_fault("dropout"))
    assert all("lamp" in f for f in flags[25:50])
    assert max(gains) == 1.0


def test_biased_light_sensor_does_not_move_the_lamps():
    gains, flags, truth = fault_run(Fault(tick=20, kind="bias", target=PPF, params={"offset": -15.0}, duration=30))
    assert max(gains) == 1.0
    assert any("light_sensor_offset" in f for f in flags[20:50])
    assert all(t == pytest.approx(295.14, rel=1e-3) for t in truth[2:])
