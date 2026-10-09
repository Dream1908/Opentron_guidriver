"""Harmless OT-2 import test with no hardware actions."""

from opentrons import protocol_api


metadata = {
    "protocolName": "Harmless Comment-Only OT-2 Test 2026-10-09",
    "author": "Codex",
    "description": "Comment-only import test with no labware, instruments, liquids, or motion.",
}

requirements = {
    "robotType": "OT-2",
    "apiLevel": "2.15",
}


def run(protocol: protocol_api.ProtocolContext) -> None:
    """Write one run-log comment and perform no hardware operations."""
    protocol.comment("Harmless comment-only OT-2 protocol for 2026-10-09 executed.")
