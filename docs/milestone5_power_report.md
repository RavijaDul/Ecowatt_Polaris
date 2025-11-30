**Power Report**
- **Purpose:** Collect before/after server-side power instrumentation to measure the effect of changes (Wi‑Fi gating, light-sleep etc.).
- **Script:** `scripts/power_report.py` — snapshots data from the server and compares two snapshots.
- **Server endpoints used:** `GET /api/power/<device>` — returns recent power records in JSON. Ensure the server exposes this API and that device uploads include `t_sleep_ms`, `t_uplink_ms`, `idle_budget_ms`, and `uplink_bytes` fields in the payload.
- **Typical workflow:**
- 1. Run `python scripts/power_report.py snapshot --url http://localhost:5000 --device <device-id> --out before.json` to capture the baseline.
- 2. Deploy firmware changes (enable light-sleep, Wi‑Fi gating) to device(s).
- 3. Run the same command to capture `after.json` after the device has run for the same observation period.
- 4. Run `python scripts/power_report.py compare --before before.json --after after.json` to get averages and deltas.
- **Notes:**
- Capture snapshots for comparable durations and traffic patterns (same sampling interval, same remote server). Prefer at least several minutes of records (10–30 samples) for reliable averages when sampling at 5s.
- If your server API path differs, update the script or use an HTTP proxy to map paths.

**Recommended test plan (Baseline vs Optimized)**

1) Overview
- Baseline build: firmware built with *power optimizations disabled* (no manual light-sleep, no Wi‑Fi gating, default PM settings). Use sampling interval 5s for high-frequency baseline if you want to measure worst-case.
- Optimized build: firmware built with *power optimizations enabled* (enable automatic light-sleep, optionally enable Wi‑Fi gating for long upload intervals). Use the same sampling interval or a larger one (e.g., 900s) for more aggressive savings testing.

2) Device identifiers
- Use distinct device IDs for clarity (compile-time `CONFIG_ECOWATT_DEVICE_ID`) such as `EcoWatt-Dev-01-baseline` and `EcoWatt-Dev-01-opt` so server snapshots don't mix records.

3) Test durations
- Run each configuration for the same fixed wall-clock time. For 5s sampling, collect at least 10–30 samples (1–3 minutes), but prefer 5–10 minutes to smooth variance. For 900s upload interval tests, run at least a few upload cycles (e.g., 2–3 hours) or reduce to a shorter interval (e.g., 60–300s) for practical experiments.

4) Steps (short commands)
- Start server:
```powershell
cd server
python -m pip install -r requirements.txt
$env:LOG_DIR = "$PWD\logs"
python app.py
```
- Baseline run:
	- Build firmware with `CONFIG_ECOWATT_DEVICE_ID="EcoWatt-Dev-01-baseline"` and ensure `ECOWATT_MANUAL_LIGHT_SLEEP`/`ECOWATT_WIFI_GATE_BETWEEN_UPLOADS` are disabled.
	- Flash device and let it run for the chosen observation period.
	- Capture snapshot:
		```bash
		python scripts/power_report.py snapshot --url http://localhost:5000 --device EcoWatt-Dev-01-baseline --out before.json
		```
- Optimized run:
	- Build firmware with `CONFIG_ECOWATT_DEVICE_ID="EcoWatt-Dev-01-opt"` and enable `ECOWATT_ENABLE_AUTO_LIGHT_SLEEP=Y`; optionally enable `ECOWATT_WIFI_GATE_BETWEEN_UPLOADS=Y` if upload interval large enough.
	- Flash device and let it run for the same observation period.
	- Capture snapshot:
		```bash
		python scripts/power_report.py snapshot --url http://localhost:5000 --device EcoWatt-Dev-01-opt --out after.json
		```

5) Generate comparison report (plots + markdown):
```bash
python scripts/generate_comparison_report.py --before before.json --after after.json --out report_baseline_vs_opt
```

6) Interpreting results
- The report includes:
	- averages for idle/sleep/uplink times and uplink bytes
	- an approximate energy estimate in joules (uses environment vars; see below)
	- combined plot image `combined.png` and `report.md` with numeric deltas.

Energy estimation note (approximate)
- Because you don't have a hardware current meter, the scripts estimate energy from timing counters using configurable assumptions:
	- `POWER_V_SUPPLY_MV` (default 5000 mV)
	- `POWER_I_ACTIVE_MA` (default 200 mA)
	- `POWER_I_UPLINK_MA` (default 300 mA)
	- `POWER_I_SLEEP_MA` (default 5 mA)
- Formula used: E (J) = V (V) * I (A) * t (s). The scripts estimate total seconds spent in sleep, uplink, and 'idle' budgets and compute per-state energies then sum them. Keep these assumptions constant between runs so the relative delta is meaningful.

If you want more accurate results, repeat tests while measuring supply current with a DMM/oscilloscope/current probe.

Notes and practical advice
- For 5 s sampling: keep Wi‑Fi associated (no gating) and rely on automatic light-sleep where supported. Wi‑Fi gating is rarely beneficial at 5 s due to re-association and TLS handshake costs.
- For long intervals (≥30 s or 900 s): Wi‑Fi gating plus manual light-sleep can produce larger savings because wake/handshake overhead is amortized over a long idle.

If you want I can:
- Add a small helper to run both experiments semi-automatically and schedule snapshots, or
- Add server-side download links for produced snapshot artifacts so you can fetch them from the web UI.
 
**How to build the two instances (exact commands)**

I added two ready-to-use SDK config files under `configs/`:
- `configs/baseline.sdkconfig` — baseline (power optimizations off)
- `configs/optimized.sdkconfig` — optimized (auto light-sleep + Wi‑Fi gating, upload interval 900s)

To build one instance, copy the desired config to the project root `sdkconfig` and run the normal IDF build commands.

Example (PowerShell):
```powershell
cd D:\sem 07\Embedded\Ecowatt_Polaris
copy configs\baseline.sdkconfig sdkconfig    # OR copy configs\optimized.sdkconfig sdkconfig
set IDF_PATH=C:\Users\Ravija\.espressif\esp-idf\v5.4.2\esp-idf
idf.py build
idf.py -p COMX flash
```

Replace `COMX` with your serial port. If you change the `sdkconfig` file, re-run `idf.py build` before flashing.

If you prefer not to overwrite `sdkconfig`, save a copy and swap files between builds.
