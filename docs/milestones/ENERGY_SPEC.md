# Energy tracking — specification

Milestone 1 ships an **estimated** energy figure and **specifies** (does not yet
build) measured energy for locally-hosted models. This file is the spec for both.

## 1. What ships now: per-token estimate

Optional, per-model, opt-in. A model's TOML may declare an `[energy]` table:

```toml
[energy]
per_input_token = 0.0002    # watt-hours per input token
per_output_token = 0.0006   # watt-hours per output token
# or a single blended figure (the split pair wins when both are present):
per_token = 0.0004          # watt-hours per token (input+output)
source = "estimate"         # provenance tag; for local models name the backend
```

`harness/cost.py:estimate_energy_wh()` multiplies the call's token counts by
these rates. It runs for **any** provider (including `pricing = "free"` local
ones), so a self-hosted model with no dollar cost still reports energy. Energy is
accumulated per turn and per session and shown on the `[harness] usage:` line in
watt-hours; if `DEEPAGENTS_ELECTRICITY_RATE` (USD/kWh) is set, an electricity
cost is derived (`Wh / 1000 * rate`).

The per-token numbers currently committed are **rough placeholders** — see
`follow-up.md`. Treat them as order-of-magnitude until measured or vendor-sourced.

## 2. What is specified, not built: measured local-device energy

For locally-hosted models (ollama / lmstudio) the real energy used can be
**measured** instead of estimated, because the model runs on hardware we control.
The intended design — `harness/cost.py:measure_local_energy_wh()` raises
`NotImplementedError` until it lands:

1. **Window.** The `CostTrackerMiddleware` already brackets each model call
   (`before_model` / `after_model`). Record a monotonic timestamp at
   `before_model` and the elapsed window at `after_model`; that interval is when
   the local LLM is thinking / producing output.
2. **Sample device power across the window** using a backend chosen by the
   model's `[energy] source`:

   | `source`       | Platform | Mechanism |
   |----------------|----------|-----------|
   | `nvidia_smi`   | NVIDIA GPU | poll `nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits` (W) at a fixed interval; trapezoid-integrate over the window |
   | `rapl`         | Linux/Intel CPU | read the energy counter `/sys/class/powercap/intel-rapl:0/energy_uj` (µJ) at window start/end; delta ÷ 3.6e9 = Wh (handle counter wraparound) |
   | `powermetrics` | macOS (Apple Silicon) | `sudo powermetrics --samplers cpu_power,gpu_power -i <ms>`; sum CPU+GPU power samples across the window |
   | `ipmi` / `redfish` | bare-metal server | BMC whole-node power reading via `ipmitool dcmi power reading` or Redfish `/Power`; coarse but vendor-neutral |

3. **Integrate** sampled power (W) over the window (s) to watt-hours
   (`Wh = ∫ P dt / 3600`); for counter-based backends (RAPL) take the delta
   directly. Attribute the measured energy to that call and feed it to the
   accumulator in place of the per-token estimate.

### Open issues for the measured path (deferred)

- **Attribution.** Sampling measures the *whole device*, not just the LLM
  process; idle/baseline draw and co-tenant load are included. Subtracting a
  measured idle baseline is the minimum; per-process GPU accounting (e.g. NVML
  per-PID) is better but not universally available.
- **Sampling cost / rate.** Polling `nvidia-smi` is not free and has latency;
  pick an interval (e.g. 100–250 ms) that balances accuracy vs. overhead.
- **Permissions.** `powermetrics` needs root; RAPL sysfs may be root-only;
  inside a container these counters are often not exposed without explicit host
  passthrough. The harness container would need the device/sysfs mounted.
- **Remote local servers.** If ollama/lmstudio runs on a *different* host than
  the harness, power must be read on that host (agent/exporter), not in-container.

Until built, local models fall back to the per-token estimate (§1) when an
`[energy]` table is present, and report no energy otherwise.
