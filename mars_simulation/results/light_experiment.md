# Light experiment: how BioSim lettuce responds to light

Run 2026-10-26 with `scripts/light_experiment.py --reps 2` against the Docker BioSim server. One 28-day lettuce cycle (10 m²) per run. Lamps follow a fixed schedule. CO2 is held near 1200 ppm and grey water at 20 L/tick, so light is the only thing that varies. Full light means 295 µmol/m²/s, which BioSim delivers at 2.42 W of lamp power (see `bindings.delivered_ppf`).

## Results

| Condition | Mean PPF | Harvest (kg) | Died |
|---|---:|---:|---|
| Full light | 295 | 32.8 | 0/2 |
| Dimmed to 90 % | 265 | 25.0 | 0/2 |
| Dimmed to 75 % | 221 | 14.4 | 0/2 |
| Dimmed to 50 % | 147 | 4.2 | 0/2 |
| Dimmed to 25 % | 74 | 0.4 | 0/2 |
| Dimmed to 10 % | 30 | ~0 | 1/2, lack of light at tick 629 |
| Lamps off all cycle | 0 | 0 | 2/2, lack of light at ticks 414 and 467 |
| Off 4 h every sol | 247 | 20.5 | 0/2 |
| Off 8 h every sol | 200 | 11.0 | 0/2 |
| Off 12 h every sol | 153 | 4.8 | 0/2 |
| Off 16 h every sol | 105 | 1.4 | 0/2 |
| 24 h blackout at tick 100 / 336 / 550 | 284 | 28.2 / 30.5 / 32.2 | 0/2 each |
| 72 h blackout at tick 100 / 336 / 550 | 263 | 19.1 / 25.6 / 31.2 | 0/2 each |
| 168 h blackout at tick 100 / 336 | 221 | 8.4 / 20.0 | 0/2 each |
| 336 h blackout at tick 100 / 336 | 147 | 2.3 / 17.3 | 0/2 each |

The two runs of each condition agreed to within 0.2 kg.

## What it means

1. **Yield is very sensitive to light.** At 75 % PPF the crop loses more than half its yield.
2. **Lamp schedules shouldn't turn lamps off in BioSim.** The crop model already assumes a 16 h photoperiod (`Lettuce.getPhotoperiod`), so turning lamps off counts the dark hours twice: 8 h off costs 66 %. Proposal: keep the lamps on at full PPF in BioSim, and model the 16 h schedule only in the power and heat accounting.
3. **When light is lost matters more than how much.** A 72 h blackout costs 42 % of the yield at tick 100 but only 5 % at tick 550. Early PPF sets canopy closure (`Plant.java` canopy-closure PPF list). So when power is short in a dust storm, protect lighting while the crop is young and shed it first near harvest.
4. **Light faults cause yield loss, not death.** No blackout of up to 14 days killed the crop. The crop only died when light stayed at or below 10 % for most of the cycle. Score light faults on yield.
5. **Crop death can't be seen over REST.** `Plant.kill()` resets the crop before the shelf harvests it, so a death adds 0 kg and no state key changes. The script gets deaths from BioSim's container log. Fault detection can't observe crop death from `obs`.
6. **CO2 has to be controlled in every run.** Without injection the crop pulls greenhouse CO2 to 0 ppm by about tick 288 and dies of low CO2 (1.4 kg harvest instead of 32.8 kg).

## Open questions for the meeting

- Power (Toni): does the supervisor take a crop-age-dependent lighting priority (point 3)?
- Power and thermal (Toni, Ameya): a separate LED load of about 1.2 kW on `Power_Store` that is also a heat source in the thermal model. This is a config change.
- Atmosphere: a CO2 controller is a prerequisite for any meaningful crop result (point 6).
- Fault detection: how should crop death be scored when it isn't visible over REST (point 5)?
