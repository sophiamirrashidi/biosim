
"""
Irrigation and Power Sensor Layer
=================================

Purpose:
    Simulate imperfect sensor observations for the Mars greenhouse.

Architecture:
    BioSim -> bindings.py -> this sensor layer -> controllers

The incoming state already contains normalized water/power
measurements. We do NOT directly access BioSim modules here.

Supported sensor faults:
    noise   - random Gaussian measurement error
    bias    - consistent measurement offset
    dropout - measurement becomes unavailable (None)

The underlying simulation state is never modified.

Usage:
    run(
        plant,
        controllers,
        n_ticks=100,
        sensor_layer=make_water_power_sensor_layer(seed=42),
        fault_schedule=[...]
    )
"""

import random

# Explicitly list the measurements owned by this subsystem.
# Other teammates can manage greenhouse, CO2, and plant sensors.
WATER_POWER_KEYS = frozenset({
    "water.potable_L",
    "water.grey_L",
    "water.dirty_L",
    "water.crop_draw_L",
    "power.store_level",
    "power.store_capacity",
    "power.solar_out",
    "power.crop_draw",
})


def make_water_power_sensor_layer(seed=42):
    """
    Build a sensor callback compatible with interface.loop.run().

    Parameters:
        seed: Random seed for repeatable noise experiments.

    Returns:
        A function sensor_layer(truth, ctx) -> observations.

    'truth' contains the actual simulated measurements.
    'ctx.active_faults' contains faults active at this tick.

    The returned observations are what controllers will see.
    """

    rng = random.Random(seed)

    def sensor_layer(truth, ctx):
        # Copy the input so sensor faults never alter ground truth.
        observations = dict(truth)

        for fault in ctx.active_faults:

            # Ignore faults owned by other sensor subsystems.
            if fault.target not in WATER_POWER_KEYS:
                continue

            key = fault.target

            # Skip unavailable measurements.
            if key not in observations:
                continue

            if fault.kind == "dropout":
                # Missing measurement does NOT mean zero.
                observations[key] = None

            elif fault.kind == "bias":
                # Consistent calibration error, e.g. -100 L.
                if observations[key] is not None:
                    offset = float(
                        fault.params.get("offset", 0.0)
                    )
                    observations[key] += offset

            elif fault.kind == "noise":
                # Random error around the actual measurement.
                if observations[key] is not None:
                    sigma = float(
                        fault.params.get("sigma", 0.0)
                    )

                    if sigma < 0:
                        raise ValueError(
                            "Noise sigma must be nonnegative"
                        )

                    observations[key] += rng.gauss(
                        0.0, sigma
                    )

        return observations

    return sensor_layer
