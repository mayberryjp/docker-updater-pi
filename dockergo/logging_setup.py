"""Bounded persistent diagnostics, mirrored to stderr for systemd."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(path: str):
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        destination, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("dockergo")
    logger.setLevel(logging.INFO)
    for output in (handler, logging.StreamHandler()):
        output.setFormatter(formatter)
        logger.addHandler(output)
    return logger
