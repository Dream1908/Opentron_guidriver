"""One-shot visible Import-panel diagnostic; intentionally does not start a run."""

import time

from driver import OpentronGuiDriver


driver = OpentronGuiDriver(target_app="Opentrons OT-2", robot_name="opentrons")
try:
    driver._startup()
    driver.navigate_protocols()
    driver._som_click("Import button in the top right corner")
    driver._run(driver._cua.key("return", delivery_mode="foreground"))
    time.sleep(2)
    driver._screenshot(mode="som")
    print([
        (item.get("label"), item.get("role"), item.get("frame"))
        for item in driver._cua._last_elements
        if str(item.get("label", "")).casefold() in {"import", "upload", "browse", "exit"}
    ])
finally:
    driver.shutdown()
