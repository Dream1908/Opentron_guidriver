"""
Main entry point for the GUI driver edge service.

This service integrates any desktop instrument software into PUDA using
Hermes computer use (cua-driver) for GUI automation and a vision LLM for
screen reading — no instrument SDK required.

Configuration is loaded from a .env file (see .env.example).
The EdgeRunner publishes heartbeats and host health automatically;
the GuiDriver's @tlm_stream handles instrument status telemetry.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

from puda import EdgeNatsClient, EdgeRunner
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from driver import GuiDriver

# ── logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)      # suppress Anthropic HTTP noise
logging.getLogger("anthropic").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ── configuration ──────────────────────────────────────────────────────────

class Config(BaseSettings):
    """
    Environment-driven configuration for the GUI driver edge service.

    All fields are read from .env (or real environment variables).
    Field names are case-insensitive; underscores and hyphens are interchangeable.
    """

    # ── PUDA core ──────────────────────────────────────────────────────────
    machine_id: str = Field(
        description="Unique PUDA machine identifier (e.g. 'hplc-1', 'pump-controller')."
    )
    nats_servers: str = Field(
        description="Comma-separated NATS server URLs, e.g. nats://bears:4222."
    )

    # ── GUI driver ─────────────────────────────────────────────────────────
    target_app: str = Field(
        description=(
            "Exact window / application name that cua-driver should target, "
            "e.g. 'Chromeleon', 'ChemStation', 'Xcalibur', 'Notepad'."
        )
    )
    capture_interval: float = Field(
        default=30.0,
        description="Seconds between automatic instrument_status telemetry snapshots.",
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
    """Load and validate config; exit the process on failure."""
    try:
        return Config()
    except Exception as e:
        logger.error("Failed to load configuration: %s", e, exc_info=True)
        sys.exit(1)


# ── main ───────────────────────────────────────────────────────────────────

async def main() -> None:
    config = load_config()

    logger.info("=== GUI Driver Configuration ===")
    logger.info("  machine_id   : %s", config.machine_id)
    logger.info("  nats_servers : %s", config.nats_servers)
    logger.info("  target_app   : %s", config.target_app)
    logger.info("  cap_interval : %.1f s", config.capture_interval)
    logger.info("================================")

    logger.info("Initialising GuiDriver for %r…", config.target_app)
    driver = GuiDriver(
        target_app=config.target_app,
        capture_interval=config.capture_interval,
    )
    driver.startup()   # connects to cua-driver MCP, lists available tools

    logger.info("Connecting to NATS at %s…", config.nats_servers)
    edge_nats_client = EdgeNatsClient(
        servers=config.nats_server_list,
        machine_id=config.machine_id,
    )

    runner = EdgeRunner(nats_client=edge_nats_client, machine_driver=driver)
    await runner.connect()

    logger.info(
        "==================== %s GUI Edge Service Ready ====================",
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
