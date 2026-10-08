"""Send verified PUDA SDK commands with unique step numbers; no direct GUI input."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main import load_config
from puda.command_service import CommandService
from puda.models import CommandRequest

STATE = Path(__file__).with_name("a3_sdk_session.json")

async def main():
    action = sys.argv[1]
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if action == "start":
        if state.get("active"):
            raise RuntimeError("Session already active; do not start another")
        state = {"run_id": str(uuid.uuid4()), "step": 0, "active": False}
        STATE.write_text(json.dumps(state))
    if not state:
        raise RuntimeError("No SDK session")
    service = CommandService(load_config().nats_server_list)
    await service.connect()
    try:
        identity = {"run_id": state["run_id"], "user_id": "", "username": "Hermes Agent"}
        if action == "start":
            response = await service.start_run(machine_id="ot2-1", **identity)
        elif action == "complete":
            response = await service.complete_run(machine_id="ot2-1", **identity)
        else:
            state["step"] += 1
            STATE.write_text(json.dumps(state))
            params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
            response = await service.send_queue_command(
                request=CommandRequest(name=action, machine_id="ot2-1", params=params, kwargs={}, step_number=state["step"]),
                timeout=90, **identity,
            )
        if response is None:
            raise RuntimeError("No PUDA response; do not blindly retry motion")
        data = response.model_dump(mode="json")
        out = Path(__file__).with_name(f"a3_sdk_{state['step']:03d}_{action}.json")
        out.write_text(json.dumps(data), encoding="utf-8")
        images = []
        def clean(value):
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if key == "image_b64" and isinstance(item, str):
                        image = out.with_suffix('.png')
                        image.write_bytes(base64.b64decode(item))
                        images.append(str(image))
                        result[key] = f"[saved {len(item)} base64 characters]"
                    else:
                        result[key] = clean(item)
                return result
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value
        print(json.dumps(clean(data), indent=2, ensure_ascii=True))
        print('RESPONSE_FILE:',out)
        print('SCREENSHOTS:', images)
        if str(data.get('response', {}).get('status', '')).lower() not in ['success', 'completed', 'ok']:
            raise RuntimeError('PUDA command did not report success; inspect saved response')
        if action in ['start', 'complete']:
            state['active'] = action == 'start'
        STATE.write_text(json.dumps(state))
    finally:
        await service.disconnect()

asyncio.run(main())
