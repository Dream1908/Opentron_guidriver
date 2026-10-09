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
        import os
        child_env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        # On Linux the MCP stdio client only forwards a small allowlist of env
        # vars, so display-session variables must be passed explicitly or
        # cua-driver cannot see any windows.
        for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
                    "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS"):
            if os.environ.get(key):
                child_env[key] = os.environ[key]
        params = StdioServerParameters(
            command="cua-driver", args=["mcp"],
            env=child_env,
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

    async def _find_file_dialog(self) -> dict[str, Any] | None:
        """Return the native file-open dialog window, if one is open.

        Windows names it "Open" under the app; on Linux the GNOME file chooser
        is owned by xdg-desktop-portal-* and titled "Open Files", so it must be
        found among all windows rather than by app name.
        """
        result = await self._call("list_windows", {"on_screen_only": False})
        for window in self._structured(result).get("windows", []):
            title = str(window.get("title", "")).casefold()
            app = str(window.get("app_name", "")).casefold()
            if title in {"open", "open file", "open files"} and (
                "portal" in app or "opentrons" in app or not app
            ):
                return window
        return None

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
# Opentrons OT-2 GUI driver
# ─────────────────────────────────────────────────────────────────────────────

class OpentronGuiDriver:
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
        self.target_app = target_app
        self._last_status: dict = {}
        self._cua = CuaDriverClient()

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="guidriver-async-loop"
        )
        self._loop_thread.start()
        self.robot_name = robot_name   # used by select_robot to pick the right OT-2

    # ── generic GUI helpers & base PUDA commands (merged from the former GuiDriver) ──

    def _startup(self) -> None:
        self._run(self._cua.start())
        logger.info("OpentronGuiDriver ready — target_app=%r", self.target_app)

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

    # ── exact-label helpers ────────────────────────────────────────────────
    # The fuzzy ``_som_click`` scores word overlap and can pick the wrong
    # control (e.g. close a side panel instead of advancing it). Commands that
    # advance a run use these exact-label helpers instead.

    def _snapshot_elements(self, attempts: int = 3) -> list[dict]:
        """Capture a fresh accessibility snapshot and return its elements."""
        elements: list[dict] = []
        for attempt in range(attempts):
            self._screenshot(mode="som")
            elements = list(getattr(self._cua, "_last_elements", []) or [])
            if elements:
                break
            if attempt < attempts - 1:
                time.sleep(0.25)
        return elements

    @staticmethod
    def _exact(elements: list[dict], label: str, roles: set[str] | None = None) -> list[dict]:
        """Elements whose label equals ``label`` (case-insensitive, trimmed)."""
        wanted = label.strip().casefold()
        return [
            element for element in elements
            if str(element.get("label", "")).strip().casefold() == wanted
            and (roles is None or str(element.get("role", "")).casefold() in roles)
            and element.get("enabled", True)
        ]

    def _invoke_element(self, element: dict, tolerant: bool = False) -> bool:
        """Invoke one accessibility element.

        With ``tolerant=True`` a cua-driver "input rejected" error is logged and
        swallowed (returns False): on this Electron surface some clicks are
        reported as rejected although they were delivered, so the caller MUST
        verify the effect by read-back before trusting or failing.
        """
        try:
            self._run(self._cua.click(element=int(element["element_index"])))
        except RuntimeError as exc:
            if not tolerant or "input rejected" not in str(exc):
                raise
            logger.warning(
                "click on %r reported rejected; verifying by read-back (%s)",
                element.get("label"), exc,
            )
            return False
        return True

    def _click_label(self, label: str, fallback_description: str | None = None,
                     roles: set[str] | None = None, tolerant: bool = False) -> int:
        """Click the element labelled exactly ``label``; optionally fall back to fuzzy matching."""
        matches = self._exact(self._snapshot_elements(), label, roles or {"button"})
        if matches:
            self._invoke_element(matches[0], tolerant=tolerant)
            return int(matches[0]["element_index"])
        if fallback_description is None:
            raise RuntimeError(f"No enabled control labelled {label!r} on screen")
        return self._som_click(fallback_description)

    def _pending_dialog(self, elements: list[dict]) -> dict | None:
        """Describe a modal confirmation dialog, or None when none is showing.

        Opentrons confirmation modals ("Are you sure…?") always offer a
        'Go back' button next to the confirming button.
        """
        go_back = self._exact(elements, "Go back", {"button"})
        if not go_back:
            return None
        anchor = go_back[0]
        frame = anchor.get("frame") or {}
        texts: list[str] = []
        buttons: list[str] = []
        modal = self._modal_frame(elements, anchor)
        if modal:
            # The App exposes the dialog's exact bounds: take what is inside.
            for element in elements:
                label = str(element.get("label", "")).strip()
                if element is anchor or not label or label.casefold() == "none":
                    continue
                if not self._inside(element, modal):
                    continue
                role = str(element.get("role", "")).casefold()
                if role == "button":
                    buttons.append(label)
                elif role == "text":
                    texts.append(label)
        elif frame:
            ax, ay = frame["x"], frame["y"]
            for element in elements:
                eframe = element.get("frame") or {}
                if not eframe or element is anchor:
                    continue
                role = str(element.get("role", "")).casefold()
                label = str(element.get("label", "")).strip()
                if not label or label.casefold() == "none":
                    continue
                if role == "button" and abs(eframe["y"] - ay) <= 25:
                    buttons.append(label)
                elif role == "text" and 0 < ay - eframe["y"] <= 250 and abs(eframe["x"] - ax) <= 500:
                    texts.append(label)
        buttons.append(str(anchor.get("label")))
        return {"text": texts, "buttons": buttons}

    @classmethod
    def _modal_frame(cls, elements: list[dict], anchor: dict | None = None) -> dict | None:
        """Frame of the App's modal container (``ModalShell_ModalArea``), if exposed.

        The tree can expose several "modal" groups (some page-wide); take the
        smallest one that contains ``anchor`` (the dialog's 'Go back' button).
        """
        frames = [
            element["frame"] for element in elements
            if "modal" in str(element.get("label", "")).casefold()
            and str(element.get("role", "")).casefold() == "group"
            and element.get("frame")
            and (anchor is None or cls._inside(anchor, element["frame"]))
        ]
        return min(frames, key=lambda f: f["w"] * f["h"], default=None)

    @staticmethod
    def _inside(element: dict, frame: dict) -> bool:
        """True when the element lies entirely within ``frame``.

        Whole-element containment (not just the centre) so that a wide page
        element sitting behind a dialog is not mistaken for dialog content.
        """
        f = element.get("frame") or {}
        if not f:
            return False
        return (frame["x"] <= f["x"] and f["x"] + f["w"] <= frame["x"] + frame["w"]
                and frame["y"] <= f["y"] and f["y"] + f["h"] <= frame["y"] + frame["h"])

    def _dialog_confirm_button(self, elements: list[dict], label: str) -> dict | None:
        """The ``label`` button that sits in the dialog (inside its modal frame, else nearest 'Go back')."""
        go_back = self._exact(elements, "Go back", {"button"})
        candidates = self._exact(elements, label, {"button"})
        if not go_back or not candidates:
            return None
        modal = self._modal_frame(elements, go_back[0])
        if modal:
            inside = [c for c in candidates if self._inside(c, modal)]
            if inside:
                return inside[0]
        gframe = go_back[0].get("frame") or {}

        def distance(element: dict) -> float:
            frame = element.get("frame") or {}
            if not frame or not gframe:
                return 0.0
            return abs(frame["x"] - gframe["x"]) + abs(frame["y"] - gframe["y"])

        return min(candidates, key=distance)

    def _current_file_dialog(self) -> dict | None:
        """Return the open native file dialog, or None.

        First the original check (a window of the target app titled "Open",
        which is how Windows exposes it); then, for Linux/GNOME where the
        chooser belongs to xdg-desktop-portal, a search across all windows.
        """
        windows = self._run(self._cua._matching_windows(self.target_app))
        for window in windows:
            if str(window.get("title", "")).casefold() == "open":
                return window
        finder = getattr(self._cua, "_find_file_dialog", None)
        return self._run(finder()) if finder is not None else None

    def _wait_file_dialog(self, appear: bool = True, timeout: float = 15.0) -> dict | None:
        """Poll until the native file dialog appears (or disappears if appear=False)."""
        deadline = time.monotonic() + timeout
        while True:
            dialog = self._current_file_dialog()
            if appear and dialog is not None:
                return dialog
            if not appear and dialog is None:
                return None
            if time.monotonic() >= deadline:
                return dialog
            time.sleep(0.5)

    def _type_into_file_dialog(self, dialog: dict, file_path: str) -> None:
        """Type a path into the GNOME/GTK file chooser: Ctrl+L, path, Enter."""
        # Re-target the next input at the dialog window, not the app window.
        self._cua._pid = int(dialog["pid"])
        self._cua._window_id = int(dialog["window_id"])
        self._run(self._cua._call("bring_to_front", {
            "pid": self._cua._pid, "window_id": self._cua._window_id,
        }))
        time.sleep(1.0)
        self._run(self._cua.key("ctrl+l", delivery_mode="foreground"))
        time.sleep(0.5)
        self._run(self._cua.type_text(file_path, delivery_mode="foreground"))
        time.sleep(0.5)
        self._run(self._cua.key("return", delivery_mode="foreground"))

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
        if not fragments and sys.platform.startswith("linux"):
            # The Linux accessibility tree does not expose the Electron URL, so
            # the route cannot be verified. Only skip when there is no route
            # info at all; a visible wrong route still fails above.
            logger.warning("route %s unverifiable: no URL exposed in accessibility tree", expected)
            return
        raise RuntimeError(f"Opentrons navigation did not reach #{expected}")
    # ── base PUDA commands ─────────────────────────────────────────────────
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
            dict: {'file_path': str, 'protocol_name': str | None,
                   'imported': bool | None, 'image_b64': str}.
            ``imported`` is True when the protocol's metadata name is visible in
            the refreshed Protocols list, False when it is not, and None when
            the name could not be read from the file.
        """
        logger.info("import_protocol: %s", file_path)
        if (upload_x is None) != (upload_y is None):
            raise ValueError("Supply both upload_x and upload_y, or neither")

        # Navigating via the sidebar also closes a side panel left open by an
        # earlier attempt. (Do not try to detect an open panel from the
        # accessibility tree: it keeps a stale "Upload" node after closing.)
        self.navigate_protocols()
        # Import button (top-right of Protocols tab), matched by exact label.
        self._click_label("Import", "Import button in the top right corner")

        # The side panel slides in; give it time to settle, then wait until its
        # Upload button's frame stops changing. Not fatal when absent:
        # explicit coordinates may be used.
        time.sleep(1.0)
        previous = None
        for _ in range(10):
            uploads = self._exact(self._snapshot_elements(attempts=1), "Upload", {"button"})
            frame = uploads[0].get("frame") if uploads else None
            if frame is not None and frame == previous:
                break
            previous = frame
            time.sleep(0.5)

        # Chromium only opens a native file chooser for a real user click, so an
        # accessibility "invoke" of Upload is silently ignored. Use a foreground
        # pixel click: explicit coordinates when supplied, otherwise pixels
        # derived from the Upload element's frame. Either way the outcome is
        # verified below by waiting for the native dialog.
        explicit = upload_x is not None
        derived = None if explicit else self._upload_pixel()
        pixel = (upload_x, upload_y) if explicit else derived
        if pixel is not None:
            try:
                self.click_at(*pixel)
            except RuntimeError as exc:
                # Reported "rejected" even when delivered; read-back decides.
                if "input rejected" not in str(exc):
                    raise
                logger.warning("Upload click reported rejected; verifying by read-back (%s)", exc)
        else:
            self._click_label("Upload", "Upload", tolerant=True)

        time.sleep(0.5)
        # The app repaints slowly, so give the dialog time to appear.
        dialog = self._wait_file_dialog(timeout=15.0)
        if dialog is None and derived is not None:
            # Derived pixels missed: last resort is the accessibility invoke.
            self._click_label("Upload", "Upload", tolerant=True)
            time.sleep(0.5)
            dialog = self._wait_file_dialog(timeout=5.0)
        if dialog is None:
            if not explicit:
                raise RuntimeError(
                    "Upload did not open the native Open file dialog after invoking the "
                    "Upload control; capture the screen, then retry with screenshot-grounded "
                    "upload_x/upload_y"
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
            dialog = self._wait_file_dialog(timeout=15.0)
            if dialog is None:
                raise RuntimeError("Upload did not open the native Open file dialog")

        if "portal" in str(dialog.get("app_name", "")).casefold():
            # GNOME file chooser: a separate portal-owned X11 window.
            self._type_into_file_dialog(dialog, file_path)
            if self._wait_file_dialog(appear=False, timeout=15.0) is not None:
                raise RuntimeError("Protocol import did not close the Open file dialog")
            time.sleep(2.0)
            return self._import_result(file_path)

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
            # Reported "rejected" even when delivered; the dialog-closed check
            # below is the real verification.
            self._invoke_element(open_button, tolerant=True)
        else:
            # Compatibility fallback for older cua-driver versions. Native
            # Windows dialogs reject background keystrokes, and this exact
            # path was explicitly authorized by the import command caller.
            self._run(self._cua.type_text(file_path, delivery_mode="foreground"))
            self._run(self._cua.key("return", delivery_mode="foreground"))

        # The dialog must close, otherwise the import did not happen.
        time.sleep(2.0)
        if hasattr(self._cua, "_matching_windows"):
            windows = self._run(self._cua._matching_windows(self.target_app))
            if any(str(window.get("title", "")).casefold() == "open" for window in windows):
                raise RuntimeError("Protocol import did not close the Open file dialog")
        return self._import_result(file_path)

    def _element_pixel(self, element: dict) -> tuple[int, int] | None:
        """Screenshot pixel of a horizontally-centred element in a slide-in side panel.

        Side panels (Import a Protocol, Choose Robot) slide in from the right,
        but the accessibility tree reports them at their pre-animation
        position: translated right by exactly the panel's own width, so frames
        lie outside the window (and cua-driver refuses to click them). An
        element centred in the panel therefore mirrors about the window's
        right edge to its real position (y is unaffected). Frames already
        inside the window are used as they are. Returns None when the
        geometry is unavailable or implausible.

        Uses the geometry of the most recent capture, so ``element`` must come
        from that same snapshot.
        """
        meta = getattr(self._cua, "_capture_meta", None) or {}
        bounds = meta.get("window_bounds") or {}
        width, height = meta.get("screenshot_width"), meta.get("screenshot_height")
        frame = element.get("frame")
        if not (frame and bounds and width and height):
            return None
        x = frame["x"] + frame["w"] / 2 - bounds["x"]
        y = frame["y"] + frame["h"] / 2 - bounds["y"]
        if x >= width:
            x = 2 * width - x
        if not (0 <= x < width and 0 <= y < height):
            return None
        return int(x), int(y)

    def _click_panel_element(self, element: dict) -> None:
        """Click a control that may live in a slide-in side panel.

        If its AX frame lies outside the window (the slide-in offset described
        in ``_element_pixel``) a plain invoke is refused by cua-driver, so click
        the real pixel instead; otherwise invoke normally. A cua-driver "input
        rejected" error on the pixel click is logged, not raised: such clicks
        are often delivered anyway, and every caller verifies by read-back.
        """
        meta = getattr(self._cua, "_capture_meta", None) or {}
        bounds = meta.get("window_bounds") or {}
        frame = element.get("frame") or {}
        off_window = bool(
            frame and bounds
            and frame["x"] + frame["w"] / 2 >= bounds["x"] + bounds["width"]
        )
        pixel = self._element_pixel(element) if off_window else None
        if pixel is None:
            self._invoke_element(element)
            return
        try:
            self.click_at(*pixel)
        except RuntimeError as exc:
            if "input rejected" not in str(exc):
                raise
            logger.warning("click on %r reported rejected; verifying by read-back (%s)",
                           element.get("label"), exc)

    def _upload_pixel(self) -> tuple[int, int] | None:
        """Screenshot pixel of the Import panel's Upload button (see ``_element_pixel``)."""
        uploads = self._exact(getattr(self._cua, "_last_elements", []) or [], "Upload", {"button"})
        return self._element_pixel(uploads[0]) if uploads else None

    @staticmethod
    def _protocol_name(file_path: str) -> str | None:
        """Best-effort read of ``protocolName`` from a protocol file's metadata."""
        try:
            with open(file_path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return None
        match = re.search(r"""["']protocolName["']\s*:\s*["']([^"']+)["']""", text)
        return match.group(1) if match else None

    def _import_result(self, file_path: str) -> dict:
        """Read the Protocols list back and report whether the import is visible."""
        name = self._protocol_name(file_path)
        imported: bool | None = None
        if name:
            wanted = name.casefold()
            elements = self._snapshot_elements()
            imported = any(wanted in str(e.get("label", "")).casefold() for e in elements)
        if imported is False:
            logger.warning("imported protocol %r not visible in the Protocols list", name)
        return {
            "file_path": file_path,
            "protocol_name": name,
            "imported": imported,
            "image_b64": self._screenshot(),
        }

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

        Run start_setup() first so the "Choose Robot" side panel is open.
        Controls are matched by exact label/structure (never fuzzy text):
        the robot card containing ``robot_name`` and the 'Proceed to setup'
        button. If robot_name is empty and no ROBOT_NAME is configured, the
        first listed robot is used.

        This command never clicks through an unexpected confirmation dialog.
        If one appears it is returned in ``dialog`` with
        ``needs_confirmation=True`` and nothing further is clicked, so the
        operator can be asked first.

        Args:
            robot_name: Display name of the robot in the App. Defaults to
                        the ROBOT_NAME set in .env.

        Returns:
            dict: {'robot_name': str, 'selected': bool, 'proceeded': bool,
                   'needs_confirmation': bool, 'dialog': dict | None,
                   'image_b64': str}
        """
        name = robot_name or self.robot_name
        logger.info("select_robot: %r", name)

        time.sleep(0.5)   # let the side panel finish sliding in before reading frames
        elements = self._snapshot_elements()
        dialog = self._pending_dialog(elements)
        if dialog is not None:
            return self._confirmation_needed("select_robot", dialog, robot_name=name,
                                             selected=False, proceeded=False)

        proceed = self._exact(elements, "Proceed to setup", {"button"})
        if not proceed:
            raise RuntimeError(
                "The robot selection panel is not open (no 'Proceed to setup' button). "
                "Run start_setup first."
            )
        card = self._robot_card(elements, name, proceed[0])
        # The card is a Group (no native invoke) inside a slide-in panel whose
        # AX frames are off-window; click its real pixel position.
        self._click_panel_element(card)
        time.sleep(0.5)

        # Re-read after the click: the panel re-renders and indices change.
        elements = self._snapshot_elements()
        dialog = self._pending_dialog(elements)
        if dialog is not None:
            return self._confirmation_needed("select_robot", dialog, robot_name=name,
                                             selected=True, proceeded=False)
        proceed = self._exact(elements, "Proceed to setup", {"button"})
        if not proceed:
            raise RuntimeError(
                "'Proceed to setup' is missing or disabled after selecting the robot; "
                "capture the screen to inspect the panel."
            )
        self._click_panel_element(proceed[0])

        # Verify by read-back that the run screen opened. (The accessibility
        # tree keeps stale nodes of a closed panel, so "Proceed to setup is
        # gone" is not a reliable signal.)
        for _ in range(10):
            time.sleep(0.5)
            elements = self._snapshot_elements()
            dialog = self._pending_dialog(elements)
            if dialog is not None:
                return self._confirmation_needed("select_robot", dialog, robot_name=name,
                                                 selected=True, proceeded=False)
            on_run_page = any(
                "/protocol-runs/" in str(e.get("value", ""))
                for e in elements if str(e.get("role", "")).casefold() == "document"
            ) or bool(self._exact(elements, "Start run", {"button"}))
            if on_run_page:
                return {"robot_name": name, "selected": True, "proceeded": True,
                        "needs_confirmation": False, "dialog": None,
                        "image_b64": self._screenshot()}
        raise RuntimeError(
            "Clicked 'Proceed to setup' but the run screen did not open; "
            "capture the screen and inspect before retrying."
        )

    def _robot_card(self, elements: list[dict], name: str, proceed: dict) -> dict:
        """The invokable card that wraps the robot's name in the Choose Robot panel."""
        texts = [e for e in elements if str(e.get("role", "")).casefold() == "text"]
        groups = [
            e for e in elements
            if str(e.get("role", "")).casefold() == "group"
            and "invoke" in e.get("actions", []) and e.get("frame")
        ]

        def contains(group: dict, text: dict) -> bool:
            g, t = group["frame"], text.get("frame") or {}
            if not t:
                return False
            cx, cy = t["x"] + t["w"] / 2, t["y"] + t["h"] / 2
            return g["x"] <= cx <= g["x"] + g["w"] and g["y"] <= cy <= g["y"] + g["h"]

        def smallest(candidates: list[dict]) -> dict | None:
            return min(candidates, key=lambda g: g["frame"]["w"] * g["frame"]["h"], default=None)

        if name:
            wanted = name.strip().casefold()
            for text in texts:
                if str(text.get("label", "")).strip().casefold() == wanted:
                    card = smallest([g for g in groups if contains(g, text)])
                    if card is not None:
                        return card
            shown = [str(t.get("label", "")) for t in texts if t.get("label")]
            raise RuntimeError(
                f"No robot card named {name!r} in the Choose Robot panel. "
                f"Visible text: {shown[:20]}"
            )

        # No name: first card in the panel — a compact invokable group above
        # 'Proceed to setup' that wraps some text, nearest the panel's top.
        pframe = proceed.get("frame") or {}
        cards = [
            g for g in groups
            if g["frame"]["h"] <= 200 and g["frame"]["w"] >= 150
            and (not pframe or (g["frame"]["y"] < pframe["y"]
                                and abs(g["frame"]["x"] - pframe["x"]) <= 40))
            and any(contains(g, t) for t in texts)
        ]
        if not cards:
            raise RuntimeError("No robot card found in the Choose Robot panel")
        return min(cards, key=lambda g: g["frame"]["y"])

    def _confirmation_needed(self, command_name: str, dialog: dict, **fields: Any) -> dict:
        """Report a dialog to the caller without clicking it."""
        self._reported_dialog = (command_name, tuple(dialog.get("text", [])),
                                 tuple(dialog.get("buttons", [])))
        logger.warning("%s: confirmation dialog pending, awaiting the user: %s",
                       command_name, dialog)
        return {**fields, "needs_confirmation": True,
                "dialog": dialog, "image_b64": self._screenshot()}
    @command
    def start_run(self, confirm_dialog: bool = False) -> dict:
        """
        Click 'Start run' on the Run screen to begin executing the protocol.

        Call this after start_setup() → select_robot(). The page's own
        'Start run' button is clicked once, by exact label.

        If the App then asks for confirmation (e.g. "Are you sure you want to
        proceed to run? You haven't confirmed the labware and liquid placement"),
        NOTHING further is clicked: the dialog text and buttons are returned
        with ``needs_confirmation=True`` so the operator can be asked first.
        Only after the operator approves, call start_run(confirm_dialog=True)
        to click the dialog's 'Start run' button. confirm_dialog is ignored
        unless this driver has already reported that dialog to the caller.

        Args:
            confirm_dialog: True only after the user has approved the reported
                            dialog.

        Returns:
            dict: {'started': bool, 'run_status': str,
                   'needs_confirmation': bool, 'dialog': dict | None,
                   'image_b64': str}
        """
        logger.info("start_run (confirm_dialog=%s)", confirm_dialog)
        elements = self._snapshot_elements()
        dialog = self._pending_dialog(elements)

        if dialog is None:
            starts = self._exact(elements, "Start run", {"button"})
            if not starts:
                raise RuntimeError(
                    "No 'Start run' button on screen. Open the run with "
                    "start_setup and select_robot first."
                )
            self._invoke_element(starts[0])
            time.sleep(1.5)
            elements = self._snapshot_elements()
            dialog = self._pending_dialog(elements)

        if dialog is not None:
            reported = getattr(self, "_reported_dialog", None)
            current = ("start_run", tuple(dialog.get("text", [])),
                       tuple(dialog.get("buttons", [])))
            if not (confirm_dialog and reported == current):
                # Never click an unreviewed dialog: hand it back to the user.
                return self._confirmation_needed(
                    "start_run", dialog, started=False,
                    run_status=self._run_status(elements),
                )
            button = self._dialog_confirm_button(elements, "Start run")
            if button is None:
                raise RuntimeError(
                    f"The dialog has no 'Start run' button to confirm: {dialog}"
                )
            logger.info("start_run: user-approved dialog confirmed")
            self._reported_dialog = None
            self._invoke_element(button)
            time.sleep(1.5)
            elements = self._snapshot_elements()
            dialog = self._pending_dialog(elements)
            if dialog is not None and self._run_status(elements) not in (
                    "running", "paused", "finishing", "completed"):
                # Another (or the same, unclicked) confirmation: ask again,
                # never chain-click. A dialog node that lingers in the tree
                # while the run is already underway is ignored.
                return self._confirmation_needed(
                    "start_run", dialog, started=False,
                    run_status=self._run_status(elements),
                )

        # Give the App a moment to reflect the new state, then read it back.
        status = self._run_status(elements)
        for _ in range(6):
            if status not in ("not started", "unverified"):
                break
            time.sleep(1.0)
            elements = self._snapshot_elements()
            status = self._run_status(elements)
        return {
            "started": status in ("running", "paused", "finishing", "completed"),
            "run_status": status,
            "needs_confirmation": False,
            "dialog": None,
            "image_b64": self._screenshot(),
        }

    @staticmethod
    def _run_status(elements: list[dict]) -> str:
        """Run status as displayed by the App, or 'unverified'."""
        labels = {str(e.get("label", "")).strip().casefold() for e in elements}
        return next(
            (s for s in ("running", "paused", "finishing", "completed", "failed",
                         "stopped", "not started") if s in labels),
            "unverified",
        )
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
