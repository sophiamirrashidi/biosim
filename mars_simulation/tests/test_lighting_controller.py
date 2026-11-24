import pytest

from controllers import LightingController
from interface import bindings

FULL_W = bindings.full_light_W(10.0, 295.14)
CAP = 200000.0
CYCLE = 28 * 24


def obs(soc, tick=0, biomass=0.0):
    return {"time.tick": tick, "power.store_level": soc * CAP, "power.store_capacity": CAP,
            "crop.biomass_store_kg": biomass, "crop.s0.harvest_interval": 28.0, "crop.s0.ppf_needed": 295.14}


def power(c, *args, **kw):
    return c(obs(*args, **kw))["cmd.crop_power_W"]


def test_full_battery_gives_full_light():
    assert power(LightingController(), 0.9) == pytest.approx(FULL_W)


@pytest.mark.parametrize("tick,soc,mode", [
    # young crop keeps full light down to a low charge
    (100, 0.30, "FULL"), (100, 0.20, "REDUCED"), (100, 0.10, "SURVIVAL"),
    # middle of the cycle
    (400, 0.45, "FULL"), (400, 0.30, "REDUCED"), (400, 0.15, "SURVIVAL"),
    # near harvest the same charge already dims
    (600, 0.50, "REDUCED"), (600, 0.30, "REDUCED"), (600, 0.25, "SURVIVAL"),
])
def test_level_depends_on_charge_and_crop_age(tick, soc, mode):
    c = LightingController()
    c(obs(soc, tick))
    assert c.mode == mode


def test_levels_are_fractions_of_full_light():
    c = LightingController()
    assert power(c, 0.20, 100) == pytest.approx(0.75 * FULL_W)
    assert power(c, 0.05, 100) == pytest.approx(0.30 * FULL_W)


def test_no_flicker_at_a_threshold():
    c = LightingController()
    c(obs(0.24, 100))                     # young threshold for REDUCED is 0.25
    assert c.mode == "REDUCED"
    for soc in (0.26, 0.28, 0.29):        # back above the threshold but inside the band
        c(obs(soc, 100))
        assert c.mode == "REDUCED"
    c(obs(0.31, 100))
    assert c.mode == "FULL"


def test_brightening_from_survival_goes_one_band_at_a_time():
    c = LightingController()
    c(obs(0.05, 100))
    assert c.mode == "SURVIVAL"
    c(obs(0.20, 100))                     # clears 0.12 + band, not 0.25 + band
    assert c.mode == "REDUCED"


def test_harvest_restarts_the_crop_age():
    c = LightingController()
    c(obs(0.50, tick=650, biomass=0.0))
    assert c.mode == "REDUCED", "0.50 is below the near-harvest threshold"
    c(obs(0.50, tick=673, biomass=32.8))  # harvest: a new crop was planted
    assert c.age_frac == 0.0
    c(obs(0.50, tick=680, biomass=32.8))
    assert c.mode == "FULL", "young crop again"


def test_missing_battery_reading_keeps_the_last_level():
    c = LightingController()
    c(obs(0.20, 100))
    o = obs(0.90, 101)
    o["power.store_level"] = None
    assert c(o)["cmd.crop_power_W"] == pytest.approx(0.75 * FULL_W)
    assert c.mode == "REDUCED"


def test_missing_capacity_and_ppf_fall_back_to_defaults():
    c = LightingController()
    assert c({"time.tick": 0, "power.store_level": 0.9 * CAP})["cmd.crop_power_W"] == pytest.approx(FULL_W)


# in the loop: severe dust storm on FakePlant with Toni's power supervisor

from interface import FakePlant, Fault, TickLogger, run  # noqa: E402
from supervisors import PowerBudgetSupervisor  # noqa: E402


def storm_run(lamps, ticks=24 * 16):
    # four stacked SEVERE malfunctions: solar x0.0625, about the 2018 Opportunity storm
    storm = [Fault(tick=100, kind="malfunction", target="Solar_Array",
                   params={"intensity": "SEVERE_MALF", "length": "PERMANENT_MALF"}, duration=24 * 7)
             for _ in range(4)]
    plant = FakePlant()
    plant.reset()
    log = TickLogger()
    run(plant, {"lamps": lamps, "heater": lambda o: {"cmd.heater_W": 1000.0},
                "recycler": lambda o: {"cmd.recycler_power_W": 400.0}},
        ticks, supervisor=PowerBudgetSupervisor(5000.0), fault_schedule=storm, logger=log, reset=False)
    return [r["applied"]["cmd.crop_power_W"] for r in log.records]


def test_storm_dims_the_lamps_and_recovers():
    fixed = storm_run(lambda o: {"cmd.crop_power_W": FULL_W})
    ctl, modes = LightingController(), []

    def lamps(o):
        cmds = ctl(o)
        modes.append(ctl.mode)
        return cmds

    adaptive = storm_run(lamps)
    assert "SURVIVAL" in modes[100:100 + 24 * 7]
    assert sum(adaptive) < sum(fixed), "less lamp energy over the storm"
    assert sum(w < 1 for w in adaptive) <= sum(w < 1 for w in fixed), "never dark for longer"
    assert modes[-1] == "FULL", "back to full light once the battery recharges"
