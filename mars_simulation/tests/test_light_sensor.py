import statistics

import pytest

from interface import FakePlant, Fault, TickLogger, bindings, run
from sensors import (AtmosphereSensorLayer, LightSensorLayer, RootZoneSensorLayer, ROOT_ZONE_KEY,
                     chain_sensor_layers, light_key)

PAR = light_key(0)
ONE_WATT_PPF = bindings.LAMP_PPF_PER_W / 10.0   # what 1 W of lamp power delivers on the 10 m2 shelf


def lamps(watts):
    return lambda obs: {"cmd.crop_power_W": watts}


def sensed(layer, ticks=200, watts=1.0, faults=()):
    """Runs FakePlant with fixed lamps; returns (sensed PPF, true PPF) per tick from tick 1 on
    (at tick 0 the lamps haven't drawn anything yet)."""
    seen, log = [], TickLogger()
    run(FakePlant(), {"lamps": lamps(watts), "spy": lambda obs: seen.append(obs.get(PAR)) or {}},
        ticks, sensor_layer=layer, fault_schedule=faults, logger=log)
    return seen[1:], [r["truth"][PAR] for r in log.records][1:]


def fault(kind, tick=50, duration=20, **params):
    return Fault(tick=tick, kind=kind, target=PAR, params=params, duration=duration)


def test_nominal_reading_is_unbiased_with_the_spec_noise():
    reading, truth = sensed(LightSensorLayer(), ticks=2000)
    assert all(t == pytest.approx(ONE_WATT_PPF) for t in truth)
    assert statistics.fmean(reading) == pytest.approx(ONE_WATT_PPF, abs=0.15)
    assert statistics.stdev(reading) == pytest.approx(1.5, rel=0.1)


def test_controllers_see_the_sensed_value_and_the_log_keeps_the_truth():
    reading, truth = sensed(LightSensorLayer(), ticks=20)
    assert reading != truth
    assert all(t == pytest.approx(ONE_WATT_PPF) for t in truth)


def test_dark_lamps_never_read_negative():
    reading, _ = sensed(LightSensorLayer(sigma_umol=5.0), watts=0.0)
    assert min(reading) == 0.0


@pytest.mark.parametrize("kind,params,check", [
    ("bias", {"offset": -15.0}, lambda r, t: r == pytest.approx(t - 15.0)),
    ("drift", {"rate": 1.0}, lambda r, t: r > t),
    ("dropout", {}, lambda r, t: r is None),
])
def test_scheduled_faults_reach_the_reading(kind, params, check):
    reading, truth = sensed(LightSensorLayer(sigma_umol=0.0), ticks=100, faults=[fault(kind, **params)])
    during = range(50 - 1, 70 - 1)   # sensed() drops tick 0
    assert all(check(reading[i], truth[i]) for i in during)
    assert all(reading[i] == pytest.approx(truth[i]) for i in range(len(reading)) if i not in during)


def test_stuck_sensor_hides_a_lamp_change():
    seen = []

    def lamp_step(obs):
        return {"cmd.crop_power_W": 1.0 if obs["time.tick"] < 50 else 2.0}

    run(FakePlant(), {"lamps": lamp_step, "spy": lambda obs: seen.append(obs.get(PAR)) or {}}, 80,
        sensor_layer=LightSensorLayer(sigma_umol=0.0), fault_schedule=[fault("stuck", tick=45, duration=30)])
    assert seen[45:75] == [pytest.approx(ONE_WATT_PPF)] * 30, "frozen at the pre-fault reading"
    assert seen[76] == pytest.approx(2 * ONE_WATT_PPF)


def test_chains_with_the_atmosphere_and_root_zone_layers():
    layer = chain_sensor_layers(AtmosphereSensorLayer(seed=1), RootZoneSensorLayer(), LightSensorLayer())
    seen = []
    run(FakePlant(), {"lamps": lamps(1.0), "spy": lambda obs: seen.append(obs) or {}}, 5, sensor_layer=layer)
    assert all(PAR in o and ROOT_ZONE_KEY in o and "gh.co2_ppm" in o for o in seen)
