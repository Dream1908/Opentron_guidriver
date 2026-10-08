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
        events = []
        driver.navigate_protocols = lambda: events.append('navigate')
        driver._som_click = lambda description: events.append(description)
        driver.click_at = lambda x,y: events.append(('pixel',x,y))
        driver._screenshot = lambda **kwargs: events.append('capture') or 'image'
        async def type_text(text): events.append(('type',text))
        async def key(keys): events.append(('key',keys))
        driver._cua = SimpleNamespace(type_text=type_text,key=key)
        driver._run = asyncio.run
        with patch('driver.time.sleep'):
            driver.import_protocol(str(Path(__file__).resolve()), upload_x=764, upload_y=309)
        self.assertIn(('pixel',764,309),events)
        self.assertNotIn('Choose File button in the import sidebar',events)
        self.assertLess(events.index('capture'),events.index(('type',str(Path(__file__).resolve()))))

    def test_file_dialog_is_selected_instead_of_richer_main_window(self):
        client = CuaDriverClient()
        client._tools = {'get_window_state':object()}
        async def windows(app):
            return [{'pid':1,'window_id':10,'title':'Opentrons OT-2'},
                    {'pid':1,'window_id':20,'title':'Open'}]
        async def call(tool,args):
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
        self.assertEqual(calls,[{'x':764,'y':309}])
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
        self.assertEqual((calls[0][1]['x'], calls[0][1]['y']), (764,309))

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
