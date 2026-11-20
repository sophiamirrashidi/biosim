
"""Unit and integration tests for water/power sensors."""

from interface import FakePlant, Fault, TickLogger, run
from interface.loop import TickContext
from interface.water_power_sensors import (
    make_water_power_sensor_layer,
)


def test_normal_measurements():
    """Without faults, observations equal the input state."""

    truth = {
        "water.grey_L": 750.0,
        "power.crop_draw": 400.0,
    }

    sensor = make_water_power_sensor_layer()
    ctx = TickContext(tick=0)

    obs = sensor(truth, ctx)

    assert obs == truth
    assert obs is not truth


def test_sensor_bias():
    """A biased sensor reports a consistent offset."""

    truth = {
        "water.grey_L": 750.0,
        "power.crop_draw": 400.0,
    }

    fault = Fault(
        tick=0,
        kind="bias",
        target="water.grey_L",
        params={"offset": -100.0},
    )

    sensor = make_water_power_sensor_layer()
    ctx = TickContext(tick=0, active_faults=[fault])

    obs = sensor(truth, ctx)

    assert obs["water.grey_L"] == 650.0
    assert obs["power.crop_draw"] == 400.0
    assert truth["water.grey_L"] == 750.0


def test_power_sensor_dropout():
    """Missing sensor output must not be confused with zero power."""

    truth = {"power.crop_draw": 400.0}

    fault = Fault(
        tick=0,
        kind="dropout",
        target="power.crop_draw",
    )

    sensor = make_water_power_sensor_layer()
    ctx = TickContext(tick=0, active_faults=[fault])

    obs = sensor(truth, ctx)

    assert obs["power.crop_draw"] is None
    assert truth["power.crop_draw"] == 400.0


def test_noise_reproducible():
    """The same seed produces the same noise sequence."""

    truth = {"water.grey_L": 750.0}

    fault = Fault(
        tick=0,
        kind="noise",
        target="water.grey_L",
        params={"sigma": 5.0},
    )

    ctx = TickContext(tick=0, active_faults=[fault])

    sensor_a = make_water_power_sensor_layer(seed=42)
    sensor_b = make_water_power_sensor_layer(seed=42)

    obs_a = sensor_a(truth, ctx)
    obs_b = sensor_b(truth, ctx)

    assert obs_a == obs_b
    assert obs_a["water.grey_L"] != 750.0


def test_fault_timing_in_simulation_loop():
    """Dropout occurs only during its scheduled tick window."""

    fault = Fault(
        tick=1,
        kind="dropout",
        target="water.grey_L",
        duration=2,
    )

    log = TickLogger()

    run(
        FakePlant(),
        {},
        4,
        sensor_layer=make_water_power_sensor_layer(),
        fault_schedule=[fault],
        logger=log,
    )

    records = log.records

    assert records[0]["obs"]["water.grey_L"] is not None
    assert records[1]["obs"]["water.grey_L"] is None
    assert records[2]["obs"]["water.grey_L"] is None
    assert records[3]["obs"]["water.grey_L"] is not None

