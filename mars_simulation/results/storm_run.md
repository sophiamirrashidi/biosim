# Dust storm runs: lighting control in the full system

Run 2026-11-24 with `scripts/storm_run.py` against the Docker BioSim server (LED lamps, current main).

Each run is one 700-tick lettuce cycle on `HybridPlant` (BioSim plus the thermal model), with every subsystem in the loop:

- **Heater:** HVAC holding 22 °C.
- **Lamps:** fixed at full light (729 W), or `LightingController`.
- **Water:** irrigation with water recycling.
- **CO2:** held near 1200 ppm.
- **Sensors and actuators:** the PAR light sensor, plus lamp and atmosphere actuator faults.
- **Power:** Toni's `PowerBudgetSupervisor`, which serves the heater first, then the lamps, then the recycler, and keeps a 5 kWh reserve.

**The storm:** four stacked SEVERE malfunctions on `Solar_Array`. Checked live, solar drops to about 6 % (about the 2018 Opportunity storm) and recovers fully when they clear. The battery holds 200 kWh and is full when the storm starts at tick 100.

## Results

Columns: harvest, mean delivered PPF, hours with the lamps dark, hours the heater got less than it asked for, lowest battery, and greenhouse temperature range.

**No storm, and 14-day storms**

| Scenario | Harvest | PPF | Dark h | Heater short h | Battery min | Temperature |
|---|---:|---:|---:|---:|---:|---|
| No storm | 29.7 kg | 295 | 0 | 0 | 95.3 kWh | 21.3–29.4 °C |
| Early storm (tick 100), fixed lamps | 29.7 kg | 295 | 0 | 0 | 20.5 kWh | 21.3–29.4 °C |
| Early storm, controller v1 (charge-based) | 26.2 kg | 273 | 0 | 0 | 25.7 kWh | 21.0–29.6 °C |
| Early storm, **controller v2** | **29.5 kg** | 290 | 0 | 0 | 22.2 kWh | 21.0–29.4 °C |
| Late storm (tick 400), fixed lamps | 29.8 kg | 295 | 0 | 0 | 30.1 kWh | 21.3–29.4 °C |
| Late storm, **controller v2** | **30.6 kg** | 287 | 0 | 0 | 37.3 kWh | 21.1–29.4 °C |

**21-day storms**

| Scenario | Harvest | PPF | Dark h | Heater short h | Battery min | Temperature |
|---|---:|---:|---:|---:|---:|---|
| Early storm, fixed lamps | 16.9 kg | 235 | 137 | 137 | 5.0 kWh | −17.7–31.4 °C |
| Early storm, controller v1 | 16.1 kg | 205 | 120 | 120 | 5.0 kWh | −17.2–31.9 °C |
| Early storm, **controller v2** | **17.5 kg** | 228 | 130 | 130 | 5.0 kWh | −17.5–31.4 °C |

The late 21-day storm gives the same numbers as the late 14-day one: it starts at tick 400 and the run ends at 700, before the storm does.

**Faults** (controller v2, no storm):

- **Lamps at 70 % efficiency (ticks 200–271):** caught after 2 h. The controller asked for 1/0.7 of the power, and delivered light stayed at 295 for the whole fault.
- **Light sensor biased −15 (ticks 350–421):** caught in the first tick. The lamps weren't moved.

## What changed between v1 and v2, and why

**v1** dimmed on battery charge alone, with age-dependent thresholds. The storm runs showed two problems:

1. **It dimmed for nothing.** In the 14-day storm, fixed lamps never ran the battery below 20 kWh and lost no yield. v1 dimmed "just in case" and lost 3.5 kg.
2. **Dimming in the cold saves almost nothing.** The thermal model counts 90 % of lamp power as greenhouse heat. While the heater is running, dimming only makes the heater replace that heat: about 10 % of the energy saved, all of the light lost.

**v2:**

- **Dims on hours left, not on charge.** It estimates the battery's remaining hours above the reserve from the drain over the last sol (24 ticks, so day and night even out).
- **Dims sooner for an older crop.** It dims below 48 h left for a young crop, 96 h in the middle of the cycle, and 168 h near harvest, and goes to SURVIVAL below half of that.
- **Dims only while the heater is idle**, which in practice means daytime.

## Findings

- **v2 no longer costs yield in a storm the battery can ride out.** 29.5 vs 29.7 kg early, and 30.6 vs 29.8 kg late, while keeping more charge in hand (22–37 vs 20–30 kWh).
- **In a 21-day storm, cold is the real danger, and lighting can't fix it.** The battery reaches the reserve, the heater is starved for about 130 h, and the greenhouse freezes to about −17 °C whatever the lamps do. v2 is the best of the three (17.5 kg), but only slightly.
  - Recommendation for the heater and power owners: a storm mode that lowers the heating setpoint towards the crop's survival limit, so less battery goes on heat.
- **Lamp heat matters to the energy balance.** A lighting policy that ignores it either wastes light (dimming while heating) or wastes energy (full light on warm days). This interaction is the main result of these runs.

## Limits

- **One run per scenario.** BioSim's growth is close to deterministic here; the earlier light sweeps matched within 0.2 kg run to run.
- **Model assumptions:** the temperature comes from the thermal model with a fixed 22 °C setpoint, and the 200 kWh battery and 20 kW solar array come from `configs/mars_lettuce.biosim`.
- **The thresholds aren't tuned.** The horizons (48/96/168 h) follow the light experiment's age sensitivity.
