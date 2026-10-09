"""Regression tests; transport is replaced to prevent robot/GUI motion."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driver import CuaDriverClient, OpentronGuiDriver

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
        driver = object.__new__(OpentronGuiDriver)
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

    # ── exact-label commands: select_robot / start_run ─────────────────────
    @staticmethod
    def _el(index, label, role='Button', x=0, y=0, w=100, h=40, actions=('invoke',)):
        return {'element_index': index, 'label': label, 'role': role, 'enabled': True,
                'frame': {'x': x, 'y': y, 'w': w, 'h': h}, 'actions': list(actions)}

    def _scripted_driver(self, snapshots):
        """Driver whose accessibility snapshots are scripted; records invoked elements."""
        driver = object.__new__(OpentronGuiDriver)
        clicks = []
        states = iter(snapshots)
        async def click(**kwargs):
            clicks.append(kwargs['element'])
        driver._cua = SimpleNamespace(click=click)
        driver._run = asyncio.run
        driver._screenshot = lambda **kwargs: 'image'
        driver._snapshot_elements = lambda attempts=3: next(states)
        driver._som_click = lambda description: self.fail('fuzzy click used: ' + description)
        return driver, clicks

    def _robot_panel(self):
        return [
            self._el(16, 'None', 'Group', x=17, y=72, w=1200, h=900),   # whole page
            self._el(18, 'Refresh', x=1587, y=213),
            self._el(19, 'None', 'Group', x=1297, y=253, w=300, h=80),  # the robot card
            self._el(20, 'opentrons', 'Text', x=1408, y=292, w=80, h=20, actions=('text',)),
            self._el(25, 'Proceed to setup', x=1297, y=826, w=300),
        ]

    def _confirm_dialog(self):
        return [
            self._el(31, 'Start run', x=1073, y=215),                   # page button behind
            self._el(33, 'Not started', 'Text', x=700, y=203, actions=('text',)),
            self._el(40, 'Are you sure you want to proceed to run?', 'Text', x=800, y=400,
                     w=500, actions=('text',)),
            self._el(41, "You haven't confirmed the labware placement.", 'Text', x=800, y=440,
                     w=500, actions=('text',)),
            self._el(21, 'Go back', x=850, y=600),
            self._el(22, 'Start run', x=1000, y=600),                   # the dialog's button
        ]

    def test_select_robot_clicks_exact_card_then_exact_proceed_button(self):
        panel = self._robot_panel()
        driver, clicks = self._scripted_driver([panel, panel, [self._el(9, 'Start run')]])
        driver.robot_name = 'opentrons'
        with patch('driver.time.sleep'):
            result = driver.select_robot()
        self.assertEqual(clicks, [19, 25])   # smallest wrapping Group, not the page Group
        self.assertTrue(result['proceeded'])
        self.assertFalse(result['needs_confirmation'])

    def test_select_robot_clicks_card_by_real_pixels_when_its_frame_is_off_window(self):
        panel = self._robot_panel()
        # The card's AX frame lies right of the window: wide card centred in the panel.
        panel[2] = self._el(19, 'None', 'Group', x=1297, y=253, w=348, h=80)
        panel[4] = self._el(25, 'Proceed to setup', x=1297, y=826, w=348, h=40)
        driver, clicks = self._scripted_driver([panel, panel, [self._el(9, 'Start run')]])
        driver.robot_name = 'opentrons'
        driver._cua._capture_meta = {'window_bounds': {'x': 17, 'y': 2, 'width': 1262, 'height': 890},
                                     'screenshot_width': 1260, 'screenshot_height': 888}
        pixels = []
        driver.click_at = lambda x, y: pixels.append((x, y))
        with patch('driver.time.sleep'):
            driver.select_robot()
        # Real centres, not the off-window AX points that cua-driver refuses to click.
        self.assertEqual(pixels, [(1066, 291), (1066, 844)])
        self.assertEqual(clicks, [])

    def test_select_robot_unknown_name_clicks_nothing(self):
        driver, clicks = self._scripted_driver([self._robot_panel()])
        driver.robot_name = ''
        with patch('driver.time.sleep'), self.assertRaisesRegex(RuntimeError, 'No robot card named'):
            driver.select_robot('ot2-missing')
        self.assertEqual(clicks, [])

    def test_select_robot_without_name_picks_first_card(self):
        panel = self._robot_panel()
        driver, clicks = self._scripted_driver([panel, panel, [self._el(9, 'Start run')]])
        driver.robot_name = ''
        with patch('driver.time.sleep'):
            driver.select_robot()
        self.assertEqual(clicks, [19, 25])

    def test_select_robot_returns_dialog_instead_of_clicking_through(self):
        panel = self._robot_panel()
        driver, clicks = self._scripted_driver([panel, panel, self._confirm_dialog()])
        driver.robot_name = 'opentrons'
        with patch('driver.time.sleep'):
            result = driver.select_robot()
        self.assertEqual(clicks, [19, 25])   # nothing clicked after the dialog appeared
        self.assertTrue(result['needs_confirmation'])
        self.assertFalse(result['proceeded'])

    def test_select_robot_requires_open_panel(self):
        driver, clicks = self._scripted_driver([[self._el(1, 'Protocols', 'Hyperlink')]])
        driver.robot_name = 'opentrons'
        with self.assertRaisesRegex(RuntimeError, 'start_setup'):
            driver.select_robot()
        self.assertEqual(clicks, [])

    def test_start_run_without_dialog_reports_running(self):
        driver, clicks = self._scripted_driver([
            [self._el(31, 'Start run'), self._el(33, 'Not started', 'Text')],
            [self._el(34, 'Pause run'), self._el(35, 'Running', 'Text')],
        ])
        with patch('driver.time.sleep'):
            result = driver.start_run()
        self.assertEqual(clicks, [31])
        self.assertTrue(result['started'])
        self.assertEqual(result['run_status'], 'running')
        self.assertFalse(result['needs_confirmation'])

    def test_start_run_asks_the_user_before_clicking_a_dialog(self):
        page = [self._el(31, 'Start run'), self._el(33, 'Not started', 'Text')]
        driver, clicks = self._scripted_driver([page, self._confirm_dialog()])
        with patch('driver.time.sleep'):
            result = driver.start_run()
        self.assertEqual(clicks, [31])        # only the page's own Start run button
        self.assertTrue(result['needs_confirmation'])
        self.assertFalse(result['started'])
        self.assertEqual(result['run_status'], 'not started')
        self.assertIn('Go back', result['dialog']['buttons'])
        self.assertTrue(any('Are you sure' in line for line in result['dialog']['text']))

    def test_dialog_contents_are_scoped_to_the_modal_frame(self):
        driver = object.__new__(OpentronGuiDriver)
        elements = [
            self._el(17, 'ModalShell_Overlay', 'Group', x=129, y=103, w=1148, h=788),  # larger, page-wide
            self._el(18, 'ModalShell_ModalArea', 'Group', x=447, y=375, w=626, h=244),
            self._el(19, 'None', x=1009, y=395, w=34, h=33),                          # close X
            self._el(20, "You haven't confirmed the labware placement.", 'Text',
                     x=477, y=468, w=561, h=39, actions=('text',)),
            self._el(21, 'Go back', x=809, y=541, w=110, h=48),
            self._el(22, 'Start run', x=928, y=542, w=115, h=46),
            self._el(29, '--:--:--', 'Text', x=693, y=241, w=45, h=19, actions=('text',)),  # page noise
            self._el(31, 'Start run', x=1073, y=215, w=145, h=46),                    # page button
            self._el(41, 'Instruments Review required pipettes', x=169, y=503, w=1049, h=60),  # wide, behind
        ]
        dialog = driver._pending_dialog(elements)
        self.assertEqual(dialog['text'], ["You haven't confirmed the labware placement."])
        self.assertEqual(sorted(dialog['buttons']), ['Go back', 'Start run'])
        self.assertEqual(driver._dialog_confirm_button(elements, 'Start run')['element_index'], 22)

    def test_start_run_confirms_only_after_the_dialog_was_reported(self):
        page = [self._el(31, 'Start run'), self._el(33, 'Not started', 'Text')]
        running = [self._el(35, 'Running', 'Text')]
        driver, clicks = self._scripted_driver([
            page, self._confirm_dialog(),          # first call: reported, not clicked
            self._confirm_dialog(), running,       # second call: user approved
        ])
        with patch('driver.time.sleep'):
            driver.start_run()
            result = driver.start_run(confirm_dialog=True)
        self.assertEqual(clicks, [31, 22])         # dialog button, not the page button
        self.assertTrue(result['started'])

    def test_start_run_ignores_confirm_flag_for_an_unreported_dialog(self):
        driver, clicks = self._scripted_driver([self._confirm_dialog()])
        with patch('driver.time.sleep'):
            result = driver.start_run(confirm_dialog=True)
        self.assertEqual(clicks, [])               # never reviewed by the user: no click
        self.assertTrue(result['needs_confirmation'])

    def test_import_tolerates_rejected_clicks_and_verifies_by_readback(self):
        driver = object.__new__(OpentronGuiDriver)
        driver.target_app = 'Opentrons OT-2'
        clicked = []
        async def click(**kwargs):
            clicked.append(kwargs.get('element'))
            if kwargs.get('element') == 157:     # Import is a normal, strict click
                return SimpleNamespace(structured_content={})
            raise RuntimeError("cua-driver input rejected: {'code': 'tool_invocation_failed'}")
        async def set_value(index, value): clicked.append(('set_value', index, value))
        async def matching_windows(app): return [{'title': 'Opentrons OT-2'}]
        dialog_elements = [
            self._el(7, 'File name:', 'Edit', actions=('set_value',)),
            self._el(8, 'Open', 'Button', w=120, h=40),
            self._el(9, 'Open', 'Button', w=20, h=20),
        ]
        driver._cua = SimpleNamespace(click=click, set_value=set_value, _last_elements=dialog_elements,
                                      _tools={'set_value': object()}, _matching_windows=matching_windows)
        driver._run = asyncio.run
        driver._screenshot = lambda **kwargs: 'image'
        driver._snapshot_elements = lambda attempts=3: [self._el(157, 'Import'), self._el(158, 'Upload')]
        driver.navigate_protocols = lambda: True
        driver._wait_file_dialog = lambda appear=True, timeout=15.0: {'title': 'Open', 'app_name': ''}
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            nameless = Path(folder) / 'nameless.py'
            nameless.write_text('def run(protocol):\n    pass\n', encoding='utf-8')
            with patch('driver.time.sleep'):
                result = driver.import_protocol(str(nameless))
        self.assertEqual(clicked[0], 157)                          # exact Import element
        self.assertEqual(clicked[1], 158)                          # exact Upload element
        self.assertEqual(clicked[2][:2], ('set_value', 7))
        self.assertEqual(clicked[3], 8)                            # largest Open button
        self.assertEqual(result['image_b64'], 'image')
        self.assertIsNone(result['imported'])                      # no protocolName in a test file

    def test_upload_pixel_mirrors_the_off_window_slide_in_panel(self):
        driver = object.__new__(OpentronGuiDriver)
        driver._cua = SimpleNamespace(
            _last_elements=[self._el(158, 'Upload', x=1422, y=288, w=100, h=45)],
            _capture_meta={'window_bounds': {'x': 17, 'y': 2, 'width': 1262, 'height': 890},
                           'screenshot_width': 1260, 'screenshot_height': 888},
        )
        # Verified live: this panel's Upload button really sits at about (1064, 309).
        self.assertEqual(driver._upload_pixel(), (1065, 308))
        # An on-window frame (e.g. a future app that exposes the true position) is used as is.
        driver._cua._last_elements = [self._el(158, 'Upload', x=1000, y=288, w=100, h=45)]
        self.assertEqual(driver._upload_pixel(), (1033, 308))
        driver._cua._last_elements = []
        self.assertIsNone(driver._upload_pixel())

    def test_import_clicks_derived_upload_pixels_not_the_accessibility_invoke(self):
        driver = object.__new__(OpentronGuiDriver)
        driver.target_app = 'Opentrons OT-2'
        events = []
        driver._cua = SimpleNamespace(
            _last_elements=[self._el(158, 'Upload', x=1422, y=288, w=100, h=45)],
            _capture_meta={'window_bounds': {'x': 17, 'y': 2, 'width': 1262, 'height': 890},
                           'screenshot_width': 1260, 'screenshot_height': 888},
            _tools={}, _matching_windows=None,
        )
        driver._run = asyncio.run
        driver._screenshot = lambda **kwargs: 'image'
        driver._snapshot_elements = lambda attempts=3: list(driver._cua._last_elements)
        driver.click_at = lambda x, y: events.append(('pixel', x, y))
        driver.navigate_protocols = lambda: events.append('navigate')
        def click_label(label, *args, **kwargs):
            if label != 'Import':
                self.fail('accessibility invoke of %r must not be the first route' % label)
            events.append('import')
        driver._click_label = click_label
        driver._wait_file_dialog = lambda appear=True, timeout=15.0: (
            {'title': 'Open', 'app_name': 'portal'} if appear else None)
        driver._type_into_file_dialog = lambda dialog, path: events.append(('typed', path))
        with patch('driver.time.sleep'):
            driver.import_protocol(str(Path(__file__).resolve()))
        self.assertEqual(events[:3], ['navigate', 'import', ('pixel', 1065, 308)])

    def test_import_still_fails_when_the_dialog_never_opens(self):
        driver = object.__new__(OpentronGuiDriver)
        async def click(**kwargs): return SimpleNamespace(structured_content={})
        driver._cua = SimpleNamespace(click=click)
        driver._run = asyncio.run
        driver._screenshot = lambda **kwargs: 'image'
        driver._snapshot_elements = lambda attempts=3: [self._el(157, 'Import'), self._el(158, 'Upload')]
        driver.navigate_protocols = lambda: True
        driver._wait_file_dialog = lambda appear=True, timeout=15.0: None
        with patch('driver.time.sleep'), self.assertRaisesRegex(RuntimeError, 'did not open'):
            driver.import_protocol(str(Path(__file__).resolve()))

    def test_protocol_name_is_read_from_metadata(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'p.py'
            path.write_text('metadata = {\n    "protocolName": "My Test",\n}\n', encoding='utf-8')
            self.assertEqual(OpentronGuiDriver._protocol_name(str(path)), 'My Test')
        self.assertIsNone(OpentronGuiDriver._protocol_name('C:/definitely/missing.py'))

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
        driver = object.__new__(OpentronGuiDriver)
        driver._cua = SimpleNamespace(_capture_meta={'screenshot_width':958,'screenshot_height':1138})
        driver._screenshot = lambda **kwargs: 'before'
        driver._run = lambda coro: self.fail('Out-of-bounds input must not be dispatched')
        async def click(**kwargs):
            self.fail('Out-of-bounds input must not be dispatched')
        driver._cua.click = click
        with self.assertRaises(ValueError):
            driver.click_at(1200,309)

    def test_puda_pixel_command_returns_delivery_effect_and_readback(self):
        driver = object.__new__(OpentronGuiDriver)
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
        driver = object.__new__(OpentronGuiDriver)
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
