import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

LOGS_DIR = Path(os.getenv("LOGS_DIR", "logs"))
LOGS_DIR.mkdir(exist_ok=True)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def configure_logging(
    logger_name: str = "app",
    log_file: Optional[str] = None,
    level: str = LOG_LEVEL,
) -> logging.Logger:
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level))

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, level))
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file:
        log_path = LOGS_DIR / log_file
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        file_handler.setLevel(getattr(logging, level))
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


backend_logger = configure_logging(logger_name="backend", log_file="backend.log")
bot_logger = configure_logging(logger_name="bot", log_file="bot.log")
database_logger = configure_logging(logger_name="database", log_file="database.log")
calendar_logger = configure_logging(logger_name="calendar", log_file="calendar.log")
stt_logger = configure_logging(logger_name="stt", log_file="stt.log")

# Route SQLAlchemy engine + dialect queries → database.log
if LOG_LEVEL == "DEBUG":
    _db_log_path = LOGS_DIR / "database.log"
    _db_file_handler = logging.handlers.RotatingFileHandler(
        str(_db_log_path), maxBytes=10 * 1024 * 1024, backupCount=5
    )
    _db_formatter = logging.Formatter(
        "%(asctime)s | sqlalchemy | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _db_file_handler.setFormatter(_db_formatter)
    for _sa_name in ("sqlalchemy.engine", "sqlalchemy.pool", "sqlalchemy.dialects"):
        _sa_logger = logging.getLogger(_sa_name)
        _sa_logger.setLevel(logging.DEBUG)
        _sa_logger.addHandler(_db_file_handler)
        _sa_logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
