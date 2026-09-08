"""Centralized logging configuration for the HealthKicks Cloud API."""

import logging
import sys

from app.core.config import Settings, settings


def setup_logging(settings_obj: Settings = settings) -> None:
    """Configure root, uvicorn, and application loggers with full traceback verbosity."""
    log_level_name = getattr(settings_obj, "log_level", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine", "healthkicks"):
        logger = logging.getLogger(logger_name)
        logger.setLevel(log_level)
