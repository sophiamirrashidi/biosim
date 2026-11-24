import pytest

from interface import bindings
from interface.schema import COMMAND_KEYS, STATE_KEYS, TRUTH_ONLY_KEYS, is_state_key, public, shelf_key

from conftest import raw_fixtures


@pytest.mark.parametrize("name,raw_state", raw_fixtures(), ids=lambda x: x if isinstance(x, str) else "")
def test_every_schema_key_is_produced(name, raw_state):
    state = bindings.to_state(raw_state)
    for key in list(STATE_KEYS) + list(TRUTH_ONLY_KEYS):
        assert key in state, key
    assert shelf_key(0, "harvest_interval") in state
    assert all(is_state_key(k) for k in state)


@pytest.mark.parametrize("name,raw_state", raw_fixtures(), ids=lambda x: x if isinstance(x, str) else "")
def test_config_validates(name, raw_state):
    assert bindings.validate(raw_state) == []


def test_derived_values(raw):
    s = bindings.to_state(raw)
    assert s["gh.co2_ppm"] == pytest.approx(1200.0)
    assert s["water.crop_draw_L"] == pytest.approx(20.0)
    assert s["time.hour_of_day"] == 0.0
    assert "temperature" not in " ".join(s)


def test_truth_only_keys_are_stripped(raw):
    obs = public(bindings.to_state(raw))
    assert not set(obs) & set(TRUTH_ONLY_KEYS)


def test_active_faults_only_lists_performed(raw):
    raw["modules"]["Solar_Array"]["malfunctions"] = [
        {"id": 3, "name": "x", "intensity": "SEVERE_MALF", "length": "PERMANENT_MALF",
         "performed": True, "tickToMalfunction": 5, "doneEnoughRepairWork": False},
        {"id": 4, "name": "y", "intensity": "LOW_MALF", "length": "TEMPORARY_MALF",
         "performed": False, "tickToMalfunction": 50, "doneEnoughRepairWork": False},
    ]
    faults = bindings.to_state(raw)["faults.active"]
    assert [f["id"] for f in faults] == [3]


@pytest.mark.parametrize("mutate,expect", [
    (lambda r: r["globals"].update(tickLength=0.5), "tickLength"),
    (lambda r: r["globals"].update(simulationIsPaused=False), "not paused"),
    (lambda r: r["modules"].pop("CO2_Injector"), "missing"),
    (lambda r: r["modules"]["Lettuce_Bed"]["consumers"][1]["rates"].update(maxFlowRates=[50]), "maxFlowRates"),
    (lambda r: r["modules"]["Lettuce_Bed"]["properties"]["shelves"][0].pop("waterLevel"), "unpatched"),
    (lambda r: r["modules"]["CO2_Injector"]["consumers"][0].update(connections=["Greenhouse.CO2Store"]),
     "environment"),
])
def test_validate_catches_config_mistakes(raw, mutate, expect):
    mutate(raw)
    problems = bindings.validate(raw)
    assert any(expect in p for p in problems), problems


def test_every_command_has_a_binding():
    assert set(bindings.COMMAND_BINDINGS) == set(COMMAND_KEYS)


def test_water_keys(raw):
    s = bindings.to_state(raw)
    assert s["water.dehumidified_L"] == pytest.approx(10.0 * 0.01801524)
    assert s["crop.s0.reservoir_capacity_L"] == 500.0
    assert s["crop.s0.reservoir_L"] == 20.0
    obs = public(s)
    assert "crop.s0.reservoir_L" not in obs, "true reservoir level must stay hidden from controllers"
    assert "crop.s0.reservoir_capacity_L" in obs


def test_irrigation_draws_from_the_potable_tank():
    assert bindings.COMMAND_BINDINGS["cmd.crop_water_L"][0].flow_type == "PotableWater"


def test_unpatched_server_fails_with_a_clear_message(raw):
    raw["modules"]["Lettuce_Bed"]["properties"]["shelves"][0].pop("waterLevel")
    with pytest.raises(bindings.BindingError, match="unpatched"):
        bindings.to_state(raw)


def test_missing_module_error_is_readable(raw):
    raw["modules"].pop("Grey_Water_Store")
    with pytest.raises(bindings.BindingError, match="Grey_Water_Store"):
        bindings.to_state(raw)


@pytest.mark.parametrize("power_W,area,expect", [
    (0.0, 10.0, 0.0),                               # lamps off
    (100.0, 10.0, 40.5),                            # below saturation: linear in power
    (364.37, 10.0, 147.57),                         # half of full light
    (728.74, 10.0, 295.14),                         # full light
    (2000.0, 10.0, 295.14),                         # saturates at what the crop needs
    (5.0, 0.0, 0.0),                                # empty shelf
])
def test_delivered_ppf_follows_shelf_light_plants(power_W, area, expect):
    assert bindings.delivered_ppf(power_W, area, 295.14) == pytest.approx(expect, rel=1e-3)


def test_led_efficacy_matches_the_config():
    from pathlib import Path
    import re
    xml = (Path(__file__).resolve().parent.parent / "configs" / "mars_lettuce.biosim").read_text()
    assert float(re.search(r'lampEfficacy="([\d.]+)"', xml).group(1)) == pytest.approx(bindings.LED_PPF_PER_W)
    assert bindings.full_light_W(10.0, 295.14) == pytest.approx(728.74, abs=0.01)


def test_delivered_ppf_uses_the_efficacy_the_server_reports(raw):
    shelf = raw["modules"]["Lettuce_Bed"]["properties"]["shelves"][0]
    raw["modules"]["Lettuce_Bed"]["consumers"][0]["rates"]["actualFlowRates"] = [364.37]
    shelf["photonEfficacy"] = 4.05
    assert bindings.to_state(raw)[shelf_key(0, "ppf_delivered")] == pytest.approx(147.57, rel=1e-3)


def test_delivered_ppf_from_bed_power_draw(raw):
    # no photonEfficacy reported: an unpatched build, high pressure sodium lamps
    bed_power = raw["modules"]["Lettuce_Bed"]["consumers"][0]["rates"]
    assert bindings.to_state(raw)[shelf_key(0, "ppf_delivered")] == pytest.approx(295.14)
    bed_power["actualFlowRates"] = [1.0]
    assert bindings.to_state(raw)[shelf_key(0, "ppf_delivered")] == pytest.approx(122.15, rel=1e-3)
    bed_power["actualFlowRates"] = [0.0]
    assert bindings.to_state(raw)[shelf_key(0, "ppf_delivered")] == 0.0
