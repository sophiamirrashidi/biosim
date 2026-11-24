import pytest

from actuators import LAMP_KEY, LightingActuatorLayer
from estimators import LampMonitor
from interface import FakePlant, Fault, TickLogger, bindings, run
from sensors import LightSensorLayer, light_key

FULL_W = bindings.full_light_W(10.0, 295.14)
PAR = light_key(0)


def replay(faults=(), ticks=120, lamp_W=FULL_W):
    """Runs FakePlant with the light sensor and lamp faults, then replays the obs through
    LampMonitor, passing the command in force during each step. Returns per-tick outputs."""
    plant = FakePlant()
    plant.reset()
    log = TickLogger()
    run(plant, {"lamps": lambda o: {LAMP_KEY: lamp_W}}, ticks, sensor_layer=LightSensorLayer(),
        actuator_layer=LightingActuatorLayer(), fault_schedule=list(faults), logger=log, reset=False)
    mon, asked, rows = LampMonitor(), None, []
    for rec in log.records:
        rows.append(dict(mon.update(rec["obs"], asked), tick=rec["tick"]))
        asked = rec["commands"]["lamps"][LAMP_KEY]
    return rows


def lamp_fault(kind, **params):
    return Fault(tick=40, kind=kind, target=LAMP_KEY, params=params, duration=30)


def sensor_fault(kind, **params):
    return Fault(tick=40, kind=kind, target=PAR, params=params, duration=30)


def first(rows, label):
    return next((r["tick"] for r in rows if label in r["lamp.faults"]), None)


def test_nominal_run_has_no_alarms():
    rows = replay()
    assert all(r["lamp.faults"] == [] for r in rows)
    assert all(r["lamp.ppf"] == pytest.approx(295.14, abs=3.0) for r in rows[1:])


@pytest.mark.parametrize("kind,params,ratio", [
    ("efficiency", {"factor": 0.6}, 0.6),
    ("dropout", {}, 0.0),
    ("stuck", {"value": 2000.0}, 2000.0 / FULL_W),
])
def test_lamp_faults_are_flagged_with_the_power_ratio_and_the_sensor_stays_trusted(kind, params, ratio):
    rows = replay([lamp_fault(kind, **params)])
    caught = first(rows, "lamp")
    assert caught is not None and 41 <= caught <= 43, "the draw shows one tick later, then lamp_ticks"
    during = [r for r in rows if caught <= r["tick"] <= 70]
    assert all(r["lamp.ratio"] == pytest.approx(ratio, rel=1e-3) for r in during)
    assert all(r["lamp.sensor_ok"] for r in rows), "the sensor agrees with the lower light"
    assert first([r for r in rows if r["tick"] < 40], "lamp") is None
    assert rows[-1]["lamp.faults"] == [], "clears after the fault"


@pytest.mark.parametrize("kind,params,label,max_delay", [
    ("bias", {"offset": -15.0}, "light_sensor_offset", 3),    # the SQ-500's 5 % calibration error
    ("drift", {"rate": 1.0}, "light_sensor_offset", 15),
    ("stuck", {}, "light_sensor_stuck", 3),
    ("dropout", {}, "light_sensor_dropout", 0),
])
def test_sensor_faults_are_told_apart_from_lamp_faults(kind, params, label, max_delay):
    rows = replay([sensor_fault(kind, **params)])
    caught = first(rows, label)
    assert caught is not None and caught - 40 <= max_delay
    assert first(rows, "lamp") is None, "the lamps are fine"
    during = [r for r in rows if caught <= r["tick"] < 70]
    assert all(not r["lamp.sensor_ok"] for r in during)
    assert all(r["lamp.ppf"] == pytest.approx(295.14, abs=1.0) for r in during), "falls back to the power-based light"


@pytest.mark.parametrize("battery,alarm", [(10000.0, False), (150000.0, True)])
def test_shortfall_is_only_a_lamp_fault_when_the_battery_could_cover_it(battery, alarm):
    # the same 40 % shortfall for 10 ticks: on a low battery the supervisor may be cutting the
    # lamps on purpose, so only a full battery makes it a fault
    mon = LampMonitor()
    obs = {"power.crop_draw": 0.6 * FULL_W, "power.store_level": battery, PAR: 0.6 * 295.14}
    outs = [mon.update(obs, FULL_W) for _ in range(10)]
    assert any("lamp" in o["lamp.faults"] for o in outs) == alarm


def test_lamps_off_is_not_a_stuck_sensor():
    rows = replay(lamp_W=0.0, ticks=30)
    assert all(r["lamp.faults"] == [] for r in rows)
