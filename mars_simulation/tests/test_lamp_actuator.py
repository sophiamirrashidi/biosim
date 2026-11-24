import pytest

from actuators import AtmosphereActuatorLayer, LAMP_KEY, LightingActuatorLayer, chain_actuator_layers
from interface import FakePlant, Fault, TickContext, TickLogger, bindings, run

FULL_W = bindings.full_light_W(10.0, 295.14)
PPF = "crop.s0.ppf_delivered"


def lamp_fault(kind, tick=10, duration=10, **params):
    return Fault(tick=tick, kind=kind, target=LAMP_KEY, params=params, duration=duration)


def lamp_run(faults, actuator_layer=None, ticks=30):
    log = TickLogger()
    run(FakePlant(), {"lamps": lambda o: {LAMP_KEY: FULL_W}}, ticks,
        actuator_layer=actuator_layer or LightingActuatorLayer(), fault_schedule=faults, logger=log)
    # commands at tick k show up in the state at tick k+1
    return [(r["applied"][LAMP_KEY], nxt["truth"]["power.crop_draw"], nxt["truth"][PPF])
            for r, nxt in zip(log.records, log.records[1:])]


@pytest.mark.parametrize("kind,params,factor", [
    ("efficiency", {"factor": 0.6}, 0.6),   # failed LED driver strings
    ("stuck", {"value": 0.0}, 0.0),         # relay stuck open
    ("dropout", {}, 0.0),                   # dead lamp circuit
])
def test_lamp_faults_cut_power_and_light(kind, params, factor):
    rows = lamp_run([lamp_fault(kind, **params)])
    for tick, (applied, draw, ppf) in enumerate(rows):
        expect = factor if 10 <= tick < 20 else 1.0
        assert applied == pytest.approx(expect * FULL_W)
        assert draw == pytest.approx(expect * FULL_W)
        assert ppf == pytest.approx(expect * 295.14, abs=1e-6)


def test_stuck_on_can_push_the_lamps_past_full_light():
    rows = lamp_run([lamp_fault("stuck", value=2000.0)])
    applied, draw, ppf = rows[12]
    assert draw == pytest.approx(2000.0)
    assert ppf == pytest.approx(295.14), "extra power is heat only"


def test_lamp_layer_leaves_other_commands_alone():
    layer = LightingActuatorLayer()
    out = layer({LAMP_KEY: 500.0, "cmd.heater_W": 3000.0},
                TickContext(tick=0, active_faults=[Fault(0, "dropout", "cmd.heater_W")]))
    assert out == {LAMP_KEY: 500.0, "cmd.heater_W": 3000.0}


def test_atmosphere_layer_default_keys_are_unchanged():
    layer = AtmosphereActuatorLayer()
    out = layer({LAMP_KEY: 500.0}, TickContext(tick=0, active_faults=[lamp_fault("dropout", tick=0)]))
    assert out == {LAMP_KEY: 500.0}


def test_chains_with_the_atmosphere_layer():
    layer = chain_actuator_layers(AtmosphereActuatorLayer(), LightingActuatorLayer())
    faults = [lamp_fault("efficiency", tick=0, factor=0.5), Fault(0, "stuck", "cmd.heater_W", {"value": 0.0})]
    out = layer({LAMP_KEY: 600.0, "cmd.heater_W": 3000.0}, TickContext(tick=0, active_faults=faults))
    assert out == {LAMP_KEY: 300.0, "cmd.heater_W": 0.0}
