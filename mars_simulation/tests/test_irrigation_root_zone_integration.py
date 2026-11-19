
"""Test irrigation behavior when the root-zone sensor drops out."""

from controllers import IrrigationController
from estimators import RootZoneMonitor
from interface import FakePlant, Fault, run
from sensors import RootZoneSensorLayer


def test_irrigation_continues_during_sensor_dropout():
    # Enable the fault-aware irrigation controller.
    controller = IrrigationController(
        root_zone_monitor=RootZoneMonitor()
    )

    observations = []

    def irrigation(obs):
        # Generate a water command from the observed state.
        commands = controller(obs)

        # Record the sensor reading, trusted source, and command.
        observations.append({
            "tick": obs["time.tick"],
            "reading": obs["crop.s0.root_zone_L"],
            "trusted": controller.last_root_zone["rz.trusted"],
            "water_command": commands["cmd.crop_water_L"],
        })

        return commands

    # The root-zone sensor stops reporting from tick 50 to 59.
    fault = Fault(
        tick=50,
        kind="dropout",
        target="crop.s0.root_zone_L",
        duration=10,
    )

    # Run the greenhouse simulation without Docker.
    run(
        FakePlant(),
        {"irrigation": irrigation},
        90,
        sensor_layer=RootZoneSensorLayer(seed=42),
        fault_schedule=[fault],
    )

    during = [
        row for row in observations
        if 50 <= row["tick"] < 60
    ]

    # The sensor should be unavailable for exactly 10 ticks.
    assert len(during) == 10
    assert all(row["reading"] is None for row in during)

    # The monitor should rely on the model-based estimate.
    assert all(row["trusted"] == "estimate" for row in during)

    # Irrigation commands should remain valid.
    assert all(
        0 <= row["water_command"] <= 200
        for row in during
    )

    # The sensor should resume after the fault.
    recovered = next(
        row for row in observations
        if row["tick"] == 60
    )
    assert recovered["reading"] is not None



def test_irrigation_detects_biased_root_zone_sensor():
    """A biased moisture sensor should not be trusted after detection."""

    controller = IrrigationController(
        root_zone_monitor=RootZoneMonitor()
    )

    records = []

    def irrigation(obs):
        commands = controller(obs)

        records.append({
            "tick": obs["time.tick"],
            "trusted": controller.last_root_zone["rz.trusted"],
            "faults": list(controller.last_root_zone["rz.faults"]),
            "command": commands["cmd.crop_water_L"],
        })

        return commands

    fault = Fault(
        tick=50,
        kind="bias",
        target="crop.s0.root_zone_L",
        params={"offset": -2.0},
        duration=20,
    )

    run(
        FakePlant(),
        {"irrigation": irrigation},
        90,
        sensor_layer=RootZoneSensorLayer(seed=42),
        fault_schedule=[fault],
    )

    # While the bias is active, the monitor should identify an
    # offset and rely on the estimator instead of the bad sensor.
    during_fault = [
        row for row in records
        if 50 <= row["tick"] < 70
    ]

    assert any(
        "offset" in row["faults"]
        and row["trusted"] == "estimate"
        for row in during_fault
    )

    # The controller must still produce valid commands.
    assert all(
        0 <= row["command"] <= 200
        for row in records
    )

