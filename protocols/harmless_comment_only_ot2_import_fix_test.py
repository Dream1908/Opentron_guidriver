"""Harmless OT-2 test protocol: one comment, no labware, pipettes, liquids, or motion."""

from opentrons import protocol_api


metadata = {
    "protocolName": "Harmless Comment-Only OT-2 Import Fix Test",
    "author": "Cursor",
    "description": "Comment-only protocol: no labware, instruments, liquids, or motion.",
}

requirements = {
    "robotType": "OT-2",
    "apiLevel": "2.15",
}


def run(protocol: protocol_api.ProtocolContext) -> None:
    """Write one run-log comment and perform no hardware operations."""
    protocol.comment("Harmless comment-only OT-2 import-fix test protocol executed. No motion.")
