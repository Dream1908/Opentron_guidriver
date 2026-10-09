"""Harmless OT-2 import test: records a comment and performs no hardware actions."""

from opentrons import protocol_api


metadata = {
    "protocolName": "Harmless Comment-Only OT-2 Test",
    "author": "Codex",
    "description": "Import-analysis test with no labware, instruments, liquids, or motion.",
}

requirements = {
    "robotType": "OT-2",
    "apiLevel": "2.15",
}


def run(protocol: protocol_api.ProtocolContext) -> None:
    """Add a run-log comment without loading or moving any hardware."""
    protocol.comment("Harmless comment-only OT-2 protocol executed.")
