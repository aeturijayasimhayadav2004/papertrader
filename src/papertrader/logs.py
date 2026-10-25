"""Logging: console + rotating file at <log_dir>/papertrader.log."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "papertrader.log"
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter(FORMAT))
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        root.addHandler(fh)
        root.addHandler(sh)
    root.setLevel(level)
    for noisy in ("yfinance", "httpx", "httpcore", "urllib3", "peewee", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path
