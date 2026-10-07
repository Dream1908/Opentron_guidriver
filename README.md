# PUDA GUI Driver

A PUDA edge service that integrates **any desktop instrument software** into PUDA without a hardware SDK. Instead of a vendor SDK, it uses:

| Layer | Technology | Role |
|---|---|---|
| GUI automation | [Hermes computer use](https://hermes-agent.nousresearch.com/docs/user-guide/features/computer-use) → `cua-driver` MCP | Background click / type / key dispatch, no focus steal |
| Vision reading | Claude vision via Hermes model config | Read instrument state from screenshots |
| PUDA integration | `puda` `EdgeRunner` + NATS | Expose commands and telemetry to the PUDA platform |

---

## Repo Structure

```
PUDA_guidriver/
├── pyproject.toml   # dependencies (puda, mcp, anthropic, pydantic-settings)
├── main.py          # Config → GuiDriver → EdgeNatsClient → EdgeRunner
├── driver.py        # CuaDriverClient (MCP) + GuiDriver (@command, @tlm_stream)
├── .env.example     # environment variable template
├── Dockerfile       # container build (optional)
└── compose.yml      # docker compose (optional)
```

---

## How It Works

```
PUDA CLI / Protocol
       │  NATS
       ▼
  EdgeRunner  ──────────────────────────►  GuiDriver (@command methods)
                                                  │
                           ┌───────────────────────┤
                           │                       │
                     cua-driver MCP          Claude vision
                    (GUI automation)     (via Hermes model config)
                           │                       │
                     ┌─────▼──────┐      ┌─────────▼────────┐
                     │ Instrument │ PNG  │  Extract status / │
                     │ Software   │─────►│  plan GUI actions │
                     │ (any app)  │      └──────────────────┘
                     └────────────┘
```

1. A PUDA command arrives via NATS (e.g. `status`, `find_and_click`, `run_gui_step`).
2. `GuiDriver` calls `cua-driver` (via MCP) to **capture a screenshot** of the instrument window — in the background, no focus change.
3. The screenshot is sent to **Claude vision** to extract the instrument state or decide which UI element to interact with. The LLM model and API credentials come from your existing Hermes configuration — no separate setup required.
4. `GuiDriver` dispatches the synthesised mouse/keyboard event back through `cua-driver`.
5. The result is returned to PUDA.

---

## Prerequisites

### 1. Install Hermes Agent with computer use

Follow the [Hermes installation guide](https://hermes-agent.nousresearch.com/docs/getting-started/installation), then enable computer use and verify the setup:

```powershell
hermes computer-use install
hermes computer-use doctor      # verify — all checks should be green
```

`cua-driver` must be on your `PATH`. The doctor output confirms it. The LLM model and API key used for vision analysis are read from your Hermes configuration — no additional credentials are needed in this driver.

**Windows note:** Grant no special permissions at install time, but if you drive over SSH (not RDP/console), follow the [Windows SSH autostart guide](https://cua.ai/docs/how-to-guides/driver/windows-ssh).

### 2. Python environment (uv)

```powershell
uv sync
```

---

## Setup

```powershell
Copy-Item .env.example .env
```

Edit `.env` — only three values are needed:

| Variable | Description |
|---|---|
| `MACHINE_ID` | Unique PUDA machine ID, e.g. `hplc-1` |
| `NATS_SERVERS` | NATS cluster URLs |
| `TARGET_APP` | **Exact window title** of the instrument software |
| `CAPTURE_INTERVAL` | Seconds between telemetry status captures (default `30.0`) |

### Finding the exact TARGET_APP name

`TARGET_APP` must match the **window title** as cua-driver sees it. On Windows:

```powershell
# List all visible window titles
Get-Process | Where-Object {$_.MainWindowTitle} | Select-Object MainWindowTitle
```

Use the title (or a unique prefix) as `TARGET_APP`.

---

## Run

```powershell
# Baremetal (uv)
uv run python main.py

# Or with Docker
docker compose -f compose.yml up -d --build
docker compose -f compose.yml logs -f
```

---

## PUDA Commands

| Command | Description |
|---|---|
| `status` | Capture screenshot → LLM reads all visible values → JSON status |
| `capture_screenshot` | Return raw base64 PNG of the target app |
| `find_and_click` | Describe a UI element; LLM locates + clicks it |
| `type_text` | Type text, optionally clicking a field first |
| `press_key` | Send a keyboard shortcut (e.g. `ctrl+s`, `f5`) |
| `run_gui_step` | Execute one natural-language GUI instruction autonomously |
| `ask_screen` | Ask the LLM a read-only question about the current screen |
| `home` | Navigate to the app's main/home screen |
| `reset` | Send Escape to cancel any in-progress operation |
| `shutdown` | Close the cua-driver MCP session cleanly |

### Telemetry stream

`puda.<MACHINE_ID>.tlm.stream.instrument_status` — published every `CAPTURE_INTERVAL` seconds. The LLM reads the current screenshot and returns all visible instrument values as JSON.

```
puda machine watch 'puda.<MACHINE_ID>.tlm.stream.>'
```

### Example CLI usage

```bash
# Get current instrument status
puda machine run my-instrument status

# Click a named button
puda machine run my-instrument find_and_click --description "Start Run button"

# Set a value in a specific field
puda machine run my-instrument type_text --text "37.5" --field_description "temperature setpoint"

# Send a keyboard shortcut
puda machine run my-instrument press_key --keys "ctrl+s"

# Ad-hoc natural-language instruction
puda machine run my-instrument run_gui_step --instruction "Open the File menu and click Export"

# Read-only question about the screen
puda machine run my-instrument ask_screen --question "Is there an active alarm?"
```

---

## Customising for a Specific Instrument

### 1. Add instrument-specific commands

Subclass `GuiDriver` in `driver.py` and add `@command` methods:

```python
class HplcGuiDriver(GuiDriver):
    @command
    def start_run(self, method_name: str) -> dict:
        """Load a method and start an HPLC run."""
        self.find_and_click("Method dropdown")
        self.type_text(method_name, "method name field")
        self.find_and_click("Start Run button")
        return self.status()
```

### 2. Multi-step automation (PUDA protocols)

Chain commands in a PUDA protocol YAML:

```yaml
steps:
  - machine: hplc-1
    command: find_and_click
    params: {description: "New Method button"}
  - machine: hplc-1
    command: type_text
    params: {text: "1.0", field_description: "flow rate mL/min"}
  - machine: hplc-1
    command: press_key
    params: {keys: "return"}
  - machine: hplc-1
    command: status
```

---

## Troubleshooting

**`cua-driver not found`** — run `hermes computer-use install`, confirm `cua-driver --version` works in a new terminal.

**No image returned from capture** — the app window may be minimised or off-screen. Ensure it is visible. Run `hermes computer-use doctor` to check Screen Recording permission.

**LLM cannot identify element** — the SOM screenshot may have too many elements. Use descriptive text that matches visible labels in the UI (button text, field labels). Use `capture_screenshot` + manual inspection to see what the LLM sees.

**Windows SSH / Session 0** — drive from an RDP or console session directly, or enable the cua-driver autostart scheduled task. See the [Windows SSH guide](https://cua.ai/docs/how-to-guides/driver/windows-ssh).
