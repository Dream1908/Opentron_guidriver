"""PUDA GUI driver — controls any desktop instrument software via Hermes computer use."""

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
# cua-driver MCP client
# ─────────────────────────────────────────────────────────────────────────────

class CuaDriverClient:
    """
    Async MCP client for cua-driver.

    cua-driver exposes computer-use actions as MCP tools. This client starts the
    driver as a stdio subprocess, lists its tools at startup, then wraps the
    most important ones (capture, click, type, key, scroll) behind a stable
    async API regardless of how cua-driver names them in any given release.

    Tool-name resolution order (first match wins):
      capture : computer_use → screenshot → capture_screen → get_window_state
      click   : computer_use → click → left_click
      type    : computer_use → type → type_text
      key     : computer_use → key → press_key → hotkey
      scroll  : computer_use → scroll → scroll_element
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
        """Start cua-driver MCP subprocess and initialise the session."""
        params = StdioServerParameters(command="cua-driver", args=["mcp"])
        self._ctx = stdio_client(params)
        read, write = await self._ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        tools_result = await self._session.list_tools()
        self._tools = {t.name: t for t in tools_result.tools}
        logger.info("cua-driver connected. Available tools: %s", sorted(self._tools.keys()))

    async def stop(self) -> None:
        """Gracefully close the MCP session and cua-driver subprocess."""
        if self._session:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
        if self._ctx:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass

    # ── tool resolution helpers ────────────────────────────────────────────

    def _resolve(self, candidates: list[str]) -> str:
        """Return the first candidate tool name present in the driver."""
        for name in candidates:
            if name in self._tools:
                return name
        raise RuntimeError(
            f"None of the expected tools {candidates} found in cua-driver. "
            f"Available: {sorted(self._tools.keys())}"
        )

    async def _call(self, tool: str, args: dict) -> Any:
        assert self._session, "CuaDriverClient not started"
        return await self._session.call_tool(tool, args)

    # ── computer-use actions ───────────────────────────────────────────────

    async def capture(self, app: str | None = None, mode: str = "screenshot") -> Any:
        """
        Capture the screen or a specific app window.

        Args:
            app:  Application/window name to target. None = full screen.
            mode: 'screenshot' for plain image, 'som' for Set-of-Marks numbered overlay,
                  'ax' for accessibility-tree-only (no image, text-only models).
        """
        tool = self._resolve(self._CAPTURE_TOOLS)
        if tool == "computer_use":
            args: dict = {"action": "capture", "mode": mode}
            if app:
                args["app"] = app
        else:
            # Native cua-driver tool — best-effort arg mapping
            args = {}
            if app:
                args["app"] = app
            if tool == "get_window_state":
                # get_window_state returns the AX/UIA tree; no mode param
                args.pop("mode", None)
        return await self._call(tool, args)

    async def click(
        self,
        element: int | None = None,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
    ) -> Any:
        """Click a SOM-numbered element or absolute coordinates."""
        tool = self._resolve(self._CLICK_TOOLS)
        if tool == "computer_use":
            args: dict = {"action": "click", "button": button}
            if element is not None:
                args["element"] = element
            elif x is not None and y is not None:
                args["x"] = x
                args["y"] = y
        else:
            args = {"button": button}
            if element is not None:
                args["element"] = element
            elif x is not None and y is not None:
                args["x"] = x
                args["y"] = y
        return await self._call(tool, args)

    async def type_text(self, text: str) -> Any:
        """Type text into the currently focused element."""
        tool = self._resolve(self._TYPE_TOOLS)
        if tool == "computer_use":
            args = {"action": "type", "text": text}
        else:
            args = {"text": text}
        return await self._call(tool, args)

    async def key(self, keys: str, capture_after: bool = False) -> Any:
        """Press a key or key combination (e.g. 'return', 'ctrl+s', 'escape')."""
        tool = self._resolve(self._KEY_TOOLS)
        if tool == "computer_use":
            args = {"action": "key", "keys": keys, "capture_after": capture_after}
        else:
            args = {"keys": keys}
        return await self._call(tool, args)

    async def scroll(self, direction: str = "down", amount: int = 3) -> Any:
        """Scroll in the focused element."""
        tool = self._resolve(self._SCROLL_TOOLS)
        if tool == "computer_use":
            args = {"action": "scroll", "direction": direction, "amount": amount}
        else:
            args = {"direction": direction, "amount": amount}
        return await self._call(tool, args)

    # ── image extraction ───────────────────────────────────────────────────

    @staticmethod
    def extract_image_b64(result: Any) -> str | None:
        """
        Extract a base64-encoded PNG string from an MCP tool result.

        cua-driver embeds images in tool-result content blocks with either a
        'data' field (base64) or a 'url' field. We try both.
        """
        if result is None:
            return None
        content = getattr(result, "content", None)
        if content is None:
            return None
        for block in content:
            btype = getattr(block, "type", None)
            # MCP image content block
            if btype == "image":
                return getattr(block, "data", None)
            # Some drivers use mimeType on a generic blob block
            mime = getattr(block, "mimeType", None)
            if mime and "image" in mime:
                return getattr(block, "data", None)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# PUDA GUI driver
# ─────────────────────────────────────────────────────────────────────────────

class GuiDriver:
    """
    PUDA GUI driver — integrates any desktop instrument software via computer use.

    Replaces SDK calls with:
      • Screenshot capture  (cua-driver → background, no focus steal)
      • Vision LLM analysis (Anthropic Claude — reads instrument state from image)
      • GUI event dispatch  (cua-driver → synthesised mouse / keyboard events)
    """

    _STATUS_PROMPT = (
        "You are reading a screenshot of instrument control software. "
        "Extract every piece of status information visible on screen and return it "
        "as a single JSON object. Include all numeric readings, units, modes, states, "
        "alarms, and error messages you can see. Use snake_case keys."
    )

    # Vision model — uses Hermes's configured provider via ANTHROPIC_API_KEY in the environment.
    _LLM_MODEL = "claude-sonnet-4-5"

    def __init__(
        self,
        target_app: str,
        capture_interval: float = 30.0,
    ) -> None:
        self.target_app = target_app
        self.capture_interval = capture_interval

        self._last_status: dict = {}
        # Reads ANTHROPIC_API_KEY from the environment automatically —
        # no need to set it in .env; Hermes manages the credential.
        self._anthropic = anthropic.Anthropic()
        self._cua = CuaDriverClient()

        # Background asyncio event loop — bridges sync PUDA worker threads
        # to async MCP calls without blocking or spawning per-call loops.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="guidriver-async-loop",
        )
        self._loop_thread.start()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def startup(self) -> None:
        """
        Start the cua-driver MCP session.
        Called once from main.py before the EdgeRunner loop.
        """
        self._run(self._cua.start())
        logger.info(
            "GuiDriver ready — target_app=%r  model=%s", self.target_app, self.llm_model
        )

    def _run(self, coro) -> Any:
        """Submit an async coroutine to the background loop and block until done."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=60)

    # ── vision helpers ─────────────────────────────────────────────────────

    def _vision_query(self, img_b64: str, prompt: str) -> str:
        """Send a base64 PNG screenshot to the vision LLM and return the text reply."""
        msg = self._anthropic.messages.create(
            model=self._LLM_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": img_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        return msg.content[0].text

    def _parse_json_from_llm(self, text: str) -> dict:
        """Extract the first JSON object from an LLM reply. Falls back to {'raw': text}."""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"raw": text}

    def _screenshot(self, mode: str = "screenshot") -> str:
        """Capture and return a base64 PNG of the target app. Raises if capture fails."""
        result = self._run(self._cua.capture(app=self.target_app, mode=mode))
        img_b64 = CuaDriverClient.extract_image_b64(result)
        if img_b64 is None:
            raise RuntimeError(
                f"cua-driver returned no image for app={self.target_app!r}. "
                "Check that the app is visible and cua-driver has Screen Recording permission."
            )
        return img_b64

    # ── PUDA machine_state ─────────────────────────────────────────────────

    @machine_state
    def snapshot(self) -> dict:
        """
        Cached state merged into every MACHINE_STATE update.
        Values are set by the last successful status / telemetry call.
        Do NOT read from the screen here — this runs on the event loop.
        """
        return {
            "target_app": self.target_app,
            "last_status": self._last_status,
        }

    # ── PUDA commands ──────────────────────────────────────────────────────

    @command
    def status(self) -> dict:
        """
        Capture a screenshot of the instrument software and extract its current
        status using vision LLM analysis.

        The LLM reads all visible readings, modes, alarms, and values from the
        screenshot and returns them as a JSON object with snake_case keys.

        Returns:
            dict: All status fields visible on the instrument software screen.
        """
        logger.info("status: capturing %r for vision analysis", self.target_app)
        img_b64 = self._screenshot(mode="screenshot")
        response = self._vision_query(img_b64, self._STATUS_PROMPT)
        self._last_status = self._parse_json_from_llm(response)
        logger.info("status result: %s", self._last_status)
        return self._last_status

    @command
    def capture_screenshot(self) -> dict:
        """
        Capture a raw screenshot of the target application.

        Returns:
            dict: {'app': str, 'image_b64': str}  — base64-encoded PNG.
        """
        logger.info("capture_screenshot: %r", self.target_app)
        img_b64 = self._screenshot(mode="screenshot")
        return {"app": self.target_app, "image_b64": img_b64}

    @command
    def find_and_click(self, description: str) -> dict:
        """
        Locate a UI element by natural-language description using SOM vision, then
        click it.

        The driver captures a Set-of-Marks screenshot (each visible element is
        numbered), sends it to the vision LLM with the description, receives the
        element number, and dispatches a click — all without bringing the window
        to the foreground.

        Args:
            description: Plain-English description of the element to click, e.g.
                         "the Start button", "flow rate setpoint field",
                         "the Run/Stop toggle in the toolbar".

        Returns:
            dict: {'clicked_element': int, 'description': str}
        """
        logger.info("find_and_click: looking for %r in %r", description, self.target_app)
        img_b64 = self._screenshot(mode="som")
        prompt = (
            f"This screenshot uses SOM (Set of Marks) numbering — each interactive "
            f"UI element has an integer label.\n"
            f"Which element number best matches: '{description}'?\n"
            f"Reply with ONLY the integer element number, nothing else."
        )
        response = self._vision_query(img_b64, prompt).strip()
        m = re.search(r"\d+", response)
        if not m:
            raise RuntimeError(
                f"LLM could not identify an element for {description!r}. "
                f"LLM reply: {response!r}"
            )
        element_num = int(m.group())
        logger.info("find_and_click: clicking element %d", element_num)
        self._run(self._cua.click(element=element_num))
        return {"clicked_element": element_num, "description": description}

    @command
    def type_text(self, text: str, field_description: str = "") -> dict:
        """
        Type text into a UI field.

        If field_description is provided, the driver first calls find_and_click
        to focus the correct field before typing.

        Args:
            text:              Text to type.
            field_description: Optional — click this field before typing, e.g.
                               "the flow rate input box". Leave empty to type into
                               whatever is currently focused.

        Returns:
            dict: {'typed': str, 'field': str}
        """
        if field_description:
            self.find_and_click(field_description)
        logger.info("type_text: typing %r into %r", text, field_description or "<focused>")
        self._run(self._cua.type_text(text))
        return {"typed": text, "field": field_description}

    @command
    def press_key(self, keys: str) -> dict:
        """
        Press a keyboard shortcut or key in the target application.

        Args:
            keys: Key name or combination, e.g. 'return', 'escape', 'ctrl+s',
                  'f5', 'ctrl+shift+r', 'alt+f4'.

        Returns:
            dict: {'keys': str}
        """
        logger.info("press_key: %s", keys)
        self._run(self._cua.key(keys))
        return {"keys": keys}

    @command
    def run_gui_step(self, instruction: str) -> dict:
        """
        Execute one natural-language GUI instruction autonomously.

        The driver captures a SOM screenshot, asks the vision LLM to plan the
        single next GUI action (click / type / key) needed to fulfil the
        instruction, then executes it.

        Use this for ad-hoc control of the instrument. For repeatable sequences,
        build a PUDA protocol that chains multiple run_gui_step calls or
        more specific commands.

        Args:
            instruction: Natural-language description of what to do, e.g.
                         "Set the temperature setpoint to 37 °C",
                         "Click the Stop button",
                         "Open the File menu and choose Export Data".

        Returns:
            dict: {'instruction': str, 'action_taken': dict, 'result': dict}
        """
        logger.info("run_gui_step: %r", instruction)
        img_b64 = self._screenshot(mode="som")
        plan_prompt = (
            f"You are a GUI automation agent controlling '{self.target_app}'.\n"
            f"The screenshot shows the current UI with SOM-numbered interactive elements.\n\n"
            f"Instruction: {instruction}\n\n"
            f"Respond with ONLY a JSON object for the single best next action:\n"
            f'  {{"action": "click", "element": <N>}}\n'
            f'  {{"action": "type",  "text":    "<string>"}}\n'
            f'  {{"action": "key",   "keys":    "<keys>"}}\n'
            f"Choose exactly one action. Do not include any explanation."
        )
        plan_text = self._vision_query(img_b64, plan_prompt)
        match = re.search(r"\{.*\}", plan_text, re.DOTALL)
        if not match:
            raise RuntimeError(
                f"LLM did not return a valid action JSON. Reply: {plan_text!r}"
            )
        action = json.loads(match.group())
        action_type = action.get("action", "")
        result: dict = {}

        if action_type == "click":
            elem = action.get("element")
            self._run(self._cua.click(element=elem))
            result = {"clicked_element": elem}
        elif action_type == "type":
            text = action.get("text", "")
            self._run(self._cua.type_text(text))
            result = {"typed": text}
        elif action_type == "key":
            keys = action.get("keys", "")
            self._run(self._cua.key(keys))
            result = {"keys": keys}
        else:
            raise RuntimeError(
                f"Unknown action type from LLM: {action_type!r}. Full action: {action}"
            )

        logger.info("run_gui_step done: %s → %s", instruction, action)
        return {"instruction": instruction, "action_taken": action, "result": result}

    @command
    def ask_screen(self, question: str) -> dict:
        """
        Ask the vision LLM an arbitrary question about the current state of the
        instrument software screen. Useful for read-only inspection without
        triggering any actions.

        Args:
            question: Free-form question about the screen, e.g.
                      "What is the current pump flow rate?",
                      "Is there any active alarm or warning?",
                      "What measurement is currently selected?".

        Returns:
            dict: {'question': str, 'answer': str, 'parsed': dict | None}
        """
        logger.info("ask_screen: %r", question)
        img_b64 = self._screenshot(mode="screenshot")
        answer = self._vision_query(img_b64, question)
        parsed = self._parse_json_from_llm(answer) if answer.strip().startswith("{") else None
        return {"question": question, "answer": answer, "parsed": parsed}

    @command
    def home(self) -> bool:
        """
        Navigate the target application to its main / home screen.

        Captures the current screen, asks the LLM whether the app is already
        on the home screen; if not, clicks the element the LLM identifies as
        the path back to home (e.g. Home button, back arrow, main tab).

        Returns:
            bool: True when the home sequence completes.
        """
        logger.info("home: navigating %r to home screen", self.target_app)
        img_b64 = self._screenshot(mode="som")
        home_prompt = (
            f"Is the application '{self.target_app}' currently showing its main/home screen? "
            f"If yes, reply with exactly the word HOME. "
            f"If not, reply with only the integer SOM element number to click to navigate home "
            f"(e.g. a Home button, main tab, or back arrow)."
        )
        response = self._vision_query(img_b64, home_prompt).strip()
        if "HOME" in response.upper():
            logger.info("home: already at home screen")
        else:
            m = re.search(r"\d+", response)
            if m:
                elem = int(m.group())
                logger.info("home: clicking element %d to navigate home", elem)
                self._run(self._cua.click(element=elem))
            else:
                logger.warning("home: LLM gave unexpected response: %r", response)
        return True

    @command
    def reset(self) -> bool:
        """
        Software reset — press Escape to cancel any in-progress operation and
        return the instrument software to an idle/ready state.

        Returns:
            bool: True if the reset key was dispatched successfully.
        """
        logger.info("reset: sending Escape to %r", self.target_app)
        self._run(self._cua.key("escape"))
        return True

    @command
    def shutdown(self) -> bool:
        """
        Shut down the GUI driver. Closes the cua-driver MCP session cleanly.

        Returns:
            bool: True if shutdown was successful.
        """
        logger.info("shutdown: closing cua-driver MCP session")
        try:
            self._run(self._cua.stop())
        except Exception as e:
            logger.warning("shutdown: error closing cua-driver: %s", e)
        self._loop.call_soon_threadsafe(self._loop.stop)
        return True

    # ── PUDA telemetry streams ─────────────────────────────────────────────

    @tlm_stream(interval=30.0, name="instrument_status")
    def stream_status(self) -> dict | None:
        """
        Periodic vision-based status poll.

        Captures a screenshot of the instrument software every CAPTURE_INTERVAL
        seconds and extracts the current status via the vision LLM. Published to:
            puda.<machine_id>.tlm.stream.instrument_status

        Returns None to skip a sample (keeps the stream alive but publishes nothing).
        """
        try:
            img_b64 = self._screenshot(mode="screenshot")
            response = self._vision_query(img_b64, self._STATUS_PROMPT)
            self._last_status = self._parse_json_from_llm(response)
            return self._last_status
        except Exception as e:
            logger.warning("stream_status: failed — %s", e)
            return None
