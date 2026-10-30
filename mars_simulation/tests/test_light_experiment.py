import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import light_experiment as le  # noqa: E402
from interface import FakePlant, bindings  # noqa: E402


def test_full_lamp_power_gives_exactly_the_needed_ppf():
    assert bindings.delivered_ppf(le.FULL_LAMP_W, le.CROP_AREA_M2, le.PPF_NEEDED) == pytest.approx(le.PPF_NEEDED)
    assert bindings.delivered_ppf(0.5 * le.FULL_LAMP_W, le.CROP_AREA_M2, le.PPF_NEEDED) == pytest.approx(
        0.5 * le.PPF_NEEDED)


@pytest.mark.parametrize("cond,tick,hour,expect", [
    (le.Condition("level", "x", level=0.5), 10, 3.0, 0.5),
    (le.Condition("photoperiod", "x", off_hours=8), 10, 16.0, 1.0),   # 16.66 is the first dark hour
    (le.Condition("photoperiod", "x", off_hours=8), 10, 17.0, 0.0),
    (le.Condition("blackout", "x", blackout_start=100, blackout_len=24), 99, 3.0, 1.0),
    (le.Condition("blackout", "x", blackout_start=100, blackout_len=24), 100, 3.0, 0.0),
    (le.Condition("blackout", "x", blackout_start=100, blackout_len=24), 123, 3.0, 0.0),
    (le.Condition("blackout", "x", blackout_start=100, blackout_len=24), 124, 3.0, 1.0),
])
def test_lamp_fraction_schedule(cond, tick, hour, expect):
    assert le.lamp_fraction(cond, tick, hour, 24.66) == expect


def test_blackouts_stay_inside_one_cycle():
    for c in le.conditions(["blackout"]):
        assert c.blackout_start + c.blackout_len <= 672


def test_run_condition_finds_the_fake_harvest():
    r = le.run_condition(FakePlant(), le.Condition("level", "full"))
    assert r.harvest_tick == 672
    assert r.harvest_kg == pytest.approx(25.0)
    assert not r.died
    assert r.mean_ppf == pytest.approx(le.PPF_NEEDED, rel=0.01)


def test_parse_deaths_pairs_each_death_with_the_next_harvest_tick():
    # lines as BioSim's Plant and Shelf loggers write them
    lines = [
        "biosim-server-1  | ... INFO  c.t.b.server.simulation.food.Shelf - Shelf: Harvested 32.22kg of "
        "Lettuce (5.26% inedible) after 28.0 days of growth on tick 673",
        "biosim-server-1  | ... INFO  c.t.b.server.simulation.food.Plant - Lettuce crops have died from "
        "lack of light at 0.0 days (risk was 1.52%)",
        "biosim-server-1  | ... INFO  c.t.b.server.simulation.food.Shelf - Shelf: Harvested 0.0kg of "
        "Lettuce (0.0% inedible) after 0.0 days of growth on tick 418",
    ]
    assert le.parse_deaths(lines) == [("lack of light", 418)]
