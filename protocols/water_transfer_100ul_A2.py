"""Transfer 100 uL water from slot 7 A1 to slot 9 A1 using tip A2."""
from opentrons import protocol_api

metadata = {
    "protocolName": "Water 100uL A1 to A1 - Tip A2 - Slots 7 8 9",
    "author": "Yee Jia En",
    "description": (
        "P300 GEN2 right mount. Corning 360 uL plates in slots 7 and 9. "
        "Opentrons 300 uL tiprack in slot 8; offset x=0, y=+2, z=0 mm. "
        "Pick tip A2, transfer 100 uL water, discard tip into fixed trash."
    ),
    "apiLevel": "2.13",
}


def run(protocol: protocol_api.ProtocolContext):
    tiprack = protocol.load_labware("opentrons_96_tiprack_300ul", "8")
    tiprack.set_offset(x=0.0, y=2.0, z=0.0)
    source = protocol.load_labware("corning_96_wellplate_360ul_flat", "7")
    dest = protocol.load_labware("corning_96_wellplate_360ul_flat", "9")
    p300 = protocol.load_instrument("p300_single_gen2", "right", tip_racks=[tiprack])

    p300.pick_up_tip(tiprack["A2"])
    p300.aspirate(100, source["A1"].bottom(1))
    p300.dispense(100, dest["A1"].bottom(1))
    p300.blow_out(dest["A1"].top(-2))
    p300.drop_tip()
