# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Python automation + PyQt6 GUI framework for DC generator characterization. The core sink is a GW Instek **PEL-5000C** high-power electronic load, but the system now integrates **four instruments**, each with its own hand-rolled driver package (no VISA/pyvisa anywhere):

| Package | Instrument | Transport | Role |
|---|---|---|---|
| `pel5000c/` | GW Instek PEL-5000C electronic load | LAN/TCP **:4001**, SCPI | The load sink (CC/CR/CV/CP), reads V/I/P |
| `keyence/` | Keyence KV-N40 PLC (Host-Link 上位鏈路) | LAN/TCP **:8500**, ASCII | Servo motor on/off + central monitor DM block (temperature, rpm, mode, alarm) |
| `gpm8310/` | GW Instek GPM-8310 power meter | LAN/TCP **:23**, SCPI | Reads **power factor (λ / `LAMBda`)** — a client spec requirement |
| `fotek/` | FOTEK NT-22-RS temp controller | RS-485 Modbus RTU | ⚠ **deprecated** — temperature now comes via the PLC; driver kept for reference |

`ui/main_window.py` (PyQt6) is the operator-facing app that ties PEL + PLC + GPM together with one background polling thread per instrument. All operator-facing text, log messages, and docs are in **Traditional Chinese**; keep new user-visible strings in the same language.

This drives **real hardware that pulls kilowatts**. See "Safety" below before changing anything in `pel5000c/driver.py`, the UI threads, or test scripts that toggle `LOAD ON`.

## Commands

```powershell
# Install (project is not a package — no setup.py / pyproject)
python -m pip install -r requirements.txt

# Launch the operator GUI (PyQt6 — the main app)
python ui\main_window.py

# Run a test script (these ARE the CLI entry points — there is no pytest suite)
python tests\01_connection_test.py     # PEL: safest, no LOAD ON, just *IDN?
python tests\02_manual_control.py      # PEL: interactive REPL: cc/cr/cv/cp/on/off/m/raw
python tests\03_vi_curve.py            # PEL: CC sweep → CSV + PNG
python tests\04_efficiency_test.py     # PEL: η = P_out / P_in (prompts for P_in)
python tests\05_load_sweep.py          # PEL: auto-sweep CC/CR/CV/CP
python tests\06_plc_connection_test.py # Keyence PLC: connect + read monitor DM block
python tests\07_gpm_connection_test.py # GPM-8310: connect + read power factor (PF)
```

Configuration is centralized in `config.yaml` (per-instrument IP/port, DUT ratings, sweep ranges, output dirs). To change behavior, edit YAML — don't hardcode in scripts. Site-specific IPs in the checked-in config differ from factory defaults: PEL-5000C `192.168.16.128` (factory `192.168.0.100`), PLC `192.168.0.10`, GPM-8310 `192.168.0.100` (socket port factory default **23**). Each non-PEL instrument has an `enabled:` flag — the UI auto-connects on startup only when it is `true`.

There is no linter, formatter, or test runner configured. "Testing" means running a script (or the UI) against the instrument, or against a TCP mock — see how the GPM driver was validated by pointing it at a localhost socket server.

## Architecture

Layers, loosely coupled through `config.yaml`:

1. **Driver packages** — one per instrument, all the same shape: a class that is a context manager wrapping a socket (or serial), domain-specific exceptions with a common base, and thin methods that each wrap one protocol command. **When you need a command not yet wrapped, add a method to the driver — don't send raw strings from callers.** All three network drivers share the SCPI/ASCII framing idiom (TX terminator, RX terminator, ASCII, `command_delay` after each write).
   - **`pel5000c/`** — `PEL5000C`. TX `\n`, RX `\r\n`. `connect()` auto-sends `REMOTE`; `disconnect()`/`__exit__` auto-sends `LOAD OFF` + `LOCAL` — **do not bypass this**, it's the last-line safety net. `measure_vip()` prefers the native `MEAS:VC?` (single query returning V,I) over two queries to minimize V/I time skew; follow that pattern. Exceptions: `PELConnectionError/CommandError/TimeoutError/SafetyError`, base `PELError`.
   - **`keyence/`** — `KeyenceKV` Host-Link (上位鏈路) ASCII driver. TX `\r`, RX `\r\n`. Methods `read_word(s)`, `write_word(s)`, `set_relay`/`reset_relay` (ST/RS). Reads a contiguous DM "central monitor" block in one `RDS`. Base `KVError`.
   - **`gpm8310/`** — `GPM8310` power meter. TX `\n`, RX `\r\n`. `connect()` sends `:COMMunicate:REMote ON` + `:HEADer OFF` + `:NUMeric:FORMat ASCii`; `disconnect()` sends `:COMMunicate:REMote OFF` (it does **not** touch `LOCKout`, so the front-panel Local key keeps working). Power factor has no `MEAS:PF?` shortcut — `configure_power_factor()` sets `:NUMeric:NORMal:ITEM1 LAMBda`, then `read_power_factor()` queries `:NUMeric:NORMal:VALue? 1` and converts the meter's `NAN`/`INF` sentinels into `GPMCommandError`. Base `GPMError`.
   - **`fotek/`** — `FotekNT22` Modbus-RTU temp reader. ⚠ deprecated (temperature now via PLC); leave as reference.

2. **`ui/main_window.py`** (PyQt6) — the operator app. One `QThread` poller per instrument: `PollingThread` (PEL V/I/P at 5 Hz + LOAD/MODE state), `PLCPollingThread` (DM monitor block), `GPMPollingThread` (power factor). **Each instrument's socket is serialized by its own `threading.Lock`** (`io_lock` / `plc_lock` / `gpm_lock`) because the poller and user-button handlers can both touch it. `AutoTestThread` drives the PEL through the 5%/50%/100% staged-load sequence, calling `assert_within` after every sample, and pulls temperature/rpm/PF from cached values (`_last_temp` / `_last_rpm` / `_last_pf`) rather than contending for the PLC/GPM sockets. PLC `DM7002` (auto mode) locks out all manual buttons. Mirror the existing per-instrument connect/disconnect/`_stop_*_monitor`/poller-failed quartet when wiring a new instrument.

3. **`utils/`** — cross-cutting helpers. `load_config()` resolves `config.yaml` relative to the project root (not CWD). `setup_logger()` wires `pel5000c.*` loggers into the script's handlers so driver `log.debug(">>> cmd")` lines appear in run logs. `CSVLogger` writes UTF-8 **with BOM** (`utf-8-sig`) so Excel renders Chinese headers correctly. `plotter.py` forces matplotlib `Agg` and a Chinese-capable font stack (Microsoft JhengHei → YaHei → SimHei → DejaVu Sans); don't switch backends or remove the font config.

4. **`tests/NN_*.py`** — numbered top-to-bottom by risk/complexity, one standalone `python tests\NN_*.py` entry point each, with a `main()` returning an int exit code. They all `sys.path.insert(0, parent)` to import the driver packages without installation, then: load YAML → set up logger → open the driver as a context manager → (for sweeps) write CSV under `data/` and PNGs under `reports/` with a `timestamp_tag()` suffix. Mirror this when adding new tests.

## Safety (load-bearing — read before editing test scripts or driver)

- The DUT can push tens of amps through the load. The `LOAD OFF`/`LOCAL` cleanup in `PEL5000C.disconnect()` is a hardware safety mechanism, not a courtesy — never remove it, and never add code paths that exit the context manager without it running.
- `PEL5000C.assert_within(v, i, v_max, i_max)` calls `load_off()` **before** raising `PELSafetyError`. Any new safety check must drop the load first, then raise. Don't invert this order.
- `config.yaml` has `dut.voltage_max` / `dut.current_max` separate from `dut.rated_*` — the `*_max` values are the trip thresholds passed to `assert_within`. When adding new sweeps, plumb these through and call `assert_within` after every measurement (see `03_vi_curve.py:89` and `05_load_sweep.py:42` for the pattern).
- `command_delay` (default 50ms) after every `write()` is intentional — the instrument can drop commands if pushed faster. Don't reduce it without testing on the real unit.
- README explicitly warns: when first running a sweep against new hardware, lower `vi_curve.current_stop` in `config.yaml` to a small value (e.g. 5A) before increasing.

## Conventions to follow

- **Driver methods are thin wrappers around the protocol.** When you need a command not yet wrapped, add a method to the relevant driver (`PEL5000C` / `KeyenceKV` / `GPM8310`) rather than sending raw strings from test scripts or the UI — keeps each instrument's command vocabulary in one file. See `PEL5000C.set_mode_cc/cr/cv/cp` and `GPM8310.configure_power_factor` for the pattern.
- **Adding a new instrument = a new driver package + UI quartet.** Mirror an existing package (class as context manager, `XxxError` base + connection/command/timeout subclasses, `__init__.py` re-exporting) and add its `enabled:`/`ip:`/`port:` block to `config.yaml`. In the UI, add a poller `QThread`, a per-instrument `Lock`, a connection box, and the connect/disconnect/`_stop_*_monitor`/poller-failed methods.
- **Output files are timestamped, never overwritten.** Use `utils.logger.timestamp_tag()` and write into `data/` (CSV + logs) and `reports/` (plots). Don't introduce fixed filenames.
- **CSV encoding is `utf-8-sig`** (read from `output.csv_encoding`). Don't switch to plain `utf-8` — Excel will mojibake the Chinese headers.
- **No `requirements-dev.txt` / test framework.** If you need to add automated tests, propose the layout to the user first rather than dropping in pytest unprompted.
