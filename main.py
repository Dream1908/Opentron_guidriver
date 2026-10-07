"""
Main entry point for the Opentrons OT-2 GUI driver edge service.

Controls the Opentrons desktop App via Hermes computer use (cua-driver MCP)
and Claude vision — no HTTP API or SSH required.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

from puda import EdgeNatsClient, EdgeRunner
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from driver import OpentronGuiDriver

# ── logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ── configuration ──────────────────────────────────────────────────────────

class Config(BaseSettings):
    # ── PUDA core ──────────────────────────────────────────────────────────
    machine_id: str = Field(description="Unique PUDA machine ID, e.g. 'ot2-1'.")
    nats_servers: str = Field(description="Comma-separated NATS server URLs.")

    # ── Opentrons GUI driver ───────────────────────────────────────────────
    target_app: str = Field(
        default="Opentrons",
        description="Window title of the Opentrons desktop App.",
    )
    robot_name: str = Field(
        default="",
        description=(
            "Display name of the OT-2 in the Opentrons App robot list. "
            "Leave empty to auto-select the first available robot."
        ),
    )
    capture_interval: float = Field(
        default=15.0,
        description="Seconds between run_status telemetry captures.",
    )

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def nats_server_list(self) -> list[str]:
        return [s.strip() for s in self.nats_servers.split(",") if s.strip()]


def load_config() -> Config:
    try:
        return Config()
    except Exception as e:
        logger.error("Failed to load configuration: %s", e, exc_info=True)
        sys.exit(1)


# ── main ───────────────────────────────────────────────────────────────────

async def main() -> None:
    config = load_config()

    logger.info("=== Opentrons GUI Driver ===")
    logger.info("  machine_id   : %s", config.machine_id)
    logger.info("  nats_servers : %s", config.nats_servers)
    logger.info("  target_app   : %s", config.target_app)
    logger.info("  robot_name   : %s", config.robot_name or "<auto>")
    logger.info("  cap_interval : %.1f s", config.capture_interval)
    logger.info("============================")

    driver = OpentronGuiDriver(
        target_app=config.target_app,
        robot_name=config.robot_name,
        capture_interval=config.capture_interval,
    )
    driver.startup()

    edge_nats_client = EdgeNatsClient(
        servers=config.nats_server_list,
        machine_id=config.machine_id,
    )

    runner = EdgeRunner(nats_client=edge_nats_client, machine_driver=driver)
    await runner.connect()
    logger.info(
        "==================== %s OT-2 GUI Edge Service Ready ====================",
        config.machine_id,
    )
    await runner.run()


# ── retry loop ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.warning("Gracefully stopping…")
            sys.exit(0)
        except Exception as e:
            logger.error("Fatal error: %s", e, exc_info=True)
            logger.info("Retrying in 5 s…")
            time.sleep(5)
