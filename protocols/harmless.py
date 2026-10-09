"""Harmless OT-2 protocol containing no hardware actions."""

from opentrons import protocol_api


metadata = {
    "protocolName": "Harmless Comment-Only OT-2 Test 2",
    "author": "Codex",
    "description": (
        "Comment-only protocol with no labware, instruments, liquids, or motion."
    ),
}

requirements = {
    "robotType": "OT-2",
    "apiLevel": "2.15",
}


def run(protocol: protocol_api.ProtocolContext) -> None:
    """Write one run-log comment and perform no hardware operations."""
    protocol.comment("Harmless comment-only OT-2 protocol test 2 executed.")
