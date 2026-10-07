"""PUDA GUI driver for Opentrons OT-2 — controls the Opentrons desktop App via Hermes computer use."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any

import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from puda import command, machine_state, tlm_stream

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# cua-driver MCP client  (generic — unchanged from template)
# ─────────────────────────────────────────────────────────────────────────────

class CuaDriverClient:
    """
    Async MCP client for cua-driver.

    Starts cua-driver as a stdio subprocess, discovers its MCP tools, then
    exposes capture / click / type / key / scroll behind a stable async API.
    Tool-name resolution tries the Hermes wrapper name first, then native
    cua-driver names, so the client works across driver versions.
    """

    _CAPTURE_TOOLS = ["computer_use", "screenshot", "capture_screen", "get_window_state"]
    _CLICK_TOOLS   = ["computer_use", "click", "left_click"]
    _TYPE_TOOLS    = ["computer_use", "type", "type_text"]
    _KEY_TOOLS     = ["computer_use", "key", "press_key", "hotkey"]
    _SCROLL_TOOLS  = ["computer_use", "scroll", "scroll_element"]

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._ctx = None
        self._tools: dict[str, Any] = {}

    async def start(self) -> None:
        import shutil
        if not shutil.which("cua-driver"):
            raise RuntimeError(
                "cua-driver not found on PATH.\n"
                "Install it via Hermes Agent:\n"
                "  1. Download Hermes: https://hermes-agent.nousresearch.com\n"
                "  2. Run: hermes computer-use install\n"
                "  3. Verify: hermes computer-use doctor\n"
                "Or set HERMES_CUA_DRIVER_CMD to the full path of your cua-driver binary."
            )
        params = StdioServerParameters(command="cua-driver", args=["mcp"])
        self._ctx = stdio_client(params)
        read, write = await self._ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        tools_result = await self._session.list_tools()
        self._tools = {t.name: t for t in tools_result.tools}
        logger.info("cua-driver connected. Tools: %s", sorted(self._tools.keys()))

    async def stop(self) -> None:
        for obj in (self._session, self._ctx):
            if obj:
                try:
                    await obj.__aexit__(None, None, None)
                except Exception:
                    pass

    def _resolve(self, candidates: list[str]) -> str:
        for name in candidates:
            if name in self._tools:
                return name
        raise RuntimeError(
            f"None of {candidates} found in cua-driver. Available: {sorted(self._tools.keys())}"
        )

    async def _call(self, tool: str, args: dict) -> Any:
        assert self._session, "CuaDriverClient not started"
        return await self._session.call_tool(tool, args)

    async def capture(self, app: str | None = None, mode: str = "screenshot") -> Any:
        tool = self._resolve(self._CAPTURE_TOOLS)
        args: dict = {"action": "capture", "mode": mode} if tool == "computer_use" else {}
        if app:
            args["app"] = app
        return await self._call(tool, args)

    async def click(self, element: int | None = None, x: int | None = None,
                    y: int | None = None, button: str = "left") -> Any:
        tool = self._resolve(self._CLICK_TOOLS)
        args: dict = {"action": "click", "button": button} if tool == "computer_use" else {"button": button}
        if element is not None:
            args["element"] = element
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        return await self._call(tool, args)

    async def type_text(self, text: str) -> Any:
        tool = self._resolve(self._TYPE_TOOLS)
        args = {"action": "type", "text": text} if tool == "computer_use" else {"text": text}
        return await self._call(tool, args)

    async def key(self, keys: str, capture_after: bool = False) -> Any:
        tool = self._resolve(self._KEY_TOOLS)
        args = ({"action": "key", "keys": keys, "capture_after": capture_after}
                if tool == "computer_use" else {"keys": keys})
        return await self._call(tool, args)

    async def scroll(self, direction: str = "down", amount: int = 3) -> Any:
        tool = self._resolve(self._SCROLL_TOOLS)
        args = ({"action": "scroll", "direction": direction, "amount": amount}
                if tool == "computer_use" else {"direction": direction, "amount": amount})
        return await self._call(tool, args)

    @staticmethod
    def extract_image_b64(result: Any) -> str | None:
        content = getattr(result, "content", None)
        if not content:
            return None
        for block in content:
            if getattr(block, "type", None) == "image":
                return getattr(block, "data", None)
            if "image" in getattr(block, "mimeType", ""):
                return getattr(block, "data", None)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Generic GUI driver base  (reusable across instruments)
# ─────────────────────────────────────────────────────────────────────────────

class GuiDriver:
    """
    Base PUDA GUI driver — controls any desktop software via Hermes computer use.

    Subclass this and add @command methods for instrument-specific workflows.
    All vision analysis uses Claude via the ANTHROPIC_API_KEY in the environment
    (managed by Hermes — no separate configuration needed).
    """

    _STATUS_PROMPT = (
        "You are reading a screenshot of instrument control software. "
        "Extract every piece of status information visible on screen and return it "
        "as a single JSON object. Include all numeric readings, units, modes, states, "
        "alarms, and error messages you can see. Use snake_case keys."
    )
    _LLM_MODEL = "claude-sonnet-4-5"

    def __init__(self, target_app: str, capture_interval: float = 30.0) -> None:
        self.target_app = target_app
        self.capture_interval = capture_interval
        self._last_status: dict = {}
        self._anthropic = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        self._cua = CuaDriverClient()

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="guidriver-async-loop"
        )
        self._loop_thread.start()

    def startup(self) -> None:
        self._run(self._cua.start())
        logger.info("GuiDriver ready — target_app=%r", self.target_app)

    def _run(self, coro) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60)

    def _vision_query(self, img_b64: str, prompt: str) -> str:
        msg = self._anthropic.messages.create(
            model=self._LLM_MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64",
                                                  "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return msg.content[0].text

    def _parse_json(self, text: str) -> dict:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return {"raw": text}

    def _screenshot(self, mode: str = "screenshot") -> str:
        result = self._run(self._cua.capture(app=self.target_app, mode=mode))
        img = CuaDriverClient.extract_image_b64(result)
        if img is None:
            raise RuntimeError(
                f"cua-driver returned no image for app={self.target_app!r}. "
                "Ensure the app is visible and Screen Recording is granted."
            )
        return img

    def _som_click(self, description: str) -> int:
        """Capture SOM screenshot, ask LLM to identify element, click it. Returns element number."""
        img = self._screenshot(mode="som")
        prompt = (
            f"This screenshot has SOM-numbered UI elements. "
            f"Which number best matches: '{description}'? "
            f"Reply with ONLY the integer, nothing else."
        )
        response = self._vision_query(img, prompt).strip()
        m = re.search(r"\d+", response)
        if not m:
            raise RuntimeError(f"LLM could not identify element for {description!r}. Reply: {response!r}")
        elem = int(m.group())
        logger.info("_som_click: clicking element %d for %r", elem, description)
        self._run(self._cua.click(element=elem))
        return elem

    # ── base PUDA commands ─────────────────────────────────────────────────

    @machine_state
    def snapshot(self) -> dict:
        return {"target_app": self.target_app, "last_status": self._last_status}

    @command
    def capture_screenshot(self) -> dict:
        """Return a raw base64 PNG screenshot of the target application."""
        return {"app": self.target_app, "image_b64": self._screenshot()}

    @command
    def ask_screen(self, question: str) -> dict:
        """
        Ask the vision LLM a read-only question about the current screen state.

        Args:
            question: Free-form question, e.g. 'Is there an active error message?'

        Returns:
            dict: {'question': str, 'answer': str}
        """
        img = self._screenshot()
        answer = self._vision_query(img, question)
        return {"question": question, "answer": answer}

    @command
    def find_and_click(self, description: str) -> dict:
        """
        Locate a UI element by description using SOM vision and click it.

        Args:
            description: Plain-English description of the element to click.

        Returns:
            dict: {'clicked_element': int, 'description': str}
        """
        logger.info("find_and_click: %r", description)
        elem = self._som_click(description)
        return {"clicked_element": elem, "description": description}

    @command
    def press_key(self, keys: str) -> dict:
        """
        Press a keyboard shortcut in the target application.

        Args:
            keys: Key combination, e.g. 'escape', 'ctrl+s', 'f5'.

        Returns:
            dict: {'keys': str}
        """
        self._run(self._cua.key(keys))
        return {"keys": keys}

    @command
    def reset(self) -> bool:
        """Send Escape to cancel any in-progress operation."""
        self._run(self._cua.key("escape"))
        return True

    @command
    def shutdown(self) -> bool:
        """Close the cua-driver MCP session cleanly."""
        try:
            self._run(self._cua.stop())
        except Exception as e:
            logger.warning("shutdown error: %s", e)
        self._loop.call_soon_threadsafe(self._loop.stop)
        return True

    @tlm_stream(interval=30.0, name="instrument_status")
    def stream_status(self) -> dict | None:
        """Periodic vision-based status poll — published every capture_interval seconds."""
        try:
            img = self._screenshot()
            response = self._vision_query(img, self._STATUS_PROMPT)
            self._last_status = self._parse_json(response)
            return self._last_status
        except Exception as e:
            logger.warning("stream_status failed: %s", e)
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Opentrons OT-2 GUI driver
# ─────────────────────────────────────────────────────────────────────────────

class OpentronGuiDriver(GuiDriver):
    """
    PUDA GUI driver for the Opentrons OT-2 liquid-handling robot.

    Controls the **Opentrons desktop App** (not the robot directly) via
    Hermes computer use — no HTTP API or SSH required. All commands interact
    with the App's UI the same way a human operator would.

    Opentrons App navigation recap
    ───────────────────────────────
    Left sidebar  → Protocols | Devices | Settings
    Protocols tab → Import button (top-right) | protocol list (⋮ menu per row)
    Setup screen  → Robot Calibration → Labware Position Check → Proceed to Run
    Run tab       → Start run | Pause | Cancel run | live step log

    PUDA commands
    ─────────────
    status              — screenshot → LLM reads run state, step, progress
    get_protocol_list   — list all protocols visible in the Protocols tab
    import_protocol     — Import a protocol file into the App
    start_setup         — Open setup for a named protocol (⋮ → Start setup)
    select_robot        — Choose the OT-2 robot on the setup screen
    start_run           — Click "Start run" to begin the protocol
    pause_run           — Pause an active run
    resume_run          — Resume a paused run
    cancel_run          — Cancel / stop the current run
    get_run_progress    — Read current step and progress from the Run tab
    navigate_protocols  — Go to the Protocols tab in the sidebar
    navigate_devices    — Go to the Devices tab in the sidebar
    home                — Navigate to the Protocols tab (home screen)
    """

    _STATUS_PROMPT = (
        "You are reading a screenshot of the Opentrons App controlling an OT-2 robot. "
        "Extract the current state and return ONLY a JSON object with these fields "
        "(use null for fields not visible): "
        "run_status (idle/running/paused/complete/error/stopped), "
        "current_step (integer or null), "
        "total_steps (integer or null), "
        "current_step_description (string or null), "
        "protocol_name (string or null), "
        "robot_name (string or null), "
        "elapsed_time (string or null), "
        "errors (list of strings, empty list if none), "
        "tab (protocols/setup/run/devices/settings or null)."
    )

    def __init__(
        self,
        target_app: str = "Opentrons",
        robot_name: str = "",
        capture_interval: float = 15.0,
    ) -> None:
        super().__init__(target_app=target_app, capture_interval=capture_interval)
        self.robot_name = robot_name   # used by select_robot to pick the right OT-2

    # ── machine state ──────────────────────────────────────────────────────

    @machine_state
    def snapshot(self) -> dict:
        return {
            "target_app": self.target_app,
            "robot_name": self.robot_name,
            "last_status": self._last_status,
        }

    # ── status ─────────────────────────────────────────────────────────────

    @command
    def status(self) -> dict:
        """
        Capture a screenshot of the Opentrons App and extract the current run state.

        Returns:
            dict: run_status, current_step, total_steps, current_step_description,
                  protocol_name, robot_name, elapsed_time, errors, tab.
        """
        logger.info("status: reading Opentrons App screen")
        img = self._screenshot()
        response = self._vision_query(img, self._STATUS_PROMPT)
        self._last_status = self._parse_json(response)
        logger.info("status: %s", self._last_status)
        return self._last_status

    # ── navigation ─────────────────────────────────────────────────────────

    @command
    def navigate_protocols(self) -> bool:
        """
        Click 'Protocols' in the left sidebar to go to the Protocols tab.

        Returns:
            bool: True when the click was dispatched.
        """
        logger.info("navigate_protocols")
        self._som_click("Protocols in the left sidebar")
        return True

    @command
    def navigate_devices(self) -> bool:
        """
        Click 'Devices' in the left sidebar to go to the Devices / robot list tab.

        Returns:
            bool: True when the click was dispatched.
        """
        logger.info("navigate_devices")
        self._som_click("Devices in the left sidebar")
        return True

    @command
    def home(self) -> bool:
        """
        Navigate to the Protocols tab (the App's home screen).

        Returns:
            bool: True when navigation is complete.
        """
        return self.navigate_protocols()

    # ── protocol management ────────────────────────────────────────────────

    @command
    def get_protocol_list(self) -> dict:
        """
        Read the list of protocols shown on the Protocols tab.

        Navigates to the Protocols tab first if not already there, then uses
        vision to extract protocol names and their statuses.

        Returns:
            dict: {'protocols': [{'name': str, 'status': str}, ...]}
        """
        logger.info("get_protocol_list")
        self.navigate_protocols()
        img = self._screenshot()
        prompt = (
            "List every protocol visible in this Opentrons App Protocols tab. "
            "Return ONLY a JSON object: "
            '{\"protocols\": [{\"name\": \"<name>\", \"status\": \"<status>\"}]}'
        )
        response = self._vision_query(img, prompt)
        result = self._parse_json(response)
        logger.info("get_protocol_list: %s", result)
        return result

    @command
    def import_protocol(self, file_path: str) -> dict:
        """
        Import a protocol file into the Opentrons App.

        Clicks the 'Import' button in the Protocols tab to open the import sidebar,
        then uses the system file picker to select the given file path.

        Args:
            file_path: Absolute path to the protocol file (.py or .json),
                       e.g. 'C:\\protocols\\serial_dilution.py'.

        Returns:
            dict: {'file_path': str, 'imported': bool, 'message': str}
        """
        logger.info("import_protocol: %s", file_path)
        self.navigate_protocols()

        # Click the Import button (top-right of Protocols tab)
        self._som_click("Import button in the top right corner")

        # The import sidebar opens — click "Choose File" to open the file picker
        self._som_click("Choose File button in the import sidebar")

        # Type the file path directly into the system file picker and confirm
        import time
        time.sleep(0.5)  # let the file picker open
        self._run(self._cua.type_text(file_path))
        self._run(self._cua.key("return"))

        # Wait for analysis and verify success via screenshot
        time.sleep(2.0)
        img = self._screenshot()
        prompt = (
            f"Was the protocol file '{file_path}' successfully imported in this "
            "Opentrons App screenshot? "
            "Return ONLY JSON: {\"imported\": true/false, \"message\": \"<description>\"}"
        )
        result = self._parse_json(self._vision_query(img, prompt))
        result["file_path"] = file_path
        logger.info("import_protocol result: %s", result)
        return result

    # ── run lifecycle ──────────────────────────────────────────────────────

    @command
    def start_setup(self, protocol_name: str) -> dict:
        """
        Open the setup screen for a protocol by clicking its three-dot (⋮) menu
        and selecting 'Start setup'.

        Args:
            protocol_name: Name of the protocol as shown in the Protocols tab list.

        Returns:
            dict: {'protocol_name': str, 'setup_opened': bool}
        """
        logger.info("start_setup: %r", protocol_name)
        self.navigate_protocols()

        # Click the ⋮ menu for this specific protocol
        self._som_click(f"three-dot menu (⋮) for the protocol named '{protocol_name}'")

        # Click 'Start setup' from the dropdown
        self._som_click("Start setup option in the dropdown menu")

        import time
        time.sleep(1.0)
        return {"protocol_name": protocol_name, "setup_opened": True}

    @command
    def select_robot(self, robot_name: str = "") -> dict:
        """
        Select the OT-2 robot on the setup screen.

        If robot_name is empty, uses the driver's configured robot_name.
        If only one robot is available, clicks it directly.

        Args:
            robot_name: Display name of the robot in the App. Defaults to
                        the ROBOT_NAME set in .env.

        Returns:
            dict: {'robot_name': str, 'selected': bool}
        """
        name = robot_name or self.robot_name
        logger.info("select_robot: %r", name)
        if name:
            self._som_click(f"robot named '{name}' in the robot selection list")
        else:
            # Select the first available robot
            self._som_click("first available robot in the robot selection list")

        import time
        time.sleep(0.5)
        self._som_click("Proceed to setup button")
        return {"robot_name": name, "selected": True}

    @command
    def start_run(self) -> dict:
        """
        Click 'Start run' on the Run tab to begin executing the protocol.

        Call this after start_setup() → select_robot() → (optional) Labware
        Position Check. The Opentrons App must be on the setup or run screen.

        Returns:
            dict: {'started': bool, 'run_status': str}
        """
        logger.info("start_run")
        self._som_click("Start run button")
        import time
        time.sleep(1.5)
        return {"started": True, "run_status": "running"}

    @command
    def pause_run(self) -> dict:
        """
        Pause the currently running protocol.

        Returns:
            dict: {'paused': bool}
        """
        logger.info("pause_run")
        self._som_click("Pause button")
        import time
        time.sleep(0.5)
        return {"paused": True}

    @command
    def resume_run(self) -> dict:
        """
        Resume a paused protocol run.

        Returns:
            dict: {'resumed': bool}
        """
        logger.info("resume_run")
        self._som_click("Resume button")
        import time
        time.sleep(0.5)
        return {"resumed": True}

    @command
    def cancel_run(self) -> dict:
        """
        Cancel (stop) the current protocol run.

        The App will ask for confirmation — this command also confirms the
        cancellation by clicking the confirmation button.

        Returns:
            dict: {'cancelled': bool}
        """
        logger.info("cancel_run")
        self._som_click("Cancel run button")
        import time
        time.sleep(0.5)
        # Confirm the cancellation dialog if it appears
        img = self._screenshot()
        prompt = (
            "Is there a cancellation confirmation dialog visible in this screenshot? "
            "Reply with only YES or NO."
        )
        if "YES" in self._vision_query(img, prompt).upper():
            self._som_click("confirm cancellation button in the dialog")
        return {"cancelled": True}

    # ── run monitoring ─────────────────────────────────────────────────────

    @command
    def get_run_progress(self) -> dict:
        """
        Read the current run progress from the Run tab.

        Uses vision to extract the current step number, total steps, step
        description, elapsed time, and any errors shown in the run log.

        Returns:
            dict: run_status, current_step, total_steps, current_step_description,
                  elapsed_time, errors.
        """
        logger.info("get_run_progress")
        img = self._screenshot()
        prompt = (
            "Read the Opentrons App Run tab and extract the run progress. "
            "Return ONLY JSON: "
            "{\"run_status\": \"running|paused|complete|error\", "
            "\"current_step\": <int or null>, "
            "\"total_steps\": <int or null>, "
            "\"current_step_description\": \"<string or null>\", "
            "\"elapsed_time\": \"<string or null>\", "
            "\"errors\": [<strings>]}"
        )
        result = self._parse_json(self._vision_query(img, prompt))
        logger.info("get_run_progress: %s", result)
        return result

    # ── telemetry stream ───────────────────────────────────────────────────

    @tlm_stream(interval=15.0, name="run_status")
    def stream_run_status(self) -> dict | None:
        """
        Publish Opentrons run status every 15 seconds.

        Captures the App screen and extracts run_status, current step,
        progress, and any errors. Published to:
            puda.<machine_id>.tlm.stream.run_status
        """
        try:
            img = self._screenshot()
            response = self._vision_query(img, self._STATUS_PROMPT)
            self._last_status = self._parse_json(response)
            return self._last_status
        except Exception as e:
            logger.warning("stream_run_status failed: %s", e)
            return None
