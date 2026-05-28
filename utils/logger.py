"""Momathi Protocol — Centralized logging setup."""
import logging
import sys
from logging.handlers import RotatingFileHandler
import os


def setup_logger(
    level: int = logging.INFO,
    log_file: str = "momathi.log",
    max_bytes: int = 5 * 1024 * 1024,  # 5 MB
    backup_count: int = 3,
) -> None:
    """
    Configure logging for the entire application.
    
    Sets up console and rotating file handlers with consistent formatting.
    Call this once at application startup.
    
    Args:
        level: Logging level (default: INFO)
        log_file: Path to log file (default: momathi.log)
        max_bytes: Maximum size of each log file before rotation (default: 5 MB)
        backup_count: Number of backup log files to keep (default: 3)
    """
    # Create formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(name)-24s | %(levelname)-5s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Clear any existing handlers
    root_logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Rotating file handler
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # Log rotation info
    logger = logging.getLogger("momathi.logger")
    logger.info(
        "Logging initialized: file=%s, max_bytes=%d MB, backup_count=%d",
        log_file,
        max_bytes / (1024 * 1024),
        backup_count,
    )
