
"""Tests for the shared greenhouse power budget."""

import pytest

from interface import FakePlant, Fault, TickContext, TickLogger, run
from supervisors import PowerBudgetSupervisor


def requests():
    return {
        "heating": {"cmd.heater_W": 4000.0},
        "lighting": {"cmd.crop_power_W": 3000.0},
        "recycling": {"cmd.recycler_power_W": 1540.0},
        "irrigation": {"cmd.crop_water_L": 2.0},
    }


def allocate(battery_Wh):
    supervisor = PowerBudgetSupervisor(reserve_Wh=5000.0)
    obs = {"power.store_level": battery_Wh}
    return supervisor(obs, requests(), TickContext(tick=0))


def test_enough_power_preserves_all_requests():
    cmds = allocate(100000.0)

    assert cmds["cmd.heater_W"] == 4000.0
    assert cmds["cmd.crop_power_W"] == 3000.0
    assert cmds["cmd.recycler_power_W"] == 1540.0


def test_shortage_respects_priorities():
    # 10,000 Wh stored - 5,000 Wh reserve = 5,000 W budget.
    cmds = allocate(10000.0)

    assert cmds["cmd.heater_W"] == 4000.0
    assert cmds["cmd.crop_power_W"] == 1000.0
    assert cmds["cmd.recycler_power_W"] == 0.0


def test_non_power_commands_are_unchanged():
    cmds = allocate(10000.0)

    assert cmds["cmd.crop_water_L"] == 2.0


def test_low_battery_protects_reserve():
    cmds = allocate(4000.0)

    assert cmds["cmd.heater_W"] == 0.0
    assert cmds["cmd.crop_power_W"] == 0.0
    assert cmds["cmd.recycler_power_W"] == 0.0


def test_fakeplant_delivers_allocated_power():
    # Test the supervisor in the existing simulation loop.
    plant = FakePlant()
    plant.power = 8500.0

    supervisor = PowerBudgetSupervisor(reserve_Wh=5000.0)
    logger = TickLogger()

    controllers = {
        "heating": lambda obs: {"cmd.heater_W": 3000.0},
        "lighting": lambda obs: {"cmd.crop_power_W": 2000.0},
        "recycling": lambda obs: {"cmd.recycler_power_W": 1540.0},
    }

    final = run(
        plant,
        controllers,
        n_ticks=1,
        supervisor=supervisor,
        logger=logger,
        reset=False,
    )

    applied = logger.records[0]["applied"]

    # The battery can supply 3500 W above its reserve.
    assert applied["cmd.heater_W"] == 3000.0
    assert applied["cmd.crop_power_W"] == 500.0
    assert applied["cmd.recycler_power_W"] == 0.0

    # Tick zero has no solar generation in FakePlant.
    assert final["power.store_level"] == pytest.approx(5000.0)
    assert final["power.crop_draw"] == pytest.approx(500.0)
    assert final["power.heater_draw"] == pytest.approx(3000.0)


def test_dust_storm_reduces_solar_and_preserves_battery_reserve():
    """Power supervisor protects battery during reduced solar production."""

    def simulate(with_storm):
        plant = FakePlant()

        # Start with a low battery to expose power shortages.
        plant.power = 8000.0

        logger = TickLogger()
        supervisor = PowerBudgetSupervisor(reserve_Wh=5000.0)

        controllers = {
            "heating": lambda obs: {"cmd.heater_W": 4000.0},
            "lighting": lambda obs: {"cmd.crop_power_W": 3000.0},
            "recycling": lambda obs: {"cmd.recycler_power_W": 1540.0},
        }

        # Solar output is halved from ticks 10 through 29.
        faults = [
            Fault(
                tick=10,
                kind="malfunction",
                target="Solar_Array",
                params={
                    "intensity": "SEVERE_MALF",
                    "length": "PERMANENT_MALF",
                },
                duration=20,
            )
        ] if with_storm else []

        run(
            plant,
            controllers,
            n_ticks=50,
            supervisor=supervisor,
            fault_schedule=faults,
            logger=logger,
            reset=False,
        )

        return logger.records, plant

    normal, _ = simulate(False)
    storm, storm_plant = simulate(True)

    # Observed solar output has a one-tick delay:
    # the reading at tick 11 reflects generation during tick 10.
    for tick in range(11, 31):
        assert storm[tick]["truth"]["power.solar_out"] == pytest.approx(
            normal[tick]["truth"]["power.solar_out"] * 0.5
        )

    power_keys = (
        "cmd.heater_W",
        "cmd.crop_power_W",
        "cmd.recycler_power_W",
    )

    # Confirm the supervisor never approves more than the
    # available battery energy above the protected reserve.
    for record in storm:
        battery = record["obs"]["power.store_level"]
        approved = sum(
            record["applied"].get(key, 0.0)
            for key in power_keys
        )

        assert approved <= max(0.0, battery - 5000.0) + 1e-6

        # The physical battery must remain above the reserve.
        assert record["truth"]["power.store_level"] >= 5000.0 - 1e-6

    # Solar output should recover after the storm ends.
    assert storm_plant.solar_scale == pytest.approx(1.0)
