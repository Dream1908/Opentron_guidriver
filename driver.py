"""PUDA GUI driver for Opentrons OT-2 — controls the Opentrons desktop App via Hermes computer use."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
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
        self._capture_meta: dict[str, Any] = {}

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
        # Pass stderr as bytes. On Windows, a text stderr stream uses the active
        # ANSI code page (often cp1252); cua-driver may emit UTF-8 bytes that are
        # invalid in that code page and crash subprocess._readerthread.
        self._ctx = stdio_client(params, errlog=getattr(sys.stderr, "buffer", sys.stderr))
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
        # Include minimized/off-screen windows so they can be restored onto the
        # user's primary display instead of being controlled invisibly.
        result = await self._call("list_windows", {"on_screen_only": False})
        windows = self._structured(result).get("windows", [])
        needle = app.casefold()
        matches = [
            window for window in windows
            if (needle in str(window.get("app_name", "")).casefold()
                or needle in str(window.get("title", "")).casefold())
        ]
        if not matches:
            raise RuntimeError(
                f"No window matching {app!r}. "
                "Open the Opentrons App in the logged-in desktop session."
            )
        return matches

    async def _make_window_visible(self, window: dict[str, Any]) -> dict[str, Any]:
        """Restore an exact window to the primary display and foreground it."""
        required = {"get_screen_size", "set_window_frame", "bring_to_front"}
        missing = required - self._tools.keys()
        if missing:
            raise RuntimeError(
                "Visible foreground control is unavailable; cua-driver is missing "
                + ", ".join(sorted(missing))
            )

        pid = int(window["pid"])
        window_id = int(window["window_id"])
        bounds = window.get("bounds", {})
        screen = self._structured(await self._call("get_screen_size", {}))
        screen_width = int(screen.get("width", 0))
        screen_height = int(screen.get("height", 0))
        if not screen_width or not screen_height:
            raise RuntimeError("Could not determine the primary display size")

        x = int(bounds.get("x", 0))
        y = int(bounds.get("y", 0))
        width = int(bounds.get("width", 0))
        height = int(bounds.get("height", 0))
        intersects_primary = (
            width > 0 and height > 0 and x < screen_width and y < screen_height
            and x + width > 0 and y + height > 0
        )
        needs_reposition = (
            window.get("minimized", False)
            or not window.get("is_on_screen", False)
            or not intersects_primary
        )
        # Windows cannot resize a minimized HWND. Restore/activate it first,
        # then place it on the primary display and reaffirm foreground focus.
        await self._call("bring_to_front", {"pid": pid, "window_id": window_id})
        if needs_reposition:
            width = min(max(width, 1000), screen_width)
            height = min(max(height, 700), screen_height)
            await self._call("set_window_frame", {
                "pid": pid, "window_id": window_id, "x": 0, "y": 0,
                "width": width, "height": height,
            })
            await self._call("bring_to_front", {"pid": pid, "window_id": window_id})

        refreshed = self._structured(
            await self._call("list_windows", {"on_screen_only": False})
        ).get("windows", [])
        exact = next(
            (item for item in refreshed if int(item.get("window_id", -1)) == window_id),
            None,
        )
        if not exact or exact.get("minimized", False) or not exact.get("is_on_screen", False):
            raise RuntimeError(
                f"Refusing GUI input: {window.get('title', 'target window')!r} "
                "could not be made visible on the user's screen"
            )
        return exact

    @classmethod
    def _require_input(cls, result: Any) -> Any:
        """Fail closed on rejected/no-op input; unverifiable delivery needs read-back."""
        structured = cls._structured(result)
        if (getattr(result, "is_error", False) or getattr(result, "isError", False)
                or structured.get("ok") is False
                or structured.get("effect") == "suspected_noop"):
            raise RuntimeError(
                f"cua-driver input rejected: {structured or str(getattr(result, 'content', result))}"
            )
        return result

    async def capture(self, app: str | None = None, mode: str = "screenshot") -> Any:
        tool = self._resolve(self._CAPTURE_TOOLS)
        if tool == "get_window_state":
            if not app:
                raise RuntimeError("An app name is required for window capture")

            # Electron can leave a second blank top-level window behind. Inspect
            # every matching window and use the one with the richest UI tree.
            candidates: list[tuple[int, int, int, Any, dict[str, Any], int, int]] = []
            for window in await self._matching_windows(app):
                window = await self._make_window_visible(window)
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
                title = str(window.get("title", "")).casefold()
                modal = int(title in {"open", "save as"} or title.startswith("blob:"))
                candidates.append((modal, score, image_size, result, structured, pid, window_id))

            _, _, _, result, structured, self._pid, self._window_id = max(
                candidates, key=lambda item: (item[0], item[1], item[2])
            )
            self._snapshot_id = structured.get("snapshot_id")
            self._last_elements = structured.get("elements", [])
            self._capture_meta = structured
            return result

        args: dict = {"action": "capture", "mode": mode} if tool == "computer_use" else {}
        if app:
            args["app"] = app
        return await self._call(tool, args)
    async def click(self, element: int | None = None, x: int | None = None,
                    y: int | None = None, button: str = "left",
                    delivery_mode: str | None = "foreground") -> Any:
        tool = self._resolve(self._CLICK_TOOLS)
        args: dict = {"action": "click", "button": button} if tool == "computer_use" else {"button": button}
        if tool == "click" and self._pid is not None:
            args["pid"] = self._pid
            args["window_id"] = self._window_id
            if delivery_mode:
                args["delivery_mode"] = delivery_mode
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
        return self._require_input(await self._call(tool, args))

    async def type_text(self, text: str, delivery_mode: str | None = "foreground") -> Any:
        tool = self._resolve(self._TYPE_TOOLS)
        args = {"action": "type", "text": text} if tool == "computer_use" else {"text": text}
        if tool == "type_text" and self._pid is not None:
            args.update({"pid": self._pid, "window_id": self._window_id})
            if delivery_mode:
                args["delivery_mode"] = delivery_mode
        return self._require_input(await self._call(tool, args))

    async def set_value(self, element: int, value: str) -> Any:
        """Set a native UIA value using the current captured-window snapshot."""
        if "set_value" not in self._tools:
            raise RuntimeError("cua-driver does not expose set_value")
        args: dict[str, Any] = {
            "pid": self._pid,
            "window_id": self._window_id,
            "element_index": element,
            "value": value,
        }
        if self._snapshot_id:
            args["snapshot_id"] = self._snapshot_id
        return self._require_input(await self._call("set_value", args))

    async def key(self, keys: str, capture_after: bool = False,
                  delivery_mode: str | None = "foreground") -> Any:
        tool = self._resolve(self._KEY_TOOLS)
        if tool == "computer_use":
            args = {"action": "key", "keys": keys, "capture_after": capture_after}
        elif tool == "press_key":
            parts = keys.lower().split("+")
            args = {"key": parts[-1], "modifiers": parts[:-1]}
            if self._pid is not None:
                args.update({"pid": self._pid, "window_id": self._window_id})
                if delivery_mode:
                    args["delivery_mode"] = delivery_mode
        else:
            args = {"keys": keys}
        return self._require_input(await self._call(tool, args))

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
                selected = next(e for e in self._cua._last_elements if int(e["element_index"]) == elem)
                frame = selected.get("frame", {})
                window = self._cua._capture_meta.get("window_bounds", {})
                if frame and window:
                    cx = frame["x"] + frame["w"] / 2
                    cy = frame["y"] + frame["h"] / 2
                    if not (window["x"] <= cx < window["x"] + window["width"]
                            and window["y"] <= cy < window["y"] + window["height"]):
                        raise RuntimeError(
                            f"Accessibility coordinates for {description!r} are outside the captured window. "
                            "Use PUDA click_at with coordinates grounded in the returned screenshot."
                        )
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
        expected = f"/{route.strip('/')}"
        for attempt in range(5):
            self._screenshot(mode="som")
            values = [
                str(element.get("value", ""))
                for element in self._cua._last_elements
                if str(element.get("role", "")).casefold() == "document"
            ]
            fragments = [value.split("#", 1)[1].rstrip("/") for value in values if "#" in value]
            if expected in fragments:
                return
            if attempt < 4:
                time.sleep(0.25)
        raise RuntimeError(f"Opentrons navigation did not reach #{expected}")
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
    def click_at(self, x: int, y: int) -> dict:
        """Dispatch a visible foreground click using fresh screenshot pixels.

        Use only coordinates grounded in a fresh screenshot, not native AX bounds.
        The target window is restored to the primary display and foregrounded.
        No automatic repeat is performed.
        """
        self._screenshot()
        width = self._cua._capture_meta.get("screenshot_width")
        height = self._cua._capture_meta.get("screenshot_height")
        if width is None or height is None or not (0 <= x < width and 0 <= y < height):
            raise ValueError("Click coordinates must be inside the fresh screenshot")
        result = self._run(self._cua.click(x=x, y=y, delivery_mode="foreground"))
        time.sleep(0.5)
        return {"x": x, "y": y,
                "effect": CuaDriverClient._structured(result).get("effect", "unverifiable"),
                "image_b64": self._screenshot()}

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
        # Background Escape is rejected by this Electron surface. Click the
        # route directly; verification still fails closed if a modal blocks it.
        self._som_click("Protocols in the left sidebar")
        try:
            self._verify_route("protocols")
        except RuntimeError:
            # On protocol-detail pages the already-selected sidebar link can
            # be an Electron no-op. Prefer the exact-route breadcrumb exposed
            # farther right in the fresh accessibility snapshot.
            self._screenshot(mode="som")
            links = [
                element for element in self._cua._last_elements
                if str(element.get("role", "")).casefold() in {"link", "hyperlink"}
                and str(element.get("value", "")).split("#", 1)[-1].rstrip("/") == "/protocols"
            ]
            if not links:
                raise
            breadcrumb = max(links, key=lambda element: element.get("frame", {}).get("x", 0))
            self._run(self._cua.click(element=int(breadcrumb["element_index"])))
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
    def import_protocol(self, file_path: str, upload_x: int | None = None,
                        upload_y: int | None = None) -> dict:
        """
        Import a protocol file into the Opentrons App.

        Clicks the 'Import' button in the Protocols tab to open the import sidebar,
        then uses the system file picker to select the given file path.

        Args:
            file_path: Absolute path to the protocol file (.py or .json),
                       e.g. 'C:\\protocols\\serial_dilution.py'.
            upload_x: Optional Upload-button X in the fresh screenshot (not AX coordinates).
            upload_y: Optional Upload-button Y; supply both coordinates together.

        Returns:
            dict: {'file_path': str, 'imported': bool, 'message': str}
        """
        logger.info("import_protocol: %s", file_path)
        if (upload_x is None) != (upload_y is None):
            raise ValueError("Supply both upload_x and upload_y, or neither")
        self.navigate_protocols()

        # Click the Import button (top-right of Protocols tab)
        self._som_click("Import button in the top right corner")

        # Current app calls this Upload. Broken off-window AX coordinates must
        # be replaced only by explicit, screenshot-grounded pixels, not guesses.
        time.sleep(0.5)
        if upload_x is not None:
            self.click_at(upload_x, upload_y)
        else:
            self._som_click("Upload")

        time.sleep(0.5)
        windows = self._run(self._cua._matching_windows(self.target_app))
        if not any(str(window.get("title", "")).casefold() == "open" for window in windows):
            if upload_x is None:
                raise RuntimeError(
                    "Upload did not open the file dialog; provide screenshot-grounded upload_x/upload_y"
                )

            # Read-back proved that Electron dropped one of the background
            # clicks. Re-open Import and retry the two explicitly authorized
            # controls with foreground delivery, then verify the native dialog.
            self.navigate_protocols()
            self._screenshot(mode="som")
            imports = [
                element for element in self._cua._last_elements
                if str(element.get("role", "")).casefold() == "button"
                and str(element.get("label", "")).casefold() == "import"
            ]
            if not imports:
                raise RuntimeError("Import button is not available on the Protocols list")
            frame = imports[0].get("frame", {})
            bounds = self._cua._capture_meta.get("window_bounds", {})
            if not frame or not bounds:
                raise RuntimeError("Import button has no screenshot-grounded frame")
            import_x = int(frame["x"] - bounds["x"] + frame["w"] / 2)
            import_y = int(frame["y"] - bounds["y"] + frame["h"] / 2)
            self._run(self._cua.click(
                x=import_x, y=import_y, delivery_mode="foreground"
            ))
            time.sleep(0.5)
            self._screenshot()
            self._run(self._cua.click(
                x=upload_x, y=upload_y, delivery_mode="foreground"
            ))
            time.sleep(0.5)
            windows = self._run(self._cua._matching_windows(self.target_app))
            if not any(str(window.get("title", "")).casefold() == "open" for window in windows):
                raise RuntimeError("Upload did not open the native Open file dialog")

        # Type the file path directly into the system file picker and confirm
        self._screenshot()  # select the modal file dialog before sending text
        dialog_elements = getattr(self._cua, "_last_elements", [])
        filename_fields = [
            element for element in dialog_elements
            if str(element.get("role", "")).casefold() == "edit"
            and str(element.get("label", "")).casefold() == "file name:"
        ]
        open_buttons = [
            element for element in dialog_elements
            if str(element.get("role", "")).casefold() in {"button", "splitbutton"}
            and str(element.get("label", "")).casefold() == "open"
            and "invoke" in element.get("actions", [])
        ]
        if filename_fields and open_buttons and "set_value" in getattr(self._cua, "_tools", {}):
            self._run(self._cua.set_value(int(filename_fields[0]["element_index"]), file_path))
            open_button = max(
                open_buttons,
                key=lambda element: (
                    element.get("frame", {}).get("w", 0)
                    * element.get("frame", {}).get("h", 0)
                ),
            )
            self._run(self._cua.click(element=int(open_button["element_index"])))
        else:
            # Compatibility fallback for older cua-driver versions. Native
            # Windows dialogs reject background keystrokes, and this exact
            # path was explicitly authorized by the import command caller.
            self._run(self._cua.type_text(file_path, delivery_mode="foreground"))
            self._run(self._cua.key("return", delivery_mode="foreground"))

        # Return the resulting UI state without automated interpretation.
        time.sleep(2.0)
        if hasattr(self._cua, "_matching_windows"):
            windows = self._run(self._cua._matching_windows(self.target_app))
            if any(str(window.get("title", "")).casefold() == "open" for window in windows):
                raise RuntimeError("Protocol import did not close the Open file dialog")
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
        time.sleep(1.5)
        image = self._screenshot()
        labels = {str(e.get("label", "")).casefold() for e in self._cua._last_elements}
        status = next((s for s in ("running", "not started", "paused", "completed", "failed")
                       if s in labels), "unverified")
        return {"started": status == "running", "run_status": status, "image_b64": image}
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
