"""Journalisation de l'application.

Sans console, `print` ne leve pas d'erreur : il ne fait simplement rien.
Lance par pythonw, le programme perdrait donc silencieusement toute trace de
ce qu'il fait -- et c'est precisement dans ce mode qu'il tourne au
quotidien. Les messages partent donc dans un fichier, et vers la console
seulement lorsqu'il y en a une.

Les exceptions non rattrapees y sont egalement deroutees : sans cela, une
erreur de fond dans un thread disparaitrait sans laisser de trace.
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
    """Gestionnaire qui force l'ecriture jusqu'au disque.

    `flush` ne fait que remettre les octets au systeme, qui les garde en
    cache : coupez l'alimentation et les dernieres lignes disparaissent.
    C'est exactement ce qui empeche de comprendre un incident ou la machine
    s'est arretee -- le journal s'interrompt juste avant ce qu'on cherche.

    Un `fsync` par ligne coute cher sur un journal bavard ; ici quelques
    lignes par minute, et la certitude de les retrouver.
    """

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            if self.stream is not None:
                self.stream.flush()
                os.fsync(self.stream.fileno())
        except (OSError, ValueError):
            pass  # flux ferme ou support qui ne sait pas synchroniser


def log_path(base: Path | None = None) -> Path:
    """Emplacement du journal, a cote de la configuration."""
    from .config import config_path

    folder = base or config_path().parent
    return folder / LOG_FILENAME


def setup(base: Path | None = None, verbose: bool = False) -> logging.Logger:
    """Installe les gestionnaires. Appelable plusieurs fois sans dommage."""
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
        # Un journal illisible ne doit pas empecher l'application de tourner.
        pass

    # sys.stdout vaut None sous pythonw : il n'y a alors pas de console.
    if sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    _install_exception_hooks(logger)
    _configured = True
    logger.info("--- log opened (%s) ---", "console + file" if sys.stdout else "file only")
    return logger


def _install_exception_hooks(logger: logging.Logger) -> None:
    """Deroute vers le journal ce qui serait sinon perdu."""

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


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
