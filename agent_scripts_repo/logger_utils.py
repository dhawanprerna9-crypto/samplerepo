import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime
from configparser import ConfigParser


config = ConfigParser()
config_path = Path(__file__).parent / "config.ini"
config.read(config_path)

def _project_root() -> Path:
    return Path(__file__).resolve().parent


def suppress_external_logs():
    noisy_loggers = [
        "httpx", "httpcore", "urllib3", "requests",
        "openai", "azure", "azure.core", "msal", "asyncio"
    ]
    for name in noisy_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)


def resolve_thread_log_path(thread_id: str) -> Path:
    project_root = _project_root()
    today = datetime.now().strftime("%Y-%m-%d")
    return project_root / "shared_folder" / str(thread_id) / "logs" / f"orchestrator_{today}.log"


def setup_thread_logging(thread_id: str, log_level: str = "INFO") -> Path:
    log_level = log_level.upper()
    max_bytes =  config.getint("app_log", "log_max_bytes")
    backup_count = config.getint("app_log", "log_backup_count")

    log_path = resolve_thread_log_path(thread_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    suppress_external_logs()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(filename)s | %(message)s"
    )

    # Console handler (only once)
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root_logger.handlers
    ):
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        root_logger.addHandler(ch)

    # Remove previous thread handlers
    for h in list(root_logger.handlers):
        if isinstance(h, RotatingFileHandler) and getattr(h, "_thread_handler", False):
            root_logger.removeHandler(h)
            h.close()

    fh = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    fh.setFormatter(formatter)
    fh._thread_handler = True
    root_logger.addHandler(fh)

    return log_path