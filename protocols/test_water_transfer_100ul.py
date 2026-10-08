"""Regression checks and real Opentrons simulation for this transfer."""
import ast
from pathlib import Path
import unittest
from opentrons import simulate

PROTOCOL = Path(__file__).with_name("water_transfer_100ul_A2.py")

class WaterTransferTest(unittest.TestCase):
    def test_requested_deck_tip_offset_and_transfer(self):
        text = PROTOCOL.read_text(encoding="utf-8")
        tree = ast.parse(text)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        loads = [(ast.literal_eval(n.args[0]), ast.literal_eval(n.args[1]))
                 for n in calls if isinstance(n.func, ast.Attribute)
                 and n.func.attr == "load_labware"]
        self.assertEqual(set(loads), {
            ("opentrons_96_tiprack_300ul", "8"),
            ("corning_96_wellplate_360ul_flat", "7"),
            ("corning_96_wellplate_360ul_flat", "9"),
        })
        offsets = [n for n in calls if isinstance(n.func, ast.Attribute)
                   and n.func.attr == "set_offset"]
        self.assertEqual(len(offsets), 1)
        self.assertEqual(offsets[0].func.value.id, "tiprack")
        self.assertEqual({k.arg: ast.literal_eval(k.value) for k in offsets[0].keywords},
                         {"x": 0.0, "y": 2.0, "z": 0.0})
        instruments = [n for n in calls if isinstance(n.func, ast.Attribute)
                       and n.func.attr == "load_instrument"]
        self.assertEqual([ast.literal_eval(a) for a in instruments[0].args],
                         ["p300_single_gen2", "right"])
        with PROTOCOL.open(encoding="utf-8") as handle:
            runlog, _ = simulate.simulate(handle)
        events = "\n".join(str(item["payload"].get("text", "")) for item in runlog)
        print("\nSIMULATION COMMANDS:\n" + events)
        pickups = [item for item in runlog if item["payload"].get("text", "").startswith("Picking up tip")]
        self.assertEqual(len(pickups), 1)
        self.assertIn("A2", pickups[0]["payload"]["text"])
        self.assertIn("8", pickups[0]["payload"]["text"])
        aspirates = [item for item in runlog if item["payload"].get("text", "").startswith("Aspirating")]
        dispenses = [item for item in runlog if item["payload"].get("text", "").startswith("Dispensing")]
        self.assertEqual(len(aspirates), 1)
        self.assertEqual(len(dispenses), 1)
        self.assertEqual(aspirates[0]["payload"]["volume"], 100)
        self.assertEqual(dispenses[0]["payload"]["volume"], 100)
        self.assertIn("A1", aspirates[0]["payload"]["text"])
        self.assertIn("7", aspirates[0]["payload"]["text"])
        self.assertIn("A1", dispenses[0]["payload"]["text"])
        self.assertIn("9", dispenses[0]["payload"]["text"])
        self.assertIn("Dropping tip", events)

if __name__ == "__main__":
    unittest.main()
