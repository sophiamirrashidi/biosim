# CSCI 513 Cyber-Physical Systems: Formal Safety, Runtime Assurance & Verification Context

**Course:** CSCI 513 (Autonomous Cyber-Physical Systems)  
**System Under Test:** Martian Bioregenerative Life Support Habitat (NASA BioSim 2.0.0 + Python CPS Architecture)  
**Target Environment:** Dockerized BioSim Plant Engine (`biosim-server`) & NASA OpenMCT Dashboard (`openmct-biosim`)  

---

## 1. Cyber-Physical Architecture & Execution Boundary

The system operates as a classic distributed Cyber-Physical System where the **Cyber Supervisor & Safety Monitor** interacts with the **Physical Plant** across a network container boundary:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           CYBER SUPERVISORY LAYER (Host)                        │
│                                                                                 │
│   ┌───────────────────────────┐                 ┌───────────────────────────┐   │
│   │    Complex Controller     │                 │   Simplex Fail-Safe Shield│   │
│   │   (Uncertified PID/RL)    │                 │   (Verified Emergency CBF)│   │
│   └─────────────┬─────────────┘                 └─────────────▲─────────────┘   │
│                 │ Proposed: u_perf                            │ Override: u_safe│
│                 ▼                                             │                 │
│        ┌───────────────────┐        Safety Hazard Imminent    │                 │
│        │  Safety Monitor   ├──────────────────────────────────┘                 │
│        │ (STL Robustness ρ)│                                                    │
│        └────────┬──────────┘                                                    │
│                 │                                                               │
│                 │ Certified Safe Actuation (u_certified)                        │
└─────────────────┼───────────────────────────────────────────────────────────────┘
                  │ REST Network Boundary: HTTP POST /tick, /temperature, /consumers
┌─────────────────┼───────────────────────────────────────────────────────────────┐
│                 ▼                                                               │
│   ┌─────────────────────────────────────────────────────────────────────────┐   │
│   │                    Docker Container: biosim-server                      │   │
│   │                    (NASA BioSim 2.0.0 Java Physical Plant)              │   │
│   │                                                                         │   │
│   │   - Crop Biology: Canopy closure, daily carbon gain, transpiration      │   │
│   │   - Mass Conservation: Potable, grey, dirty water & O2/CO2 stores       │   │
│   │   - Electrical Grid: Battery storage, solar generation, power sinks     │   │
│   └────────────────────────────────────┬────────────────────────────────────┘   │
│                                        │ Telemetry Stream (JSON)                │
│                                        ▼                                        │
│   ┌─────────────────────────────────────────────────────────────────────────┐   │
│   │                    Docker Container: openmct-biosim                     │   │
│   │                (NASA OpenMCT Real-Time Telemetry Dashboard)             │   │
│   │                                                                         │   │
│   │   - Sensed vs Truth Telemetry, Fused State Estimates                    │   │
│   │   - Real-Time STL Robustness Degree Signals: ρ_temp(t), ρ_water(t)      │   │
│   └─────────────────────────────────────────────────────────────────────────┘   │
│                             PHYSICAL PLANT & SENSING LAYER                      │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Docker Boundary Properties:
1. **Network Discretization & Delay:** All commands (`cmd.heater_W`, `cmd.crop_power_W`, `cmd.crop_water_L`, `cmd.co2_inject_mol`) cross an HTTP REST boundary into the container. BioSim stores report values from the previous tick, enforcing a strict $1\text{-tick}$ actuation delay:
   $$x(t + 1) = f(x(t), u(t - 1))$$
2. **Sensor Isolation & Fault Injection:** Physical plant state ($x_{\text{true}}$) inside the container is hidden. The Python sensor layer injects physical imperfections (thermal lag $\tau = 0.5\text{h}$, calibration drift, Gaussian noise, NDIR cross-sensitivity, dropouts) to test estimator and controller resilience.
3. **Reproducibility:** Docker isolates execution from host OS variations, guaranteeing exact reproducibility for mission replays and fault injection schedules.

---

## 2. Formal Safety Specifications in Signal Temporal Logic (STL)

In accordance with CSCI 513 formal methods, system requirements are expressed in **Signal Temporal Logic (STL)** over continuous trajectories over horizon $\mathcal{T} = [0, T_{\text{mission}}]$ ($T_{\text{mission}} = 672\text{ hours} \approx 28\text{ Martian sols}$):

### Specification 1: Thermal Survival & Operational Health ($\phi_{\text{temp}}$)
The habitat air temperature $T_{\text{in}}(t)$ must never cross biological death limits ($7.0^\circ\text{C}$ to $32.0^\circ\text{C}$), and must remain within the optimal vegetative growth band during fault-free operation:
$$\phi_{\text{temp\_critical}} = \square_{[0, T]} \left( 7.0^\circ\text{C} \le T_{\text{in}}(t) \le 32.0^\circ\text{C} \right)$$
$$\phi_{\text{temp\_nominal}} = \square_{[0, T]} \left( \neg \text{fault}(t) \implies (18.0^\circ\text{C} \le T_{\text{in}}(t) \le 25.0^\circ\text{C}) \right)$$

### Specification 2: Atmosphere Composition & Pressure Envelope ($\phi_{\text{atmo}}$)
$$\phi_{\text{atmo}} = \square_{[0, T]} \left( (1000\text{ ppm} \le \text{CO}_2(t) \le 1500\text{ ppm}) \land (0.195 \le \text{O}_2(t) \le 0.235) \land (65\text{ kPa} \le P_{\text{total}}(t) \le 105\text{ kPa}) \right)$$

### Specification 3: Drought Avoidance & Root-Zone Buffer ($\phi_{\text{water}}$)
The shelf water reservoir must never deplete below the minimum root wetting buffer ($2.0\text{ L}$), avoiding irreversible crop mortality:
$$\phi_{\text{water}} = \square_{[0, T]} \left( \text{reservoir\_level}(t) \ge 2.0\text{ L} \right)$$

### Specification 4: Energy Reserve Safety Invariant ($\phi_{\text{energy}}$)
Battery storage must never deplete below the critical life-support threshold ($E_{\text{reserve}} = 10,000\text{ Wh}$):
$$\phi_{\text{energy}} = \square_{[0, T]} \left( E_{\text{battery}}(t) \ge E_{\text{reserve}} \right)$$

### Specification 5: Bounded-Time Actuator Fault Recovery ($\phi_{\text{recovery}}$)
If an actuator failure occurs (e.g. primary heater burnout or degradation), auxiliary heating must engage and stabilize cabin temperature within a recovery deadline $\tau_{\text{rec}} = 2\text{ hours}$:
$$\phi_{\text{recovery}} = \square_{[0, T]} \left( \text{heater\_fault}(t) \implies \lozenge_{[0, 2\text{h}]} \square_{[0, 6\text{h}]} (T_{\text{in}}(t) \ge 15.0^\circ\text{C}) \right)$$

---

## 3. Control Barrier Functions (CBFs) for Forward Invariance

To guarantee that $T_{\text{in}}(t) \ge T_{\min} = 7.0^\circ\text{C}$ under worst-case Martian night temperatures ($-120.15^\circ\text{C}$), we construct a **Control Barrier Function (CBF)**.

### Continuous Energy Balance Dynamics:
$$C_{\text{th}} \frac{dT_{\text{in}}}{dt} = \eta_{\text{lamp}} P_{\text{lamp}} + \eta_{\text{heat}} P_{\text{heater}} - UA(T_{\text{in}} - T_{\text{ext}}(t))$$
- Thermal capacitance: $C_{\text{th}} = 1,771,225\text{ J/K}$
- Hull heat transfer coefficient: $UA = 14.0\text{ W/K}$
- Conversion efficiencies: $\eta_{\text{lamp}} = 0.90$, $\eta_{\text{heat}} = 1.00$
- Exterior boundary: $T_{\text{ext}}(t) = -50.15 + 70.0 \cos\left(\frac{2\pi}{24.65}(t - 14.0)\right)$

### Barrier Function Construction:
Define the safe set $\mathcal{C} = \{ T \in \mathbb{R} \mid h(T) \ge 0 \}$ with barrier:
$$h(T) = T - T_{\min}$$

By CBF theory, $\mathcal{C}$ is forward-invariant if:
$$\dot{h}(T, u) + \alpha h(T) \ge 0 \quad (\text{for class }\mathcal{K}\text{ gain } \alpha > 0)$$

Substituting the thermal dynamics:
$$\frac{1}{C_{\text{th}}} \left( 0.90 P_{\text{lamp}} + P_{\text{heater}} - UA(T - T_{\text{ext}}(t)) \right) + \alpha (T - T_{\min}) \ge 0$$

### Exact Closed-Form Minimum Energy Constraint:
$$0.90 P_{\text{lamp}} + P_{\text{heater}} \ge UA(T - T_{\text{ext}}(t)) - \alpha C_{\text{th}} (T - T_{\min})$$

> **Formal Guarantee:** Even if primary heating burns out completely ($P_{\text{heater}} = 0$), the safety shield dynamically computes the required emergency lamp power:
> $$P_{\text{lamp}}^{\text{safe}}(t) = \frac{1}{0.90} \max\left(0, \, UA(T - T_{\text{ext}}(t)) - \alpha C_{\text{th}} (T - T_{\min})\right)$$
> Applying $P_{\text{lamp}}^{\text{safe}}$ guarantees mathematically that the cabin temperature trajectory will never enter the unsafe region $T < T_{\min}$.

---

## 4. Simplex Architecture & Runtime Assurance (RTA)

To combine performance optimization with safety assurance, the system employs the **Simplex Architecture**:

1. **Uncertified Performance Controller:**
   - Regulates nominal conditions, minimizes energy consumption, and optimizes crop harvest.
   - Outputs proposed command vector: $u_{\text{perf}} = (P_{\text{heat}}^{\text{perf}}, P_{\text{lamp}}^{\text{perf}}, V_{\text{irr}}^{\text{perf}}, n_{\text{co2}}^{\text{perf}})$.
2. **Safety Monitor:**
   - Evaluates whether $u_{\text{perf}}$ satisfies the CBF invariant over a forward horizon $\Delta t_H$.
   - Computes real-time spatial robustness $\rho(\phi, t)$.
3. **Verified Fail-Safe Shield:**
   - Engages when the safety monitor flags an imminent barrier violation or when actuator burnout is confirmed.
   - Intercepts commands and enforces $P_{\text{lamp}} = P_{\text{lamp}}^{\text{safe}}$ and battery reserve protections.

---

## 5. Quantitative Semantics & Robustness Monitoring

STL provides continuous quantitative semantics measuring the **margin of safety**:
$$\rho(\mu, s(t)) = s(t) - c \quad \text{for predicate } \mu \equiv (s(t) \ge c)$$

### Monitored Robustness Degree Signals:
- **Thermal Safety Margin:** $\rho_{\text{thermal}}(t) = \min\left( T(t) - 7.0, \, 32.0 - T(t) \right)$
  - $\rho_{\text{thermal}} > 0$: Safe operating margin (e.g. $+15.0^\circ\text{C}$ buffer).
  - $\rho_{\text{thermal}} \le 0$: Hazard violation.
- **Moisture Safety Margin:** $\rho_{\text{water}}(t) = \text{reservoir\_level}(t) - 2.0\text{ L}$
- **Energy Safety Margin:** $\rho_{\text{energy}}(t) = E_{\text{battery}}(t) - E_{\text{reserve}}$

These continuous robustness signals are published to the NASA OpenMCT telemetry stream, enabling mission operators to visualize degrading safety margins before discrete alarms fire.
