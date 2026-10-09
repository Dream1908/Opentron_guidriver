"""Regression tests; transport is replaced to prevent robot/GUI motion."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driver import CuaDriverClient, GuiDriver, OpentronGuiDriver

class DriverInputTests(unittest.TestCase):
    def test_status_navigates_to_devices_and_returns_current_image(self):
        driver = object.__new__(OpentronGuiDriver)
        driver.target_app = 'Opentrons'
        driver._last_status = {}
        events = []
        driver.navigate_devices = lambda: events.append('navigate_devices') or True
        driver._screenshot = lambda: events.append('screenshot') or 'current-image'

        result = driver.status()

        self.assertEqual(events, ['navigate_devices', 'screenshot'])
        self.assertEqual(result, {
            'target_app': 'Opentrons',
            'image_b64': 'current-image',
        })
        self.assertIs(result, driver._last_status)

    def test_navigation_works_without_background_escape(self):
        for route in ('protocols', 'devices'):
            with self.subTest(route=route):
                driver = object.__new__(OpentronGuiDriver)
                events = []
                async def rejected_key(keys):
                    self.fail('Navigation must not send the rejected background Escape')
                driver._cua = SimpleNamespace(key=rejected_key)
                driver._run = asyncio.run
                driver._som_click = lambda description: events.append(('click', description))
                driver._verify_route = lambda target: events.append(('verified', target))
                self.assertTrue(getattr(driver, 'navigate_' + route)())
                self.assertEqual(events, [('click', route.capitalize() + ' in the left sidebar'),
                                          ('verified', route)])

    def test_route_verification_rejects_nested_protocol_detail(self):
        driver = object.__new__(GuiDriver)
        driver._cua = SimpleNamespace(_last_elements=[])
        captures = iter([
            'file:///app/index.html#/protocols/detail-id',
            'file:///app/index.html#/protocols',
        ])

        def screenshot(**kwargs):
            current = next(captures)
            driver._cua._last_elements = [
                {'role': 'Link', 'value': 'file:///app/index.html#/protocols'},
                {'role': 'Document', 'value': current},
            ]
            return 'image'

        driver._screenshot = screenshot
        with patch('driver.time.sleep'):
            driver._verify_route('protocols')

    def test_start_run_does_not_claim_motion_when_confirmation_is_pending(self):
        driver = object.__new__(OpentronGuiDriver)
        driver._som_click = lambda description: 1
        driver._cua = SimpleNamespace(_last_elements=[{'label':'Not started','role':'Text'}])
        driver._screenshot = lambda **kwargs: 'image'
        with patch('driver.time.sleep'):
            result = driver.start_run()
        self.assertFalse(result['started'])
        self.assertEqual(result['run_status'],'not started')
        self.assertEqual(result['image_b64'],'image')

    def test_import_accepts_grounded_upload_pixels_and_refreshes_dialog(self):
        import inspect
        self.assertIn('upload_x',inspect.signature(OpentronGuiDriver.import_protocol).parameters,
                      'Import lacks a screenshot-grounded Upload fallback')
        driver = object.__new__(OpentronGuiDriver)
        driver.target_app = 'Opentrons OT-2'
        events = []
        driver.navigate_protocols = lambda: events.append('navigate')
        driver._som_click = lambda description: events.append(description)
        driver.click_at = lambda x,y: events.append(('pixel',x,y))
        driver._screenshot = lambda **kwargs: events.append('capture') or 'image'
        async def type_text(text, **kwargs): events.append(('type',text,kwargs))
        async def key(keys, **kwargs): events.append(('key',keys,kwargs))
        window_states = iter([
            [{'title': 'Open'}],
            [{'title': 'Opentrons OT-2'}],
        ])
        async def matching_windows(app): return next(window_states)
        driver._cua = SimpleNamespace(
            type_text=type_text, key=key, _last_elements=[], _tools={},
            _matching_windows=matching_windows,
        )
        driver._run = asyncio.run
        with patch('driver.time.sleep'):
            driver.import_protocol(str(Path(__file__).resolve()), upload_x=764, upload_y=309)
        self.assertIn(('pixel',764,309),events)
        self.assertNotIn('Choose File button in the import sidebar',events)
        typed = ('type', str(Path(__file__).resolve()), {'delivery_mode': 'foreground'})
        self.assertLess(events.index('capture'), events.index(typed))
        self.assertIn(('key', 'return', {'delivery_mode': 'foreground'}), events)

    def test_file_dialog_is_selected_instead_of_richer_main_window(self):
        client = CuaDriverClient()
        client._tools = {
            'get_window_state': object(), 'get_screen_size': object(),
            'set_window_frame': object(), 'bring_to_front': object(),
        }
        async def windows(app):
            visible = {'bounds': {'x': 0, 'y': 0, 'width': 900, 'height': 700},
                       'is_on_screen': True, 'minimized': False}
            return [dict(visible, pid=1,window_id=10,title='Opentrons OT-2'),
                    dict(visible, pid=1,window_id=20,title='Open')]
        async def call(tool,args):
            if tool == 'get_screen_size':
                return SimpleNamespace(structured_content={'width':1920,'height':1080})
            if tool == 'bring_to_front':
                return SimpleNamespace(structured_content={'now_fg_hwnd':args['window_id']})
            if tool == 'list_windows':
                return SimpleNamespace(structured_content={'windows':await windows('Opentrons')})
            is_main = args['window_id']==10
            return SimpleNamespace(structured_content={'total_element_count':100 if is_main else 10,
                                                        'elements':[],'snapshot_id':'s00000001'}, content=[])
        client._matching_windows, client._call = windows, call
        asyncio.run(client.capture('Opentrons OT-2'))
        self.assertEqual(client._window_id,20)

    def test_pixel_command_rejects_coordinates_outside_screenshot(self):
        driver = object.__new__(GuiDriver)
        driver._cua = SimpleNamespace(_capture_meta={'screenshot_width':958,'screenshot_height':1138})
        driver._screenshot = lambda **kwargs: 'before'
        driver._run = lambda coro: self.fail('Out-of-bounds input must not be dispatched')
        async def click(**kwargs):
            self.fail('Out-of-bounds input must not be dispatched')
        driver._cua.click = click
        with self.assertRaises(ValueError):
            driver.click_at(1200,309)

    def test_puda_pixel_command_returns_delivery_effect_and_readback(self):
        driver = object.__new__(GuiDriver)
        self.assertTrue(callable(getattr(driver,'click_at',None)), 'PUDA screenshot-coordinate command is missing')
        calls = []
        async def click(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(structured_content={'effect':'unverifiable'}, is_error=False)
        driver._cua = SimpleNamespace(click=click, _capture_meta={'screenshot_width':958,'screenshot_height':1138})
        images = iter(['before','after'])
        driver._screenshot = lambda **kwargs: next(images)
        driver._run = asyncio.run
        result = driver.click_at(764,309)
        self.assertEqual(calls,[{'x':764,'y':309,'delivery_mode':'foreground'}])
        self.assertEqual(result['effect'],'unverifiable')
        self.assertEqual(result['image_b64'],'after')

    def test_failed_cua_input_is_not_reported_as_success(self):
        client = CuaDriverClient()
        client._tools = {'click':object()}
        async def call(tool, args):
            return SimpleNamespace(structured_content={'ok':False,'code':'background_unavailable'}, is_error=True, content=[])
        client._call = call
        with self.assertRaisesRegex(RuntimeError, 'background_unavailable'):
            asyncio.run(client.click(x=1,y=1))

    def test_pixel_click_uses_the_captured_window(self):
        client = CuaDriverClient()
        client._tools = {'click': object()}
        client._pid, client._window_id = 28460, 2820286
        calls = []
        async def call(tool, args):
            calls.append((tool, args))
            return SimpleNamespace(structured_content={'effect':'unverifiable'}, is_error=False)
        client._call = call
        asyncio.run(client.click(x=764, y=309))
        self.assertEqual(calls[0][1].get('window_id'), 2820286)
        self.assertEqual(calls[0][1].get('delivery_mode'), 'foreground')
        self.assertEqual((calls[0][1]['x'], calls[0][1]['y']), (764,309))

    def test_offscreen_window_is_restored_and_foregrounded_before_capture(self):
        client = CuaDriverClient()
        client._tools = {
            'get_window_state': object(), 'get_screen_size': object(),
            'set_window_frame': object(), 'bring_to_front': object(),
        }
        original = {
            'pid': 42, 'window_id': 99, 'title': 'Opentrons OT-2',
            'bounds': {'x': -32000, 'y': -32000, 'width': 144, 'height': 28},
            'is_on_screen': False, 'minimized': True,
        }
        restored = dict(original,
            bounds={'x': 0, 'y': 0, 'width': 1000, 'height': 700},
            is_on_screen=True, minimized=False)
        calls = []
        async def windows(app): return [original]
        async def call(tool, args):
            calls.append((tool, args))
            if tool == 'get_screen_size':
                return SimpleNamespace(structured_content={'width':1920,'height':1200})
            if tool == 'list_windows':
                return SimpleNamespace(structured_content={'windows':[restored]})
            if tool == 'get_window_state':
                return SimpleNamespace(structured_content={
                    'total_element_count':1, 'elements':[], 'snapshot_id':'fresh'},
                    content=[])
            return SimpleNamespace(structured_content={'ok':True})
        client._matching_windows, client._call = windows, call

        asyncio.run(client.capture('Opentrons'))

        names = [name for name, _ in calls]
        first_front = names.index('bring_to_front')
        frame_index = names.index('set_window_frame')
        second_front = names.index('bring_to_front', first_front + 1)
        self.assertLess(first_front, frame_index)
        self.assertLess(frame_index, second_front)
        self.assertLess(second_front, names.index('get_window_state'))
        frame = next(args for name, args in calls if name == 'set_window_frame')
        self.assertEqual((frame['x'], frame['y']), (0, 0))

    def test_off_window_accessibility_element_is_rejected_before_click(self):
        driver = object.__new__(GuiDriver)
        calls = []
        async def click(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(structured_content={'effect': 'unverifiable'}, is_error=False)
        driver._cua = SimpleNamespace(
            _last_elements=[{'label':'Upload','role':'Button','enabled':True,'element_index':140,
                             'frame':{'x':2064,'y':286,'w':100,'h':45}}],
            _capture_meta={'window_bounds':{'x':960,'y':0,'width':960,'height':1140}},
            click=click,
        )
        driver._screenshot = lambda **kwargs: 'image'
        driver._run = asyncio.run
        with self.assertRaisesRegex(RuntimeError, 'outside.*window|off.window'):
            driver._som_click('Upload')
        self.assertEqual(calls, [])

if __name__ == '__main__':
    unittest.main(verbosity=2)
