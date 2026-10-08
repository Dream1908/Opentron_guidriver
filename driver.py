"""PUDA GUI driver for Opentrons OT-2 — controls the Opentrons desktop App via Hermes computer use."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any


from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from puda import command

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
        self._pid: int | None = None
        self._window_id: int | None = None
        self._snapshot_id: str | None = None
        self._last_elements: list[dict[str, Any]] = []

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
        # Force UTF-8 in the Python-based Windows child process.
        params = StdioServerParameters(
            command="cua-driver", args=["mcp"],
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            encoding="utf-8", encoding_error_handler="replace",
        )
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

    @staticmethod
    def _structured(result: Any) -> dict[str, Any]:
        value = getattr(result, "structuredContent", None)
        if value is None:
            value = getattr(result, "structured_content", None)
        return value if isinstance(value, dict) else {}

    async def _matching_windows(self, app: str) -> list[dict[str, Any]]:
        result = await self._call("list_windows", {"on_screen_only": True})
        windows = self._structured(result).get("windows", [])
        needle = app.casefold()
        matches = [
            window for window in windows
            if (needle in str(window.get("app_name", "")).casefold()
                or needle in str(window.get("title", "")).casefold())
            and not window.get("minimized", False)
        ]
        if not matches:
            raise RuntimeError(
                f"No visible, non-minimized window matching {app!r}. "
                "Open the Opentrons App in the logged-in desktop session."
            )
        return matches

    async def capture(self, app: str | None = None, mode: str = "screenshot") -> Any:
        tool = self._resolve(self._CAPTURE_TOOLS)
        if tool == "get_window_state":
            if not app:
                raise RuntimeError("An app name is required for window capture")

            # Electron can leave a second blank top-level window behind. Inspect
            # every matching window and use the one with the richest UI tree.
            candidates: list[tuple[int, int, Any, dict[str, Any], int, int]] = []
            for window in await self._matching_windows(app):
                pid = int(window["pid"])
                window_id = int(window["window_id"])
                result = await self._call(tool, {
                    "pid": pid,
                    "window_id": window_id,
                    "include_accessibility_tree": True,
                    "include_screenshot": True,
                })
                structured = self._structured(result)
                score = int(structured.get("total_element_count", 0))
                image_size = len(self.extract_image_b64(result) or "")
                candidates.append((score, image_size, result, structured, pid, window_id))

            _, _, result, structured, self._pid, self._window_id = max(
                candidates, key=lambda item: (item[0], item[1])
            )
            self._snapshot_id = structured.get("snapshot_id")
            self._last_elements = structured.get("elements", [])
            return result

        args: dict = {"action": "capture", "mode": mode} if tool == "computer_use" else {}
        if app:
            args["app"] = app
        return await self._call(tool, args)
    async def click(self, element: int | None = None, x: int | None = None,
                    y: int | None = None, button: str = "left") -> Any:
        tool = self._resolve(self._CLICK_TOOLS)
        args: dict = {"action": "click", "button": button} if tool == "computer_use" else {"button": button}
        if tool == "click" and self._pid is not None:
            args["pid"] = self._pid
        if element is not None:
            if tool == "click":
                args["element_index"] = element
                args["window_id"] = self._window_id
                if self._snapshot_id:
                    args["snapshot_id"] = self._snapshot_id
            else:
                args["element"] = element
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        return await self._call(tool, args)

    async def type_text(self, text: str) -> Any:
        tool = self._resolve(self._TYPE_TOOLS)
        args = {"action": "type", "text": text} if tool == "computer_use" else {"text": text}
        if tool == "type_text" and self._pid is not None:
            args.update({"pid": self._pid, "window_id": self._window_id})
        return await self._call(tool, args)

    async def key(self, keys: str, capture_after: bool = False) -> Any:
        tool = self._resolve(self._KEY_TOOLS)
        if tool == "computer_use":
            args = {"action": "key", "keys": keys, "capture_after": capture_after}
        elif tool == "press_key":
            parts = keys.lower().split("+")
            args = {"key": parts[-1], "modifiers": parts[:-1]}
            if self._pid is not None:
                args.update({"pid": self._pid, "window_id": self._window_id})
        else:
            args = {"keys": keys}
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

    Public methods are exposed as PUDA commands; helpers use a leading underscore.
    Screen capture and UI interaction use cua-driver only; no LLM is required.
    """

    def __init__(self, target_app: str) -> None:
        self.target_app = target_app
        self._last_status: dict = {}
        self._cua = CuaDriverClient()

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="guidriver-async-loop"
        )
        self._loop_thread.start()

    def _startup(self) -> None:
        self._run(self._cua.start())
        logger.info("GuiDriver ready — target_app=%r", self.target_app)

    def _run(self, coro) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60)

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
        """Click the accessibility element that best matches a description."""
        wanted = set(re.findall(r"[a-z0-9]+", description.casefold()))
        ignored = {"a", "an", "the", "in", "on", "to", "of", "for", "button", "option"}
        wanted -= ignored

        # Electron can temporarily expose only its document root. Retry fresh
        # snapshots before falling back to pixels so element invocation remains
        # the preferred, background-safe route.
        for attempt in range(3):
            self._screenshot(mode="som")
            candidates: list[tuple[int, int, str]] = []
            for element in self._cua._last_elements:
                label = " ".join(str(element.get(key, "")) for key in ("label", "role", "value"))
                words = set(re.findall(r"[a-z0-9]+", label.casefold()))
                score = len(wanted & words)
                if score and element.get("enabled", True):
                    candidates.append((score, int(element["element_index"]), label))
            if candidates:
                _, elem, label = max(candidates, key=lambda item: (item[0], -item[1]))
                logger.info("accessibility click: element %d (%s) for %r", elem, label, description)
                self._run(self._cua.click(element=elem))
                return elem
            if attempt < 2:
                time.sleep(0.25)

        sidebar_fallbacks = {"protocols": (45, 205), "devices": (45, 277)}
        lowered = description.casefold()
        for name, (x, y) in sidebar_fallbacks.items():
            if name in lowered:
                logger.info("coordinate fallback: clicking %s at (%d, %d)", name, x, y)
                self._run(self._cua.click(x=x, y=y))
                return -1
        raise RuntimeError(
            f"No accessible UI element matched {description!r}. "
            "Capture the screen and use an explicit element or coordinate action."
        )

    def _verify_route(self, route: str) -> None:
        """Refresh the UI tree and fail unless the Electron URL is on route."""
        expected = f"#/{route.lstrip('/')}"
        for attempt in range(5):
            self._screenshot(mode="som")
            values = [str(element.get("value", "")) for element in self._cua._last_elements]
            if any(expected in value for value in values):
                return
            if attempt < 4:
                time.sleep(0.25)
        raise RuntimeError(f"Opentrons navigation did not reach {expected}")
    # ── base PUDA commands ─────────────────────────────────────────────────
    def _snapshot(self) -> dict:
        return {"target_app": self.target_app, "last_status": self._last_status}
    @command
    def capture_screenshot(self) -> dict:
        """Return a raw base64 PNG screenshot of the target application."""
        return {"app": self.target_app, "image_b64": self._screenshot()}
    @command
    def ask_screen(self, question: str) -> dict:
        """Capture the screen without interpreting it."""
        return {"question": question, "app": self.target_app, "image_b64": self._screenshot()}
    @command
    def find_and_click(self, description: str) -> dict:
        """
        Locate and click an accessibility element by description.

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
    def reset(self) -> bool:
        """Send Escape to cancel any in-progress operation."""
        self._run(self._cua.key("escape"))
        return True
    def shutdown(self) -> bool:
        """Close the cua-driver MCP session cleanly."""
        try:
            self._run(self._cua.stop())
        except Exception as e:
            logger.warning("shutdown error: %s", e)
        self._loop.call_soon_threadsafe(self._loop.stop)
        return True
    def _stream_status(self) -> dict | None:
        """Publish a raw instrument screenshot without analysis."""
        try:
            self._last_status = {"target_app": self.target_app, "image_b64": self._screenshot()}
            return self._last_status
        except Exception as e:
            logger.warning("stream_status failed: %s", e)
            return None


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
    status              — returns the raw screenshot without interpretation
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

    def __init__(
        self,
        target_app: str = "Opentrons",
        robot_name: str = "",
    ) -> None:
        super().__init__(target_app=target_app)
        self.robot_name = robot_name   # used by select_robot to pick the right OT-2

    # ── machine state ──────────────────────────────────────────────────────
    def _snapshot(self) -> dict:
        return {
            "target_app": self.target_app,
            "robot_name": self.robot_name,
            "last_status": self._last_status,
        }

    # ── status ─────────────────────────────────────────────────────────────
    @command
    def status(self) -> dict:
        """Navigate to Devices and return the current Opentrons image."""
        self.navigate_devices()
        self._last_status = {"target_app": self.target_app, "image_b64": self._screenshot()}
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
        self._run(self._cua.key("escape"))
        self._som_click("Protocols in the left sidebar")
        self._verify_route("protocols")
        return True
    @command
    def navigate_devices(self) -> bool:
        """
        Click 'Devices' in the left sidebar to go to the Devices / robot list tab.

        Returns:
            bool: True when the click was dispatched.
        """
        logger.info("navigate_devices")
        self._run(self._cua.key("escape"))
        self._som_click("Devices in the left sidebar")
        self._verify_route("devices")
        return True
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
        """Return a Protocols-tab screenshot and accessibility elements."""
        self.navigate_protocols()
        return {"image_b64": self._screenshot(), "elements": self._cua._last_elements}
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

        # Return the resulting UI state without automated interpretation.
        time.sleep(2.0)
        return {"file_path": file_path, "image_b64": self._screenshot()}

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
        # Try the accessible confirmation button if the dialog is present.
        try:
            self._som_click("confirm cancellation")
        except RuntimeError:
            pass
        return {"cancelled": True, "image_b64": self._screenshot()}

    # ── run monitoring ─────────────────────────────────────────────────────
    @command
    def get_run_progress(self) -> dict:
        """Return the current Run-tab screenshot without analysis."""
        return {"target_app": self.target_app, "image_b64": self._screenshot()}

    # ── telemetry stream ───────────────────────────────────────────────────
    def _stream_run_status(self) -> dict | None:
        """Publish a raw Opentrons screenshot every 15 seconds."""
        try:
            self._last_status = {"target_app": self.target_app, "image_b64": self._screenshot()}
            return self._last_status
        except Exception as e:
            logger.warning("stream_run_status failed: %s", e)
            return None
