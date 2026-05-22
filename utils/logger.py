"""Momathi Protocol — Centralized logging setup."""
import logging
import sys


def setup_logger(level: int = logging.INFO) -> None:
    """
    Configure logging for the entire application.
    
    Sets up console and file handlers with consistent formatting.
    Call this once at application startup.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(name)-24s | %(levelname)-5s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("momathi.log"),
        ],
    )
