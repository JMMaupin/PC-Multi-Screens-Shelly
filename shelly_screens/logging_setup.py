"""Application logging.

Without a console, `print` raises no error: it simply does nothing.
Launched by pythonw, the program would therefore silently lose every trace
of what it does -- and that is precisely the mode it runs in day to day.
Messages therefore go to a file, and to the console only when there is
one.

Uncaught exceptions are rerouted there too: otherwise, a background error
in a thread would vanish without a trace.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

LOGGER_NAME = "shelly_screens"
LOG_FILENAME = "shelly-screens.log"
MAX_BYTES = 512 * 1024
BACKUP_COUNT = 3

_configured = False


class DurableFileHandler(logging.handlers.RotatingFileHandler):
    """Handler that forces writes all the way to disk.

    `flush` only hands the bytes over to the OS, which keeps them in
    cache: cut the power and the last lines are gone. That is exactly what
    makes it impossible to understand an incident where the machine shut
    down -- the log stops right before what you are looking for.

    One `fsync` per line is costly on a chatty log; here it is a few lines
    per minute, and the certainty of finding them again.
    """

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            if self.stream is not None:
                self.stream.flush()
                os.fsync(self.stream.fileno())
        except (OSError, ValueError):
            pass  # stream closed, or medium that can't sync


def log_path(base: Path | None = None) -> Path:
    """Location of the log file: in `base` if given, otherwise in the
    machine's log folder, one file per account."""
    from . import paths

    return base / LOG_FILENAME if base is not None else paths.default().log_file


def setup(base: Path | None = None, verbose: bool = False) -> logging.Logger:
    """Installs the handlers. Safe to call several times."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    target = log_path(base)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = DurableFileHandler(
            target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # An unwritable log must not stop the application from running.
        pass

    # sys.stdout is None under pythonw: there is no console then.
    if sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    _install_exception_hooks(logger)
    _configured = True
    logger.info("--- log opened (%s) ---", "console + file" if sys.stdout else "file only")
    return logger


def _install_exception_hooks(logger: logging.Logger) -> None:
    """Reroutes to the log what would otherwise be lost."""

    def on_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logger.critical(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback)
        )

    sys.excepthook = on_exception

    def on_thread_exception(args) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        logger.critical(
            "Unhandled exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = on_thread_exception


