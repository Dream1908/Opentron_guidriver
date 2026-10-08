# PUDA GUI Driver — Opentrons OT-2

A PUDA edge service that integrates the **Opentrons OT-2** liquid-handling robot into PUDA by controlling the **Opentrons desktop App** via Hermes computer use — no HTTP API, no SSH, no vendor SDK required.

| Layer | Technology | Role |
|---|---|---|
| GUI automation | `cua-driver` MCP | Capture the App and dispatch accessibility or keyboard actions |
| PUDA integration | PUDA SDK `0.0.16` + NATS | Expose public methods through the pre-0.1 edge contract |

---

## How It Works

This project follows the pre-0.1 structure from `PUDAP/edge-python-template` before commit `f75c5c4` introduced decorators:

1. `EdgeRunner` discovers documented public driver methods as commands.
2. Internal helpers, startup, cached state, and telemetry methods use a leading underscore.
3. `main.py` supplies `telemetry_handler` and `state_handler` explicitly.
4. Screens are returned as raw base64 PNG data. No LLM analyzes them.
5. There are no decorator-based command, safety, state, or telemetry registrations.

---

## Opentrons App Navigation Reference

```
Left sidebar
  ├── Protocols   ← home screen; lists all imported protocols
  ├── Devices     ← shows connected OT-2 robots
  └── Settings

Protocols tab
  ├── Import (top-right)      → opens import sidebar → Choose File
  └── Protocol card ⋮ menu   → Start setup

Setup screen
  ├── Select robot            → Proceed to setup
  ├── Robot Calibration
  ├── Labware Position Check  (optional)
  └── Start run ──────────────────────────────────────────────┐

Run tab                                                        │◄──
  ├── Run Preview (live step log)
  ├── Pause / Resume
  └── Cancel run
```

---

## Prerequisites

### 1. Install the Opentrons App

Download from [opentrons.com](https://opentrons.com/ot-2/) and open it. The OT-2 must be connected and visible in the Devices tab.

### 2. Install Hermes Agent with computer use

```powershell
hermes computer-use install
hermes computer-use doctor      # verify — all checks should be green
```

The LLM model and API credentials come from your existing Hermes configuration — no additional setup needed here.

### 3. Python environment (uv)

```powershell
uv sync
```

---

## Setup

```powershell
Copy-Item .env.example .env
```

Edit `.env`:

| Variable | Description |
|---|---|
| `MACHINE_ID` | Unique PUDA machine ID, e.g. `ot2-1` |
| `NATS_SERVERS` | NATS cluster URLs |
| `TARGET_APP` | Window title of the Opentrons App (default `Opentrons`) |
| `ROBOT_NAME` | Display name of the OT-2 in the App's robot list (leave empty to auto-select) |

### Finding your robot name

Open the Opentrons App → **Devices** tab. The robot display name shown there is what goes into `ROBOT_NAME`.

---

## Run

```powershell
# Baremetal (uv)
uv run python main.py

# Or with Docker
docker compose -f compose.yml up -d --build
docker compose -f compose.yml logs -f
```

The Opentrons App must be **open and visible** on screen before starting the driver.

---

## PUDA Commands

### Status & monitoring

| Command | Description |
|---|---|
| `status` | Return the current raw screenshot as base64 PNG |
| `get_run_progress` | Return the current Run-tab screenshot without interpretation |
| `capture_screenshot` | Return a raw base64 PNG of the Opentrons App |
| `ask_screen` | Return a raw screenshot together with the supplied question |

### Navigation

| Command | Description |
|---|---|
| `navigate_protocols` | Click Protocols in the left sidebar |
| `navigate_devices` | Click Devices in the left sidebar |
| `home` | Go to the Protocols tab (App home screen) |

### Protocol management

| Command | Description |
|---|---|
| `get_protocol_list` | Return the Protocols-tab screenshot and accessibility elements |
| `import_protocol` | Import a `.py` or `.json` protocol file into the App |
| `start_setup` | Open setup for a named protocol |

### Run lifecycle

| Command | Args | Description |
|---|---|---|
| `select_robot` | `robot_name` | Choose the OT-2 on the setup screen |
| `start_run` | — | Click "Start run" to begin the protocol |
| `pause_run` | — | Pause an active run |
| `resume_run` | — | Resume a paused run |
| `cancel_run` | — | Stop the run (with confirmation) |

### Generic

| Command | Description |
|---|---|
| `find_and_click` | Click any UI element by plain-English description |
| `press_key` | Send a keyboard shortcut (e.g. `escape`, `ctrl+s`) |
| `reset` | Send Escape to dismiss any dialog |
| `shutdown` | Close the cua-driver MCP session |

### Telemetry

The pre-0.1 telemetry handler explicitly publishes heartbeat and host-health data. Screenshot status is fetched on demand with `status`, `capture_screenshot`, or `get_run_progress`.

---

## Example CLI Usage

```bash
# Check current state
puda machine run ot2-1 status

# List imported protocols
puda machine run ot2-1 get_protocol_list

# Import a new protocol
puda machine run ot2-1 import_protocol --file_path "C:\protocols\serial_dilution.py"

# Full run sequence
puda machine run ot2-1 start_setup --protocol_name "Serial Dilution Tutorial"
puda machine run ot2-1 select_robot
puda machine run ot2-1 start_run

# Monitor progress
puda machine run ot2-1 get_run_progress

# Pause and resume
puda machine run ot2-1 pause_run
puda machine run ot2-1 resume_run

# Stop
puda machine run ot2-1 cancel_run
```

---

## Example PUDA Protocol

```yaml
steps:
  - machine: ot2-1
    command: navigate_protocols
  - machine: ot2-1
    command: start_setup
    params: {protocol_name: "Serial Dilution Tutorial"}
  - machine: ot2-1
    command: select_robot
  - machine: ot2-1
    command: start_run
  - machine: ot2-1
    command: get_run_progress
```

---

## Troubleshooting

**`cua-driver not found`** — run `hermes computer-use install`, confirm `cua-driver --version` works.

**No image returned from capture** — the Opentrons App may be minimised. Ensure it is visible on screen. Run `hermes computer-use doctor` to verify Screen Recording permission.

**LLM cannot identify button** — element descriptions are matched against visible text in the App. Use the exact button label shown in the UI (e.g. "Start run", "Cancel run", "Proceed to setup"). Use `capture_screenshot` + `ask_screen` to inspect the current screen.

**Robot not found during select_robot** — set `ROBOT_NAME` in `.env` to the exact display name shown in the Opentrons App Devices tab, or leave it empty to auto-select the first robot.

**Windows SSH / Session 0** — drive from an RDP or console session, or enable the cua-driver autostart task. See the [Windows SSH guide](https://cua.ai/docs/how-to-guides/driver/windows-ssh).
