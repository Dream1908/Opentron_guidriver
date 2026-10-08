"""Second 100 uL water transfer; explicitly use fresh tip A3."""
from opentrons import protocol_api

metadata = {
    "protocolName": "Water 100uL A1 to A1 - Tip A3 - Slots 7 8 9",
    "author": "Yee Jia En",
    "description": "P300 GEN2 right. Source slot 7, destination slot 9; Corning 360 uL flat plates. Tiprack slot 8 offset (0,+2,0) mm; pickup A3. Requested execution through PUDA.",
    "apiLevel": "2.13",
}


def run(protocol: protocol_api.ProtocolContext):
    tiprack = protocol.load_labware("opentrons_96_tiprack_300ul", "8")
    tiprack.set_offset(x=0.0, y=2.0, z=0.0)
    source = protocol.load_labware("corning_96_wellplate_360ul_flat", "7")
    dest = protocol.load_labware("corning_96_wellplate_360ul_flat", "9")
    p300 = protocol.load_instrument("p300_single_gen2", "right", tip_racks=[tiprack])
    p300.pick_up_tip(tiprack["A3"])
    p300.aspirate(100, source["A1"].bottom(1))
    p300.dispense(100, dest["A1"].bottom(1))
    p300.blow_out(dest["A1"].top(-2))
    p300.drop_tip()
