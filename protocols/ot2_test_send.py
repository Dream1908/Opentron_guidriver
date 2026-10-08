"""PUDA-only sender for ot2-test; save exact responses and screenshots."""
import asyncio, base64, json, sys, uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from main import load_config
from puda.command_service import CommandService
from puda.models import CommandRequest
STATE=Path(__file__).with_name('ot2_test_session.json')
async def main():
    action=sys.argv[1]
    state=json.loads(STATE.read_text()) if STATE.exists() else {}
    if action=='start':
        if state.get('active'): raise RuntimeError('Session already active')
        state={'run_id':str(uuid.uuid4()),'step':0,'active':False}
        STATE.write_text(json.dumps(state))
    if not state: raise RuntimeError('No session')
    service=CommandService(load_config().nats_server_list)
    await service.connect()
    try:
        identity={'run_id':state['run_id'],'user_id':'','username':'Hermes Agent'}
        if action=='start': response=await service.start_run(machine_id='ot2-test',**identity)
        elif action=='complete': response=await service.complete_run(machine_id='ot2-test',**identity)
        else:
            state['step']+=1
            STATE.write_text(json.dumps(state))
            response=await service.send_queue_command(request=CommandRequest(name=action,machine_id='ot2-test',params=json.loads(sys.argv[2]) if len(sys.argv)>2 else {},kwargs={},step_number=state['step']),timeout=90,**identity)
        if response is None: raise RuntimeError('No response; never blindly retry motion')
        data=response.model_dump(mode='json')
        out=Path(__file__).with_name(f"ot2_test_{state['step']:03d}_{action}.json")
        out.write_text(json.dumps(data),encoding='utf-8')
        def clean(v):
            if isinstance(v,dict):
                r={}
                for k,x in v.items():
                    if k=='image_b64' and isinstance(x,str):
                        image=out.with_suffix('.png'); image.write_bytes(base64.b64decode(x)); r[k]=str(image)
                    elif k=='elements' and isinstance(x,list): r[k]=f'{len(x)} elements saved in response'
                    else: r[k]=clean(x)
                return r
            if isinstance(v,list): return [clean(x) for x in v]
            return v
        print(json.dumps(clean(data),indent=2))
        if str(data.get('response',{}).get('status','')).lower() not in ('success','completed','ok'): raise RuntimeError('PUDA failure; inspect response')
        if action in ('start','complete'): state['active']=action=='start'
        STATE.write_text(json.dumps(state))
    finally: await service.disconnect()
asyncio.run(main())
